"""Plateforme Event : modèle Switch (réception seule, déclenche un trigger).

Les switches Odace ne peuvent pas être commandés : ils émettent un évènement
``toggle``/``on``/``off``/... La plateforme ``event`` de HA est parfaitement
adaptée — chaque appui déclenche un évènement daté que les automatisations
peuvent utiliser comme trigger.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_DEVICES_CHANGED, SIGNAL_DEVICE_UPDATE
from .coordinator import OdaceSFSPCoordinator

_LOGGER = logging.getLogger(__name__)

EVENT_TYPES = [
    "off", "on", "toggle", "dim_up", "dim_down",
    "up", "down", "stop", "scene_user", "scene_in", "scene_out",
]

VALUE_TO_EVENT = {
    "0": "off", "1": "on", "2": "toggle",
    "3": "dim_up", "4": "dim_down",
    "5": "up", "6": "down", "7": "stop",
    "8": "scene_user", "9": "scene_in", "10": "scene_out",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: OdaceSFSPCoordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[str] = set()

    @callback
    def _sync() -> None:
        new = []
        for uuid, dev in coord.devices.items():
            if dev.get("model") == "switch" and uuid not in added:
                added.add(uuid)
                new.append(OdaceSFSPSwitchEvent(coord, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DEVICES_CHANGED, _sync))


class OdaceSFSPSwitchEvent(EventEntity):
    _attr_should_poll = False
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: OdaceSFSPCoordinator, device: Dict[str, Any]) -> None:
        self._coord = coordinator
        self._uuid = device["uuid"].lower()
        self._attr_unique_id = f"odace_sfsp_switch_{self._uuid}"
        self._attr_name = device.get("name") or f"Odace SFSP Switch {self._uuid}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Schneider Electric",
            model="Odace SFSP Switch",
            via_device=(DOMAIN, coordinator.entry.entry_id),
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DEVICE_UPDATE.format(uuid=self._uuid),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self, result: Dict[str, Any]) -> None:
        data = result.get("data", {})
        if data.get("type") != "advertisement":
            return
        value = str(data.get("value", ""))
        event_name = VALUE_TO_EVENT.get(value)
        if not event_name:
            return
        self._trigger_event(event_name, {"label": data.get("label"), "firmware": data.get("firmware")})
        self.async_write_ha_state()
