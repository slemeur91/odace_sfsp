"""Plateforme Cover : modèle Shutter (volet roulant Odace SFSP).

Supporte : ouverture, fermeture, arrêt et positionnement (0 % = fermé,
100 % = ouvert) conformément à la convention Home Assistant.

Le positionnement utilise la commande ``goto`` avec ``options = 100 - position``
(même logique que Jeedom : options représente le pourcentage de fermeture,
donc 0 = ouvert et 100 = fermé).
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
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
            if dev.get("model") == "shutter" and uuid not in added:
                added.add(uuid)
                new.append(OdaceSFSPCover(coord, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DEVICES_CHANGED, _sync))


class OdaceSFSPCover(CoverEntity):
    _attr_should_poll = False
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: OdaceSFSPCoordinator, device: Dict[str, Any]) -> None:
        self._coord = coordinator
        self._uuid = device["uuid"].lower()
        self._attr_unique_id = f"odace_sfsp_shutter_{self._uuid}"
        self._attr_name = device.get("name") or f"Odace SFSP Shutter {self._uuid}"
        self._attr_current_cover_position: int | None = None  # 0=fermé, 100=ouvert
        self._attr_is_closed: bool | None = None
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Schneider Electric",
            model="Odace SFSP Shutter",
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
        label = data.get("label", "")
        position = data.get("value")

        if label == "Ouvert":
            self._attr_current_cover_position = 100
            self._attr_is_closed = False
            self._attr_is_opening = False
            self._attr_is_closing = False
        elif label == "Fermé":
            self._attr_current_cover_position = 0
            self._attr_is_closed = True
            self._attr_is_opening = False
            self._attr_is_closing = False
        elif label == "Ouverture":
            self._attr_is_opening = True
            self._attr_is_closing = False
            if position is not None:
                self._attr_current_cover_position = int(position)
                self._attr_is_closed = False
        elif label == "Fermeture":
            self._attr_is_closing = True
            self._attr_is_opening = False
            if position is not None:
                self._attr_current_cover_position = int(position)
        elif label == "Arrêté":
            self._attr_is_opening = False
            self._attr_is_closing = False
            if position is not None:
                pos = int(position)
                self._attr_current_cover_position = pos
                self._attr_is_closed = (pos == 0)

        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._coord.async_send_command(self._uuid, "up")
        self._attr_is_opening = True
        self._attr_is_closing = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._coord.async_send_command(self._uuid, "down")
        self._attr_is_closing = True
        self._attr_is_opening = False
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._coord.async_send_command(self._uuid, "stop")
        self._attr_is_opening = False
        self._attr_is_closing = False
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Positionne le volet.

        HA transmet ``position`` entre 0 (fermé) et 100 (ouvert).
        La trame Beagle utilise ``options = 100 - position`` (pourcentage de
        fermeture), donc 0 = ouvert et 100 = fermé.
        """
        position = kwargs.get("position", 0)
        options = 100 - int(position)
        await self._coord.async_send_command(self._uuid, "goto", options=options)
        self._attr_current_cover_position = int(position)
        self._attr_is_closed = (position == 0)
        self.async_write_ha_state()
