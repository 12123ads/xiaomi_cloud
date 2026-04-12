"""按钮平台 — 响铃，直接调用后端 /command/ring 接口。"""
import logging
import time
from collections import Counter

import aiohttp

from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, COORDINATOR

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]
    entry_id    = config_entry.entry_id

    def _create_buttons(devs):
        bases = _compute_bases(devs)
        return [
            XiaomiRingButton(
                coordinator,
                dev["device_id"],
                _make_slug(dev.get("name", "") or dev.get("model", ""), dev["device_id"], bases),
                config_entry,
            )
            for dev in devs
            if dev.get("device_id")
        ]

    devices_data = _get_devices(coordinator)
    if devices_data:
        entities = _create_buttons(devices_data)
        _LOGGER.info("创建 %d 个按钮实体", len(entities))
        async_add_entities(entities, False)
        hass.data[DOMAIN][entry_id]["buttons_added"] = True
        return

    _LOGGER.debug("数据未就绪，注册监听器等待设备数据")

    @callback
    def _on_data_available():
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
        if entry_data is None or entry_data.get("buttons_added"):
            return
        devs = _get_devices(coordinator)
        if not devs:
            return
        entry_data["buttons_added"] = True
        entities = _create_buttons(devs)
        _LOGGER.info("创建 %d 个按钮实体", len(entities))
        async_add_entities(entities, False)

    config_entry.async_on_unload(
        coordinator.async_add_listener(_on_data_available)
    )


def _get_devices(coordinator) -> list:
    if not coordinator.data or not isinstance(coordinator.data, dict):
        return []
    return coordinator.data.get("devices") or []


def _compute_bases(devs: list) -> dict:
    import re
    slugs = []
    for dev in devs:
        if not dev.get("device_id"):
            continue
        raw = dev.get("name", "") or dev.get("model", "") or ""
        text = re.sub(r'[^a-z0-9]+', '_', raw.lower().strip())
        text = re.sub(r'_+', '_', text).strip('_')
        if not text:
            text = dev["device_id"][:6].lower()
        if not text.startswith("xiaomi_"):
            text = f"xiaomi_{text}"
        slugs.append(text)
    return Counter(slugs)


def _make_slug(raw_name: str, device_id: str, bases: dict) -> str:
    import re
    text = raw_name.lower().strip() if raw_name else ""
    text = re.sub(r'[^a-z0-9]+', '_', text)
    text = re.sub(r'_+', '_', text).strip('_')
    if not text:
        text = device_id[:6].lower() if len(device_id) >= 6 else device_id.lower()
    if not text.startswith("xiaomi_"):
        text = f"xiaomi_{text}"
    if bases.get(text, 0) > 1:
        sfx = device_id[-4:] if len(device_id) >= 4 else device_id
        text = f"{text}_{sfx}"
    return text


async def async_ring(hass, endpoint: str, session_key: str, device_id: str) -> dict:
    """向后端发送响铃指令，返回响应 dict。"""
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            f"{endpoint}/command/ring",
            json={"session_key": session_key, "device_id": device_id},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return await resp.json()
    except Exception as e:
        _LOGGER.error("响铃请求失败 device=...%s: %s", device_id[-4:], e)
        return {"code": -1, "reason": str(e)}


class XiaomiRingButton(ButtonEntity):
    _attr_has_entity_name      = True
    _attr_name                 = "响铃"
    _attr_icon                 = "mdi:bell-ring"

    def __init__(self, coordinator, device_id: str, slug: str, config_entry):
        self._coordinator = coordinator
        self._device_id   = device_id
        self._endpoint    = coordinator._endpoint
        self._session_key = coordinator._session_key

        dev = self._get_dev()
        self._friendly_name            = (dev.get("name", "") or dev.get("model", "")) if dev else device_id
        self._attr_unique_id           = f"{DOMAIN}:{device_id}:ring"
        self._attr_suggested_object_id = f"{slug}_ring"
        self._last_result: dict        = {}

    def _get_dev(self) -> dict | None:
        for d in _get_devices(self._coordinator):
            if d.get("device_id") == self._device_id:
                return d
        return None

    @property
    def device_info(self) -> dict:
        dev   = self._get_dev() or {}
        model = dev.get("model") or dev.get("name") or self._device_id
        return {
            "identifiers":  {(DOMAIN, self._device_id)},
            "name":         self._friendly_name or model,
            "manufacturer": "Xiaomi",
            "model":        model,
        }

    @property
    def extra_state_attributes(self) -> dict:
        dev = self._get_dev() or {}
        return {
            "device_name":         dev.get("name") or dev.get("model") or self._device_id,
            "device_id":           self._device_id,
            "last_command_result": self._last_result,
        }

    @property
    def should_poll(self) -> bool:
        return False

    async def async_added_to_hass(self):
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )

    async def async_press(self) -> None:
        data   = await async_ring(self.hass, self._endpoint, self._session_key, self._device_id)
        code   = data.get("code", -1)
        reason = data.get("reason", "")
        self._last_result = {"code": code, "reason": reason, "time": int(time.time())}

        if code == 0:
            _LOGGER.info("响铃已发送 device=...%s", self._device_id[-4:])
        elif code == 429:
            _LOGGER.info("冷却中 device=...%s reason=%s", self._device_id[-4:], reason)
        elif code == 990:
            _LOGGER.warning("需要重新登录/鉴权失败 device=...%s", self._device_id[-4:])
        else:
            _LOGGER.warning(
                "命令返回异常 code=%d reason=%s device=...%s",
                code, reason, self._device_id[-4:],
            )
