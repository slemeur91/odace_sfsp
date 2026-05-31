"""Plateforme Switch : modèles Plug (prise) et Generic (commutateur).

Les deux modèles ont un comportement identique (on/off + anti-boucle sur
la réception de l'état confirmé). Ils se distinguent par leur device_class :

  plug    → SwitchDeviceClass.OUTLET   (prise de courant)
  generic → SwitchDeviceClass.SWITCH   (commutateur générique)

Un module Generic correspond typiquement à un actionneur Odace non typé
(relai, contacteur, etc.) qui expose un état binaire on/off.
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

# Modèles gérés par cette plateforme et leur device_class associé
_SWITCH_MODELS: Dict[str, SwitchDeviceClass] = {
    "plug":    SwitchDeviceClass.OUTLET,
    "generic": SwitchDeviceClass.SWITCH,
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
            model = dev.get("model")
            if model in _SWITCH_MODELS and uuid not in added:
                added.add(uuid)
                new.append(OdaceSFSPSwitch(coord, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DEVICES_CHANGED, _sync))


class OdaceSFSPSwitch(SwitchEntity):
    """Entité switch pour les modèles plug et generic."""

    _attr_should_poll = False

    # Correspondances pour les métadonnées HA selon le modèle
    _MODEL_HA_MODEL = {
        "plug":    "Odace SFSP Plug",
        "generic": "Odace SFSP Generic",
    }
    _MODEL_UNIQUE_PREFIX = {
        "plug":    "odace_sfsp_plug",
        "generic": "odace_sfsp_generic",
    }

    def __init__(self, coordinator: OdaceSFSPCoordinator, device: Dict[str, Any]) -> None:
        self._coord = coordinator
        self._uuid = device["uuid"].lower()
        model = device.get("model", "generic")

        self._attr_device_class = _SWITCH_MODELS.get(model, SwitchDeviceClass.SWITCH)
        self._attr_unique_id = f"{self._MODEL_UNIQUE_PREFIX.get(model, 'odace_sfsp_switch')}_{self._uuid}"
        self._attr_name = device.get("name") or f"{self._MODEL_HA_MODEL.get(model, 'Odace SFSP')} {self._uuid}"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Schneider Electric",
            model=self._MODEL_HA_MODEL.get(model, "Odace SFSP Generic"),
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
            _LOGGER.debug("Switch %s ON confirmé (no-op)", self._uuid)
        if not new_on and self._coord.was_commanded_recently(self._uuid, "off"):
            _LOGGER.debug("Switch %s OFF confirmé (no-op)", self._uuid)
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
