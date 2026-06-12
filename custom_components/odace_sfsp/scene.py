"""Plateforme Scene : modèle Scene (scène virtuelle Odace SFSP).

Dans le protocole Beagle, une scène est activée avec :
  - AC toujours égal à ``"on"`` (0x01) — cf. scene.json logicalId="ac:on"
  - Param = ``"FC"`` (scène custom) ou ``"FD"`` (scène Schneider),
    dérivé de ``device["type"]`` dans build_frame via le dict SCENES

Le champ ``"type"`` du dict device doit valoir ``"custom"`` (défaut) ou
``"schneider"``. Ce champ est stocké dans coord.devices[uuid]["type"] et
transmis à craft_payload pour que build_frame puisse construire le bon Param.

Cette logique est identique à Jeedom :
  - beagle.class.php  → allowDevice() envoie device['type']
  - sendadv.py        → build_frame() lit device['type'] → SCENES[type]
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_DEVICES_CHANGED
from .coordinator import OdaceSFSPCoordinator

_LOGGER = logging.getLogger(__name__)

# Valeurs acceptées pour device["type"] — correspondent aux clés de SCENES dans const.py
_SCENE_TYPE_CUSTOM    = "custom"
_SCENE_TYPE_SCHNEIDER = "schneider"
_SCENE_TYPE_DEFAULT   = _SCENE_TYPE_CUSTOM


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: OdaceSFSPCoordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[str] = set()

    @callback
    def _sync() -> None:
        new = []
        for uuid, dev in coord.devices.items():
            if dev.get("model") == "scene" and uuid not in added:
                added.add(uuid)
                new.append(OdaceSFSPScene(coord, dev))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DEVICES_CHANGED, _sync))


class OdaceSFSPScene(Scene):
    """Entité scène pour les devices de modèle 'scene'.

    Champ obligatoire dans le dict device :
      ``type`` : "custom" (défaut) ou "schneider"
              → détermine le Param BLE (FC ou FD) dans build_frame

    Activation : envoie ac="on" au coordinator.
    Le coordinator passe device["type"] à craft_payload → build_frame
    dérive automatiquement le bon SCENES[type].
    """

    _attr_icon = "mdi:palette"

    def __init__(self, coordinator: OdaceSFSPCoordinator, device: Dict[str, Any]) -> None:
        self._coord = coordinator
        self._uuid = device["uuid"].lower()
        self._attr_unique_id = f"odace_sfsp_scene_{self._uuid}"
        self._attr_name = device.get("name") or f"Odace SFSP Scene {self._uuid}"

        # Validation du type : "custom" ou "schneider" uniquement
        scene_type = device.get("type", _SCENE_TYPE_DEFAULT)
        if scene_type not in (_SCENE_TYPE_CUSTOM, _SCENE_TYPE_SCHNEIDER):
            _LOGGER.warning(
                "type de scène invalide '%s' pour %s — utilisation de '%s'",
                scene_type, self._uuid, _SCENE_TYPE_DEFAULT,
            )
        # (le type est lu directement depuis coord.devices[uuid]["type"] lors de l'envoi)

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Schneider Electric",
            model="Odace SFSP Scene",
            via_device=(DOMAIN, coordinator.entry.entry_id),
        )

    async def async_activate(self, **kwargs: Any) -> None:
        """Active la scène via BLE.

        AC = "on" (0x01) — identique à Jeedom (logicalId="ac:on" dans scene.json).
        Le type custom/schneider est géré par build_frame via device["type"].
        """
        _LOGGER.info("Odace SFSP SCENE activate uuid=%s", self._uuid)
        await self._coord.async_send_command(self._uuid, "on")
