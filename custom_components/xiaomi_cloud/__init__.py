"""小米云服务集成组件（后端代理模式）."""
from __future__ import annotations

import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components import persistent_notification
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD

from .DataUpdateCoordinator import XiaomiCloudDataUpdateCoordinator
from .const import (
    DOMAIN,
    COORDINATOR,
    UNDO_UPDATE_LISTENER,
    CONF_ENDPOINT,
    DEFAULT_ENDPOINT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

_PLATFORMS = ["device_tracker", "sensor", "button", "switch"]

_FAST_RETRY_INTERVAL = 10   # 秒
_FAST_RETRY_MAX = 12


def _mask(text: str) -> str:
    if not text:
        return text
    if len(text) >= 7:
        return text[:3] + "*" * (len(text) - 7) + text[-4:]
    if len(text) > 3:
        return text[:3] + "*" * (len(text) - 3)
    return "***"


# ──────────────────────────────────────────────
# FastRetry：后台每 10s 重试，最多 12 次
# ──────────────────────────────────────────────

def _schedule_fast_retry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: XiaomiCloudDataUpdateCoordinator,
) -> None:
    from homeassistant.helpers.event import async_call_later

    entry_id = entry.entry_id
    domain_data = hass.data.get(DOMAIN, {}).get(entry_id, {})

    # 取消上一个定时
    old_task = domain_data.get("fast_retry_task")
    if old_task:
        old_task()

    retry_count = domain_data.get("fast_retry_count", 0)

    if retry_count >= _FAST_RETRY_MAX:
        _LOGGER.warning("[FastRetry] 已达到最大重试次数 (%d)", _FAST_RETRY_MAX)
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_initializing_{entry_id}")
        persistent_notification.async_create(
            hass,
            title="小米云服务 初始化超时",
            message=(
                "后端登录耗时过长。\n\n"
                "集成已加载，设备数据获取后实体将自动出现。\n"
                "您可以：\n"
                "- 等待几分钟后手动刷新页面\n"
                "- 重新加载集成（设置 → 小米云服务 → 重新加载）\n"
                "- 检查后端服务状态和日志"
            ),
            notification_id=f"{DOMAIN}_fallback_{entry_id}",
        )
        return

    async def _do_fast_retry(_now=None):
        domain_data = hass.data.get(DOMAIN, {}).get(entry_id)
        if not domain_data:
            return

        count = domain_data.get("fast_retry_count", 0)
        domain_data["fast_retry_count"] = count + 1

        _LOGGER.info("[FastRetry] %d/%d", count + 1, _FAST_RETRY_MAX)

        pct = int((count + 1) / _FAST_RETRY_MAX * 100)
        bar = "=" * (pct // 10) + "-" * (10 - pct // 10)
        persistent_notification.async_create(
            hass,
            title=f"小米云服务 初始化中 ({pct}%)",
            message=(
                f"正在初始化小米云服务...\n\n"
                f"后台登录进度：[{bar}] {count + 1}/{_FAST_RETRY_MAX}\n"
                f"预计还需 {(_FAST_RETRY_MAX - count - 1) * _FAST_RETRY_INTERVAL} 秒\n\n"
                f"初始化完成后此通知会自动关闭。"
            ),
            notification_id=f"{DOMAIN}_initializing_{entry_id}",
        )

        try:
            await coordinator.async_refresh()

            sync_data = coordinator.data
            if sync_data:
                code = sync_data.get("code", 0)
                reason = sync_data.get("reason", "")
                devices = sync_data.get("devices", [])

                _LOGGER.info("[FastRetry] code=%s, devices=%d, reason=%s", code, len(devices), reason)

                if code == 0 and devices and reason not in ["NO_SESSION", "LOGIN_IN_PROGRESS"]:
                    _LOGGER.info("[FastRetry] 成功获取 %d 个设备", len(devices))

                    old = domain_data.get("fast_retry_task")
                    if old:
                        old()
                        domain_data["fast_retry_task"] = None
                    domain_data["fast_retry_count"] = 0

                    persistent_notification.async_dismiss(hass, f"{DOMAIN}_initializing_{entry_id}")
                    persistent_notification.async_create(
                        hass,
                        title="小米云服务 已就绪",
                        message=f"初始化完成！已加载 {len(devices)} 个设备。\n\n此通知将在 10 秒后自动关闭",
                        notification_id=f"{DOMAIN}_ready_{entry_id}",
                    )

                    async def _auto_dismiss(_now=None):
                        persistent_notification.async_dismiss(hass, f"{DOMAIN}_ready_{entry_id}")

                    async_call_later(hass, 10, _auto_dismiss)
                    return

            _schedule_fast_retry(hass, entry, coordinator)

        except Exception as e:
            _LOGGER.warning("[FastRetry] 异常: %s: %s", type(e).__name__, e)
            _schedule_fast_retry(hass, entry, coordinator)

    cancel_fn = async_call_later(hass, _FAST_RETRY_INTERVAL, _do_fast_retry)
    domain_data["fast_retry_task"] = cancel_fn


# ──────────────────────────────────────────────
# Setup / Unload
# ──────────────────────────────────────────────

async def async_setup(hass: HomeAssistant, config) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """设置小米云服务（后端代理模式）。"""
    hass.data.setdefault(DOMAIN, {})
    entry_id = config_entry.entry_id

    username = config_entry.options.get(CONF_USERNAME, config_entry.data.get(CONF_USERNAME, ""))
    password = config_entry.options.get(CONF_PASSWORD, config_entry.data.get(CONF_PASSWORD, ""))
    endpoint = config_entry.options.get(
        CONF_ENDPOINT, config_entry.data.get(CONF_ENDPOINT, DEFAULT_ENDPOINT),
    )
    update_interval = config_entry.options.get(
        CONF_UPDATE_INTERVAL, config_entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
    )

    _LOGGER.info(
        "初始化小米云服务（后端代理模式），endpoint=%s，用户=%s",
        endpoint, _mask(username),
    )

    coordinator = XiaomiCloudDataUpdateCoordinator(
        hass,
        endpoint=endpoint,
        username=username,
        password=password,
        update_interval_minutes=int(update_interval),
        config_entry_id=entry_id,
    )

    hass.data[DOMAIN][entry_id] = {
        COORDINATOR:                        coordinator,
        "device_tracker_entities_created":  False,
        "sensor_entities_created":          False,
        "buttons_added":                    False,
        "fast_retry_count":                 0,
        "fast_retry_task":                  None,
    }

    persistent_notification.async_create(
        hass,
        title="小米云服务 正在初始化...",
        message=(
            f"正在连接小米云服务...\n\n"
            f"账号: {_mask(username)}\n"
            f"后端: {endpoint}\n\n"
            f"首次登录需要 30-60 秒，请耐心等待..."
        ),
        notification_id=f"{DOMAIN}_initializing_{entry_id}",
    )

    # 先注入凭据（非阻塞）
    await coordinator.async_ensure_auth()

    # 首次刷新：失败也继续 setup，不阻塞
    need_fast_retry = False
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as e:
        _LOGGER.warning("首次同步失败: %s，将启动快速重试", e)
        need_fast_retry = True

    sync_data = coordinator.data
    reason = sync_data.get("reason", "") if sync_data else ""
    code = sync_data.get("code", 0) if sync_data else -1
    devices = sync_data.get("devices", []) if sync_data else []

    if not sync_data or not devices:
        need_fast_retry = True
    elif reason in ["NO_SESSION", "LOGIN_IN_PROGRESS"] or code == 990:
        need_fast_retry = True
    elif devices:
        _LOGGER.info("小米云服务集成已加载，获取到 %d 个设备", len(devices))

    if need_fast_retry:
        _schedule_fast_retry(hass, config_entry, coordinator)
        persistent_notification.async_create(
            hass,
            title="小米云服务 后台登录中...",
            message=(
                f"正在等待后端登录小米账号...\n\n"
                f"当前状态: {reason or 'CONNECTING'}\n"
                f"预计耗时: 30-60 秒\n\n"
                f"设备数据加载完成后，实体将自动出现。\n"
                f"请勿重新加载集成。"
            ),
            notification_id=f"{DOMAIN}_initializing_{entry_id}",
        )
    else:
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_initializing_{entry_id}")
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_fallback_{entry_id}")

    config_entry.async_on_unload(config_entry.add_update_listener(_reload_entry))

    await hass.config_entries.async_forward_entry_setups(config_entry, _PLATFORMS)

    _LOGGER.info("小米云服务设置完成")
    return True


async def _reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """卸载配置入口。"""
    entry_id = config_entry.entry_id
    unload_ok = await hass.config_entries.async_unload_platforms(config_entry, _PLATFORMS)

    if unload_ok:
        if DOMAIN in hass.data and entry_id in hass.data[DOMAIN]:
            domain_data = hass.data[DOMAIN].pop(entry_id)
            # 取消 fast retry
            cancel = domain_data.get("fast_retry_task")
            if cancel:
                cancel()

        persistent_notification.async_dismiss(hass, f"{DOMAIN}_initializing_{entry_id}")
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_fallback_{entry_id}")
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_ready_{entry_id}")

    return unload_ok
