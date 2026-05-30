"""Intégration Odace SFSP (Schneider) pour Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import OdaceSFSPCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.EVENT, Platform.COVER, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise une gateway Odace SFSP."""
    coordinator = OdaceSFSPCoordinator(hass, entry)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Enregistrement explicite du device gateway pour que les entités enfants
    # puissent y référencer leur via_device sans déclencher d'avertissement.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Schneider Electric",
        model="Odace SFSP Gateway",
        name=entry.title,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Services
    async def _svc_send_command(call: ServiceCall) -> None:
        await coordinator.async_send_command(
            call.data["uuid"],
            call.data["ac"],
            call.data.get("options"),
        )

    async def _svc_learn(call: ServiceCall) -> None:
        coordinator.start_learn(call.data.get("timeout", 60))

    async def _svc_add_device(call: ServiceCall) -> None:
        await coordinator.async_add_device(
            {
                "uuid": call.data["uuid"],
                "mac": call.data.get("mac", ""),
                "model": call.data["model"],
                "name": call.data.get("name", f"Odace SFSP {call.data['model']} {call.data['uuid']}"),
            }
        )

    async def _svc_remove_device(call: ServiceCall) -> None:
        await coordinator.async_remove_device(call.data["uuid"])

    async def _svc_bind_device(call: ServiceCall) -> None:
        """Envoie la trame de pairing pour un périphérique déjà connu."""
        await coordinator.async_send_pair(call.data["uuid"])

    hass.services.async_register(DOMAIN, "send_command", _svc_send_command)
    hass.services.async_register(DOMAIN, "start_learn", _svc_learn)
    hass.services.async_register(DOMAIN, "add_device", _svc_add_device)
    hass.services.async_register(DOMAIN, "remove_device", _svc_remove_device)
    hass.services.async_register(DOMAIN, "bind_device", _svc_bind_device)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: OdaceSFSPCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unload_ok
