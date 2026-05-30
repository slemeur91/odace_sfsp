"""Plateforme Switch : modèle Plug (prise Odace SFSP).

Mêmes comportements que la plateforme Light (DCL) : on/off avec anti-boucle
sur la réception de l'état confirmé. Le device_class OUTLET reflète l'usage
prise de courant.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_DEVICES_CHANGED, SIGNAL_DEVICE_UPDATE
from .coordinator import OdaceSFSPCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: OdaceSFSPCoordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[str] = set()

    @callback
    def _sync() -> None:
        new = []
        for uuid, dev in coord.devices.items():
            if dev.get("model") == "plug" and uuid not in added:
                added.add(uuid)
                new.append(OdaceSFSPPlug(coord, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DEVICES_CHANGED, _sync))


class OdaceSFSPPlug(SwitchEntity):
    _attr_should_poll = False
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(self, coordinator: OdaceSFSPCoordinator, device: Dict[str, Any]) -> None:
        self._coord = coordinator
        self._uuid = device["uuid"].lower()
        self._attr_unique_id = f"odace_sfsp_plug_{self._uuid}"
        self._attr_name = device.get("name") or f"Odace SFSP Plug {self._uuid}"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Schneider Electric",
            model="Odace SFSP Plug",
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
        value = data.get("value")
        if value is None:
            return
        new_on = value == "1"
        if new_on and self._coord.was_commanded_recently(self._uuid, "on"):
            _LOGGER.debug("Plug %s ON confirmé (no-op)", self._uuid)
        if not new_on and self._coord.was_commanded_recently(self._uuid, "off"):
            _LOGGER.debug("Plug %s OFF confirmé (no-op)", self._uuid)
        self._attr_is_on = new_on
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coord.async_send_command(self._uuid, "on")
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coord.async_send_command(self._uuid, "off")
        self._attr_is_on = False
        self.async_write_ha_state()
