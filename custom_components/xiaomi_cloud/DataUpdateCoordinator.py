"""Coordinator for Xiaomi Cloud integration — calls backend /sync endpoint."""
import asyncio
import datetime
import hashlib
import logging
import math

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    FAST_RETRY_DEFAULT_SECONDS,
    FAST_RETRY_MAX,
    CONF_GAODE_APIKEY,
    MOVEMENT_FAST_INTERVAL,
    MOVEMENT_MEDIUM_INTERVAL,
    MOVEMENT_SLOW_INTERVAL,
    MOVEMENT_MEDIUM_SPEED_KMH,
    MOVEMENT_FAST_SPEED_KMH,
)

_LOGGER = logging.getLogger(__name__)


class XiaomiCloudDataUpdateCoordinator(DataUpdateCoordinator):
    """只调用后端 /sync 的协调器。不负责实体生命周期，不触发 reload。"""

    def __init__(
        self,
        hass,
        endpoint: str,
        username: str,
        password: str,
        update_interval_minutes: int,
        config_entry_id: str,
    ):
        self._endpoint = endpoint.rstrip("/")
        self._username = username
        self._password = password
        self._session_key = hashlib.sha256(username.encode()).hexdigest()[:32]
        self._config_entry_id = config_entry_id

        self._fast_retry_count = 0
        self._fast_retry_task: asyncio.Task = None
        self._current_interval_minutes = update_interval_minutes
        self._max_movement_speed_kmh: float | None = None
        self._movement_samples: dict[str, dict] = {}

        # 逆地理编码缓存：device_id -> {lat, lng, address, timestamp}
        self._geocode_cache: dict = {}
        self._geocode_min_interval = 60  # 同一位置 60s 内不重复调用高德 API

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(minutes=update_interval_minutes),
        )

    # ──────────────────────────────────────────────
    # 核心更新逻辑
    # ──────────────────────────────────────────────

    async def _async_update_data(self) -> dict:
        """调用后端 /sync，解析协议响应。"""
        session = async_get_clientsession(self.hass)
        url = f"{self._endpoint}/sync"
        # force_locate=True：后端每次 /sync 都自动判断位置是否陈旧并触发 syncMode=2
        body = {"session_key": self._session_key, "force_locate": True}

        try:
            async with session.post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"后端返回 HTTP {resp.status}")
                data = await resp.json()

        except asyncio.TimeoutError:
            raise UpdateFailed("后端请求超时")
        except aiohttp.ClientError as e:
            raise UpdateFailed(f"后端连接失败: {e}")

        code   = data.get("code", -1)
        reason = data.get("reason") or data.get("message", "")

        # ── 需要鉴权 ──
        if code == 990:
            retry_after = data.get("retry_after") or FAST_RETRY_DEFAULT_SECONDS

            if reason == "AUTH_FAILED":
                _LOGGER.error(
                    "小米后端鉴权失败（AUTH_FAILED），请检查后端日志或账号状态，停止 FastRetry"
                )
                self._cancel_fast_retry()
                return self._stale_data(code, reason)

            # LOGIN_IN_PROGRESS 或通用 990
            if self._fast_retry_count >= FAST_RETRY_MAX:
                # 已达上限，静默返回 stale data，不再刷日志
                _LOGGER.debug(
                    "后端返回 code=990 reason=%s，FastRetry 已达上限，等待后端恢复",
                    reason,
                )
                return self._stale_data(code, reason)

            _LOGGER.info(
                "后端返回 code=990 reason=%s，触发 FastRetry（%d/%d）",
                reason, self._fast_retry_count + 1, FAST_RETRY_MAX,
            )
            self._schedule_fast_retry(retry_after)
            return self._stale_data(code, reason)

        # ── 后端/网络错误 ──
        if code < 0:
            raise UpdateFailed(f"后端错误 code={code} reason={reason}")

        # ── 成功（code=0）──
        self._cancel_fast_retry()
        self._fast_retry_count = 0

        devices = data.get("devices") or []

        # 根据所有定位实体中最快的移动速度动态调整轮询间隔
        if devices:
            self._update_interval_dynamically(devices)

        # 逆地理编码：有高德 API Key 时，自动填充 device["address"]
        if devices and self._get_amap_api_key():
            await self._geocode_devices(devices)

        return data

    # ──────────────────────────────────────────────
    # 启动时注入凭据（不阻塞 setup）
    # ──────────────────────────────────────────────

    async def async_ensure_auth(self) -> dict:
        """
        调用后端 /auth/ensure，注入 username/password。
        后端会在后台触发登录；此调用立即返回，不等待登录完成。
        """
        session = async_get_clientsession(self.hass)
        url  = f"{self._endpoint}/auth/ensure"
        body = {
            "session_key": self._session_key,
            "username":    self._username,
            "password":    self._password,
        }
        try:
            async with session.post(
                url, json=body, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                return await resp.json()
        except Exception as e:
            _LOGGER.warning("auth/ensure 调用失败（非致命）: %s", e)
            return {}

    # ──────────────────────────────────────────────
    # FastRetry 内部实现
    # ──────────────────────────────────────────────

    def _schedule_fast_retry(self, retry_after: int) -> None:
        if self._fast_retry_count >= FAST_RETRY_MAX:
            _LOGGER.error(
                "FastRetry 已达上限 %d 次，停止。请检查后端服务或手动重载集成",
                FAST_RETRY_MAX,
            )
            return

        self._fast_retry_count += 1

        async def _do_retry():
            await asyncio.sleep(retry_after)
            await self.async_refresh()

        self._cancel_fast_retry()
        self._fast_retry_task = self.hass.async_create_task(_do_retry())

    def _cancel_fast_retry(self) -> None:
        if self._fast_retry_task and not self._fast_retry_task.done():
            self._fast_retry_task.cancel()
        self._fast_retry_task = None

    def _stale_data(self, code: int, reason: str) -> dict:
        """返回上次数据（维持实体当前值），或空壳。"""
        if self.data:
            return {**self.data, "code": code, "reason": reason, "ok": False}
        return {"code": code, "reason": reason, "ok": False, "devices": []}

    # ──────────────────────────────────────────────
    # 基于移动速度的三档动态轮询间隔
    # ──────────────────────────────────────────────

    @staticmethod
    def _distance_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """使用 Haversine 公式计算两点间距离。"""
        radius = 6_371_000
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        value = (
            math.sin(delta_lat / 2) ** 2
            + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
        )
        value = min(max(value, 0.0), 1.0)
        return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))

    def _device_speed_kmh(self, device: dict) -> float | None:
        """根据相邻两次有效定位计算速度，并过滤定位精度范围内的漂移。"""
        device_id = device.get("device_id")
        lat = device.get("latitude")
        lon = device.get("longitude")
        timestamp = device.get("locate_time") or device.get("ts")
        if not device_id or lat is None or lon is None or timestamp is None or device.get("stale"):
            return None

        try:
            lat = float(lat)
            lon = float(lon)
            timestamp = int(timestamp)
            accuracy = max(float(device.get("accuracy") or 0), 0)
        except (TypeError, ValueError):
            return None

        previous = self._movement_samples.get(device_id)
        if previous and timestamp <= previous["timestamp"]:
            speed = previous.get("speed_kmh")
            if speed is not None:
                device["movement_speed_kmh"] = round(speed, 2)
            return speed

        speed = None
        if previous:
            elapsed_seconds = timestamp - previous["timestamp"]
            distance_metres = self._distance_metres(
                previous["lat"], previous["lon"], lat, lon
            )
            noise_radius = max(accuracy, previous["accuracy"], 30.0)
            if distance_metres <= noise_radius:
                speed = 0.0
            elif elapsed_seconds > 0:
                speed = distance_metres / elapsed_seconds * 3.6

        self._movement_samples[device_id] = {
            "lat": lat,
            "lon": lon,
            "timestamp": timestamp,
            "accuracy": accuracy,
            "speed_kmh": speed,
        }
        if speed is not None:
            device["movement_speed_kmh"] = round(speed, 2)
        return speed

    def _update_interval_dynamically(self, devices: list) -> None:
        speeds = [
            speed
            for device in devices
            if (speed := self._device_speed_kmh(device)) is not None
        ]
        max_speed = max(speeds) if speeds else None
        self._max_movement_speed_kmh = max_speed

        if max_speed is None:
            target = MOVEMENT_MEDIUM_INTERVAL
        elif max_speed >= MOVEMENT_FAST_SPEED_KMH:
            target = MOVEMENT_FAST_INTERVAL
        elif max_speed >= MOVEMENT_MEDIUM_SPEED_KMH:
            target = MOVEMENT_MEDIUM_INTERVAL
        else:
            target = MOVEMENT_SLOW_INTERVAL

        for device in devices:
            device["polling_interval_minutes"] = target

        if target != self._current_interval_minutes:
            self._current_interval_minutes = target
            self.update_interval = datetime.timedelta(minutes=target)
            _LOGGER.info(
                "根据实体移动速度动态调整轮询间隔为 %d 分钟（最高速度=%s km/h）",
                target,
                f"{max_speed:.2f}" if max_speed is not None else "未知",
            )

    @property
    def current_interval_minutes(self) -> int:
        """当前生效的定位检测间隔。"""
        return self._current_interval_minutes

    @property
    def max_movement_speed_kmh(self) -> float | None:
        """最近一次轮询中所有设备的最高移动速度。"""
        return self._max_movement_speed_kmh

    # ──────────────────────────────────────────────
    # 高德逆地理编码
    # ──────────────────────────────────────────────

    def _get_amap_api_key(self) -> str:
        entry = self.hass.config_entries.async_get_entry(self._config_entry_id)
        if not entry:
            return ""
        return entry.options.get(CONF_GAODE_APIKEY, "") or entry.data.get(CONF_GAODE_APIKEY, "")

    async def _geocode_devices(self, devices: list) -> None:
        now = datetime.datetime.now().timestamp()
        for device in devices:
            device_id = device.get("device_id")
            # 优先用后端已转换的 GCJ-02 坐标，回退到 lat/lng
            gcj_lat = device.get("gcj02_lat") or device.get("lat")
            gcj_lng = device.get("gcj02_lon") or device.get("lng")
            if not device_id or gcj_lat is None or gcj_lng is None:
                continue

            cached = self._geocode_cache.get(device_id)
            if (cached
                    and cached.get("lat") == gcj_lat
                    and cached.get("lng") == gcj_lng
                    and (now - cached.get("timestamp", 0)) < self._geocode_min_interval):
                device["address"] = cached.get("address", "")
                continue

            try:
                address = await self._gaode_reverse_geocode(gcj_lng, gcj_lat)
                device["address"] = address
                self._geocode_cache[device_id] = {
                    "lat": gcj_lat, "lng": gcj_lng,
                    "address": address, "timestamp": now,
                }
            except Exception as e:
                _LOGGER.warning("逆地理编码失败 device=...%s: %s", str(device_id)[-4:], e)

    async def _gaode_reverse_geocode(self, lng: float, lat: float) -> str:
        params = {
            "key":        self._get_amap_api_key(),
            "location":   f"{lng},{lat}",
            "extensions": "base",
            "output":     "json",
        }
        session = async_get_clientsession(self.hass)
        async with session.get(
            "https://restapi.amap.com/v3/geocode/regeo",
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status}")
            data = await resp.json()
            if data.get("status") != "1":
                raise Exception(f"高德 API 错误: {data.get('info', '未知')}")
            address = data.get("regeocode", {}).get("formatted_address", "")
            if not address:
                raise Exception("未返回地址信息")
            return address
