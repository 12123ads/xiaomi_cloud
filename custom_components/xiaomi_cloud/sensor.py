"""传感器平台 — 地址 + 电量，数据来自后端 /sync 协议。"""
import logging
from collections import Counter

from homeassistant.core import callback
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, COORDINATOR

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]
    entry_id    = config_entry.entry_id

    # 服务状态传感器立即创建，不依赖设备数据
    async_add_entities([ServiceStatusSensor(coordinator, entry_id)], False)

    def _create_sensors(devs):
        return _build_sensors(coordinator, devs, config_entry)

    devices_data = _get_devices(coordinator)
    if devices_data:
        sensors = _create_sensors(devices_data)
        _LOGGER.info("创建 %d 个传感器", len(sensors))
        async_add_entities(sensors, False)
        hass.data[DOMAIN][entry_id]["sensor_entities_created"] = True
        return

    _LOGGER.debug("数据未就绪，注册监听器等待设备数据")

    @callback
    def _on_data_available():
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
        if entry_data is None:
            return
        if entry_data.get("sensor_entities_created"):
            return
        devs = _get_devices(coordinator)
        if not devs:
            return
        entry_data["sensor_entities_created"] = True
        sensors = _create_sensors(devs)
        _LOGGER.info("创建 %d 个传感器", len(sensors))
        async_add_entities(sensors, False)

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


def _build_sensors(coordinator, devices_data: list, config_entry) -> list:
    sensors = []
    bases   = _compute_bases(devices_data)
    for dev in devices_data:
        device_id = dev.get("device_id")
        if not device_id:
            continue
        raw_name = dev.get("name", "") or dev.get("model", "") or device_id
        slug     = _make_slug(raw_name, device_id, bases)
        sensors.append(DeviceAddressSensor(
            coordinator, device_id, slug, raw_name, config_entry.entry_id,
        ))
        sensors.append(DeviceBatterySensor(
            coordinator, device_id, slug, raw_name, config_entry.entry_id,
        ))
    return sensors


class ServiceStatusSensor(CoordinatorEntity, SensorEntity):
    """服务登录状态传感器，始终 available，直接读 coordinator.data。"""

    _attr_has_entity_name = True
    _attr_icon            = "mdi:cloud-sync"

    def __init__(self, coordinator, entry_id: str):
        super().__init__(coordinator)
        self._entry_id                   = entry_id
        self._attr_unique_id             = f"{DOMAIN}:{entry_id}:status"
        self._attr_suggested_object_id   = "xiaomi_cloud_status"
        self._attr_name                  = "服务状态"

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._entry_id)},
            "name":         "Xiaomi Cloud",
            "manufacturer": "Xiaomi",
            "model":        "Cloud Service",
        }

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        data = self.coordinator.data
        if not data:
            return "初始化中"
        reason  = data.get("reason") or ""
        code    = data.get("code", -1)
        if reason == "NO_SESSION":
            return "等待登录"
        if reason == "LOGIN_IN_PROGRESS":
            return "登录中"
        if code == 990:
            return "需要认证"
        if data.get("need_reauth"):
            return "认证过期"
        if code == 0:
            devices = data.get("devices") or []
            return f"正常（{len(devices)}台设备）" if devices else "正常"
        return f"异常（code={code}）"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if not data:
            return {"hint": "后台正在登录小米账号，请耐心等待30-60秒"}
        code    = data.get("code", -1)
        reason  = data.get("reason") or ""
        devices = data.get("devices") or []
        attrs: dict = {"code": code, "device_count": len(devices)}
        if reason:
            attrs["reason"] = reason
        if reason in ("NO_SESSION", "LOGIN_IN_PROGRESS") or code == 990:
            attrs["hint"] = "后台正在登录小米账号，请耐心等待30-60秒"
        return attrs


class DeviceAddressSensor(SensorEntity):

    _attr_has_entity_name = True
    _attr_icon = "mdi:map-marker-radius"

    def __init__(
        self,
        coordinator,
        device_id: str,
        slug: str,
        friendly_name: str,
        entry_id: str,
    ):
        self._coordinator                = coordinator
        self._device_id                  = device_id
        self._entry_id                   = entry_id
        self._attr_name                  = "地址"
        self._attr_unique_id             = f"{DOMAIN}:{device_id}:address"
        self._attr_suggested_object_id   = f"{slug}_address"
        self._friendly_name              = friendly_name

    def _get_dev(self) -> dict | None:
        for d in _get_devices(self._coordinator):
            if d.get("device_id") == self._device_id:
                return d
        return None

    @property
    def native_value(self) -> str | None:
        dev = self._get_dev()
        if not dev:
            return None
        return dev.get("address") or None

    @property
    def extra_state_attributes(self) -> dict:
        dev   = self._get_dev() or {}
        attrs = {
            "fix_time":          dev.get("fix_time"),
            "coord_type":        dev.get("coord_type"),
            "address_component": dev.get("address_component", ""),
            "stale":             dev.get("stale", False),
        }
        if not dev.get("address"):
            attrs["reason"] = "NO_ADDRESS"
        return attrs

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
    def should_poll(self) -> bool:
        return False

    async def async_added_to_hass(self):
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )


class DeviceBatterySensor(SensorEntity):

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"

    def __init__(
        self,
        coordinator,
        device_id: str,
        slug: str,
        friendly_name: str,
        entry_id: str,
    ):
        self._coordinator                = coordinator
        self._device_id                  = device_id
        self._entry_id                   = entry_id
        self._friendly_name              = friendly_name
        self._attr_name                  = "电量"
        self._attr_unique_id             = f"{DOMAIN}:{device_id}:battery"
        self._attr_suggested_object_id   = f"{slug}_battery"

    def _get_dev(self) -> dict | None:
        for d in _get_devices(self._coordinator):
            if d.get("device_id") == self._device_id:
                return d
        return None

    @property
    def native_value(self) -> int | None:
        dev = self._get_dev()
        return dev.get("battery") if dev else None

    @property
    def icon(self) -> str:
        val = self.native_value
        if val is None:  return "mdi:battery-unknown"
        if val <= 10:    return "mdi:battery-10"
        if val <= 20:    return "mdi:battery-20"
        if val <= 30:    return "mdi:battery-30"
        if val <= 40:    return "mdi:battery-40"
        if val <= 50:    return "mdi:battery-50"
        if val <= 60:    return "mdi:battery-60"
        if val <= 70:    return "mdi:battery-70"
        if val <= 80:    return "mdi:battery-80"
        if val <= 90:    return "mdi:battery-90"
        return "mdi:battery"

    @property
    def extra_state_attributes(self) -> dict:
        dev = self._get_dev() or {}
        return {
            "online": dev.get("online"),
            "stale":  dev.get("stale", False),
        }

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
    def should_poll(self) -> bool:
        return False

    async def async_added_to_hass(self):
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
