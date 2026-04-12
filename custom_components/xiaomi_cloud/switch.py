from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, COORDINATOR
from .button import async_ring

_LOGGER = logging.getLogger(__name__)


def _make_slug(raw_name: str, device_id: str) -> str:
    import re
    text = raw_name.lower().strip() if raw_name else ""
    text = re.sub(r'[^a-z0-9]+', '_', text)
    text = re.sub(r'_+', '_', text).strip('_')
    if not text:
        text = device_id[:6].lower() if len(device_id) >= 6 else device_id.lower()
    if not text.startswith("xiaomi_"):
        text = f"xiaomi_{text}"
    return text


def _create_find_switches(
    coordinator,
    entry: ConfigEntry,
    known_ids: set[str],
) -> list[XiaomiFindSwitch]:
    if not coordinator.data:
        return []

    code = coordinator.data.get("code", 0)
    reason = coordinator.data.get("reason", "")
    if reason in ["NO_SESSION", "LOGIN_IN_PROGRESS"] or code == 990:
        return []

    devices = coordinator.data.get("devices", [])
    entities = []

    for device in devices:
        device_id = device.get("device_id")
        if not device_id or device_id in known_ids:
            continue

        name = device.get("name", "") or device.get("model", "")
        if not name:
            continue

        slug = _make_slug(name, device_id)
        known_ids.add(device_id)
        entities.append(
            XiaomiFindSwitch(coordinator, entry, device_id, name, name, slug)
        )

    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR]
    known_ids: set[str] = set()

    entities = _create_find_switches(coordinator, entry, known_ids)
    if entities:
        async_add_entities(entities)
        return

    def _on_coordinator_update() -> None:
        new_entities = _create_find_switches(coordinator, entry, known_ids)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))


class XiaomiFindSwitch(CoordinatorEntity, SwitchEntity):

    _attr_icon = "mdi:bell-ring"
    _attr_is_on = False
    _attr_entity_registry_visible_default = False

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        model: str,
        slug: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = device_id
        self._device_name = device_name
        self._model = model
        self._endpoint = coordinator._endpoint
        self._session_key = coordinator._session_key
        self._attr_unique_id = f"{DOMAIN}:{device_id}:find_switch"
        self._attr_name = f"查找{model} 手机"
        if slug:
            self._attr_suggested_object_id = f"{slug}_find"

    @property
    def is_on(self) -> bool:
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        await async_ring(self.hass, self._endpoint, self._session_key, self._device_id)
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        pass
