"""设备追踪器平台 — 读取后端 /sync 协议数据。"""
import logging
from collections import Counter

from homeassistant.core import callback
from homeassistant.components.device_tracker.config_entry import SourceType, TrackerEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    COORDINATOR,
    CONF_ENABLE_GAODE_MORE_INFO,
    DEFAULT_ENABLE_GAODE_MORE_INFO,
)

_LOGGER = logging.getLogger(__name__)

# 模块级标记：gaode_maps 缺失警告只打一次
_gaode_warning_logged = False


def _get_devices(coordinator) -> list:
    if not coordinator.data or not isinstance(coordinator.data, dict):
        return []
    return coordinator.data.get("devices") or []


def _compute_bases(devs: list) -> dict:
    """统计每个型号 slug 出现次数，用于判断是否需要加后缀。"""
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
    """生成 xiaomi_ 前缀的 slug，重复型号追加设备 ID 后4位。"""
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


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]
    entry_id    = config_entry.entry_id

    def _create_trackers(devs):
        bases = _compute_bases(devs)
        return [
            XiaomiDeviceTracker(
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
        entities = _create_trackers(devices_data)
        _LOGGER.info("创建 %d 个设备追踪器", len(entities))
        async_add_entities(entities, False)
        hass.data[DOMAIN][entry_id]["device_tracker_entities_created"] = True
        return

    _LOGGER.debug("数据未就绪，注册监听器等待设备数据")

    @callback
    def _on_data_available():
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
        if entry_data is None:
            return
        if entry_data.get("device_tracker_entities_created"):
            return
        devs = _get_devices(coordinator)
        if not devs:
            return
        entry_data["device_tracker_entities_created"] = True
        entities = _create_trackers(devs)
        _LOGGER.info("创建 %d 个设备追踪器", len(entities))
        async_add_entities(entities, False)

    config_entry.async_on_unload(
        coordinator.async_add_listener(_on_data_available)
    )


class XiaomiDeviceTracker(TrackerEntity, RestoreEntity):

    _attr_has_entity_name = True
    _attr_name = None  # 实体友好名直接用设备名（如 "Redmi K40S"）

    def __init__(self, coordinator, device_id: str, slug: str, config_entry):
        self.coordinator    = coordinator
        self._device_id     = device_id
        self._entry         = config_entry
        self._attr_unique_id             = f"{DOMAIN}:{device_id}"
        self._attr_suggested_object_id   = slug
        self._slug          = slug

        # 缓存上次有效值（断线时保持最后位置）
        self._last_lat      = None
        self._last_lon      = None
        self._last_accuracy = None

        dev = self._get_dev()
        self._friendly_name = (dev.get("name", "") or dev.get("model", "")) if dev else device_id

    def _get_dev(self) -> dict | None:
        for d in _get_devices(self.coordinator):
            if d.get("device_id") == self._device_id:
                return d
        return None

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        dev = self._get_dev()
        if not dev:
            return self._last_lat
        lat = dev.get("latitude")
        if lat is not None:
            self._last_lat = lat
        return self._last_lat

    @property
    def longitude(self) -> float | None:
        dev = self._get_dev()
        if not dev:
            return self._last_lon
        lon = dev.get("longitude")
        if lon is not None:
            self._last_lon = lon
        return self._last_lon

    @property
    def location_accuracy(self) -> int:
        dev = self._get_dev()
        if not dev:
            return self._last_accuracy or 0
        acc = dev.get("accuracy")
        if acc is not None:
            self._last_accuracy = int(acc)
        return self._last_accuracy or 0

    @property
    def battery_level(self) -> int | None:
        dev = self._get_dev()
        return dev.get("battery") if dev else None

    @property
    def extra_state_attributes(self) -> dict:
        global _gaode_warning_logged

        dev   = self._get_dev() or {}
        attrs = {
            "device_id":      self._device_id,
            "friendly_name":  self._friendly_name,
            "fix_time":       dev.get("fix_time"),
            "locate_time":    dev.get("locate_time") or dev.get("ts"),
            "coord_type":     dev.get("coord_type"),
            "online":         dev.get("online"),
            "stale":          dev.get("stale", False),
            "stale_reason":   dev.get("stale_reason", ""),
            "address":        dev.get("address", ""),
            "movement_speed_kmh": dev.get("movement_speed_kmh"),
            "polling_interval_minutes": dev.get("polling_interval_minutes"),
            "wgs84_latitude":  dev.get("latitude"),
            "wgs84_longitude": dev.get("longitude"),
        }

        gcj02_lat = dev.get("gcj02_lat")
        gcj02_lon = dev.get("gcj02_lon")
        if gcj02_lat is not None and gcj02_lon is not None:
            attrs["gaode_latitude"]  = gcj02_lat
            attrs["gaode_longitude"] = gcj02_lon

        if dev.get("raw_coord_type"):
            attrs["raw_coord_type"] = dev.get("raw_coord_type")
            attrs["raw_lat"]        = dev.get("raw_lat")
            attrs["raw_lon"]        = dev.get("raw_lon")

        enable_gaode = self._entry.options.get(
            CONF_ENABLE_GAODE_MORE_INFO,
            self._entry.data.get(CONF_ENABLE_GAODE_MORE_INFO, DEFAULT_ENABLE_GAODE_MORE_INFO),
        )
        if enable_gaode:
            # 无论是否安装都写 custom_ui_more_info，让 HA 前端尝试调用
            attrs["custom_ui_more_info"] = "gaode-map"

            gaode_installed = "gaode_maps" in self.hass.config.components
            if gaode_installed:
                attrs["gaode_maps_status"] = "ok"
            else:
                attrs["gaode_maps_status"] = "missing"
                attrs["gaode_maps_hint"]   = "请在 HACS 安装 gaode_maps 并重启"
                if not _gaode_warning_logged:
                    _gaode_warning_logged = True
                    _LOGGER.warning(
                        "已启用高德地图 more-info，但未检测到 gaode_maps 组件，"
                        "请在 HACS 安装 gaode_maps 后重启 HA"
                    )

        return attrs

    @property
    def icon(self) -> str:
        return "mdi:map-marker"

    @property
    def should_poll(self) -> bool:
        return False

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

    async def async_added_to_hass(self):
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    async def async_update(self):
        await self.coordinator.async_request_refresh()
