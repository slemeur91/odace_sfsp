"""Intégration Odace SFSP (Schneider) pour Home Assistant."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import (
    AC,
    CONF_ESPHOME_ENTRY_ID,
    CONF_ESPHOME_SERVICE,
    CONF_HCI,
    CONF_JEEDOM_KEY,
    CONF_MAC,
    CONF_MQTT_TOPIC,
    CONF_SEND_MODE,
    DEFAULT_ESPHOME_SERVICE,
    DEFAULT_HCI,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    MODEL_SCENE,
    SEND_MODE_HCI,
    SUPPORTED_MODELS,
)
from .coordinator import OdaceSFSPCoordinator

_LOGGER = logging.getLogger(__name__)

# FIX : Platform.SCENE ajouté pour le modèle "scene"
PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.EVENT,
    Platform.COVER,
    Platform.SWITCH,
    Platform.SCENE,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise une gateway Odace SFSP."""
    coordinator = OdaceSFSPCoordinator(hass, entry)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Enregistrement explicite du device gateway pour que les entités enfants
    # puissent y référencer leur via_device_id sans déclencher d'avertissement.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Schneider Electric",
        model="Odace SFSP Gateway",
        name=entry.title,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
# Les services sont enregistrés une seule fois pour le domaine et retrouvent la
# passerelle concernée à chaque appel : avec plusieurs passerelles, ils ne sont
# plus liés à la dernière configurée ni supprimés quand une seule est déchargée.

# 6 caractères hex pour un module, 8 pour une scène
_UUID = vol.All(cv.string, vol.Strip, vol.Lower, vol.Match(r"^[0-9a-f]{6}([0-9a-f]{2})?$"))

_SERVICE_SCHEMAS = {
    "send_command": vol.Schema(
        {
            vol.Required("uuid"): _UUID,
            vol.Required("ac"): vol.In(list(AC)),
            vol.Optional("options"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        }
    ),
    "start_learn": vol.Schema(
        {vol.Optional("timeout", default=60): vol.All(vol.Coerce(float), vol.Range(min=5, max=300))}
    ),
    "add_device": vol.Schema(
        {
            vol.Required("uuid"): _UUID,
            vol.Required("model"): vol.In(SUPPORTED_MODELS),
            vol.Optional("name"): cv.string,
            vol.Optional("mac", default=""): cv.string,
        }
    ),
    "remove_device": vol.Schema({vol.Required("uuid"): _UUID}),
    "bind_device": vol.Schema({vol.Required("uuid"): _UUID}),
}


def _coordinators(hass: HomeAssistant) -> list[OdaceSFSPCoordinator]:
    return list(hass.data.get(DOMAIN, {}).values())


def _coordinator_for(hass: HomeAssistant, uuid: str | None = None) -> OdaceSFSPCoordinator:
    """Passerelle qui possède ``uuid`` ; à défaut la première configurée."""
    coordinators = _coordinators(hass)
    if not coordinators:
        raise HomeAssistantError("Odace SFSP : aucune passerelle chargée")
    if uuid is not None:
        for coord in coordinators:
            if uuid in coord.devices:
                return coord
    return coordinators[0]


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "send_command"):
        return

    async def _svc_send_command(call: ServiceCall) -> None:
        await _coordinator_for(hass, call.data["uuid"]).async_send_command(
            call.data["uuid"],
            call.data["ac"],
            call.data.get("options"),
        )

    async def _svc_learn(call: ServiceCall) -> None:
        for coord in _coordinators(hass):
            coord.start_learn(call.data["timeout"])

    async def _svc_add_device(call: ServiceCall) -> None:
        uuid, model = call.data["uuid"], call.data["model"]
        if len(uuid) != (8 if model == MODEL_SCENE else 6):
            raise HomeAssistantError(
                "Odace SFSP : UUID de 6 caractères hexadécimaux attendu (8 pour une scène)"
            )
        await _coordinator_for(hass, uuid).async_add_device(
            {
                "uuid": uuid,
                "mac": call.data["mac"],
                "model": model,
                "name": call.data.get("name") or f"Odace SFSP {model} {uuid}",
            }
        )

    async def _svc_remove_device(call: ServiceCall) -> None:
        await _coordinator_for(hass, call.data["uuid"]).async_remove_device(call.data["uuid"])

    async def _svc_bind_device(call: ServiceCall) -> None:
        """Envoie la trame de pairing pour un périphérique déjà connu."""
        await _coordinator_for(hass, call.data["uuid"]).async_send_pair(call.data["uuid"])

    handlers = {
        "send_command": _svc_send_command,
        "start_learn": _svc_learn,
        "add_device": _svc_add_device,
        "remove_device": _svc_remove_device,
        "bind_device": _svc_bind_device,
    }
    for name, handler in handlers.items():
        hass.services.async_register(DOMAIN, name, handler, schema=_SERVICE_SCHEMAS[name])


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Ne recharger que si les paramètres réseau ont réellement changé.

    Les opérations sur les devices (ajout/modif/suppression) appellent
    async_update_entry pour persister CONF_DEVICES, ce qui déclencherait
    un rechargement inutile — et ferait disparaître les entités pendant
    la session de configuration.
    """
    coord: OdaceSFSPCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coord is None:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    # Mêmes valeurs par défaut que le coordinator : une clé absente de la
    # configuration ne doit pas être prise pour un changement.
    data = entry.data
    send_mode = data.get(CONF_SEND_MODE, SEND_MODE_HCI)
    if (
        send_mode != coord.send_mode
        or (send_mode == SEND_MODE_HCI and data.get(CONF_HCI, DEFAULT_HCI) != coord.hci_name)
        or data.get(CONF_MAC, "00:00:00:00:00:00") != coord.dongle_mac
        or data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC) != coord.mqtt_topic
        or data.get(CONF_JEEDOM_KEY, "") != coord.jeedom_key
        or data.get(CONF_ESPHOME_ENTRY_ID, "") != coord.esphome_entry_id
        or data.get(CONF_ESPHOME_SERVICE, DEFAULT_ESPHOME_SERVICE) != coord.esphome_service
    ):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: OdaceSFSPCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
        # Dernière passerelle déchargée : retirer les services du domaine
        if not hass.data[DOMAIN]:
            for name in _SERVICE_SCHEMAS:
                hass.services.async_remove(DOMAIN, name)
    return unload_ok
