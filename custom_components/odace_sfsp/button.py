"""Bouton 'Désappairer et supprimer' pour les modules Odace SFSP commandables.

Ce bouton n'est créé que pour les modèles qui ont un mécanisme de pairing BLE
(dcl, shutter, plug, dimmer, generic). Les switchs sont exclus car ils n'ont
pas de mécanisme de pairing nécessitant un désappariage explicite.

Flow :
  1. L'utilisateur clique le bouton dans HA
  2. Une notification persistante explique la procédure
  3. Le coordinateur entre en mode désappariage (60 s)
  4. L'utilisateur appuie sur le bouton physique du module
  5. La trame binding est reçue → trame pair envoyée (toggle → désappairé)
  6. Le device est retiré de HA
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from homeassistant.components.button import ButtonEntity
from homeassistant.components.persistent_notification import async_create
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_DEVICES_CHANGED
from .coordinator import OdaceSFSPCoordinator

_LOGGER = logging.getLogger(__name__)

# Modèles commandables ayant un mécanisme de pairing BLE
_COMMANDABLE_MODELS = {"dcl", "shutter", "plug", "dimmer", "generic"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure les boutons de désappariage pour les modules commandables."""
    coordinator: OdaceSFSPCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_uuids: set[str] = set()

    def _sync_buttons() -> None:
        new_buttons = [
            OdaceSFSPUnpairButton(coordinator, uuid, device)
            for uuid, device in coordinator.devices.items()
            if uuid not in known_uuids
            and device.get("model") in _COMMANDABLE_MODELS
        ]
        if new_buttons:
            for btn in new_buttons:
                known_uuids.add(btn._uuid)
            async_add_entities(new_buttons)

    _sync_buttons()

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICES_CHANGED, _sync_buttons)
    )


class OdaceSFSPUnpairButton(ButtonEntity):
    """Bouton 'Désappairer et supprimer' pour un module commandable Odace SFSP."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:link-off"
    _attr_translation_key = "unpair"

    def __init__(
        self,
        coordinator: OdaceSFSPCoordinator,
        uuid: str,
        device: Dict[str, Any],
    ) -> None:
        self._coordinator = coordinator
        self._uuid = uuid.lower()
        self._attr_unique_id = f"{uuid}_unpair"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, uuid.lower())},
        }

    async def async_press(self) -> None:
        """Active le mode désappariage et affiche les instructions à l'utilisateur."""
        await self._coordinator.start_unpair(self._uuid)
        async_create(
            self.hass,
            (
                f"Le module **{self._uuid}** est en attente de désappariage.\n\n"
                "Appuyez sur le **bouton physique** du module dans les **60 secondes** "
                "pour confirmer. Le module sera désappairé puis automatiquement retiré "
                "de Home Assistant.\n\n"
                "_Si le bouton physique n'est pas accessible, effectuez un reset long "
                "(10 s) sur le module pour effacer tous ses appairages, puis supprimez "
                "le device manuellement dans HA._"
            ),
            title="Odace SFSP — Désappairer et supprimer",
            notification_id=f"odace_sfsp_unpair_{self._uuid}",
        )
        _LOGGER.info(
            "Odace SFSP — bouton 'Désappairer et supprimer' pressé pour %s", self._uuid
        )
