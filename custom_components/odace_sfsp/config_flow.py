"""Config flow : découverte HCI, auto-MAC, et options (ajout/suppr. devices).

La première étape liste tous les adaptateurs Bluetooth connus par Home
Assistant (``bluetooth.async_get_scanner`` + ``hciconfig``). L'utilisateur
choisit son adaptateur (``hci0`` par défaut) et la MAC est récupérée
automatiquement. Les équipements historiques Jeedom (``KNOWN_DEVICES``) sont
pré-importés.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Dict

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_MAC
from homeassistant.data_entry_flow import FlowResult

from .sender import read_controller_mac
from .const import (
    CONF_DEVICES,
    CONF_HCI,
    CONF_JEEDOM_KEY,
    CONF_MODEL,
    CONF_NAME,
    CONF_UUID,
    DEFAULT_HCI,
    DOMAIN,
    KNOWN_DEVICES,
    SUPPORTED_MODELS,
    FORCE_JEEDOM_KEY,
    FORCE_DONGLE_MAC,
)

_LOGGER = logging.getLogger(__name__)


def _list_hci_adapters(hass) -> Dict[str, str]:
    """Retourne ``{hci_name: 'hciX (MAC)'}`` pour tous les contrôleurs connus."""
    adapters: Dict[str, str] = {}
    # HA >= 2024 : utiliser async_get_adapters
    try:
        for details in bluetooth.async_get_adapters(hass).values():
            name = details.get("hci") or details.get("adapter") or "hci0"
            address = details.get("address", "00:00:00:00:00:00")
            adapters[name] = f"{name} ({address})"
    except Exception as err:  # pragma: no cover
        _LOGGER.debug("Unable to list adapters via HA: %s", err)
    if not adapters:
        adapters = {DEFAULT_HCI: f"{DEFAULT_HCI} (00:00:00:00:00:00)"}
    return adapters


class OdaceSFSPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow initial : sélection du contrôleur BLE."""

    VERSION = 1

    async def async_step_user(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        adapters = _list_hci_adapters(self.hass)

        # Si FORCE_JEEDOM_KEY est défini, l'afficher à la place d'une clé générée
        default_jeedom_key = FORCE_JEEDOM_KEY if FORCE_JEEDOM_KEY else secrets.token_hex(12)

        if user_input is not None:
            hci_name = user_input[CONF_HCI]
            mac = read_controller_mac(hci_name)
            if not mac:
                # Essaye de la récupérer depuis la description renvoyée par HA
                try:
                    for details in bluetooth.async_get_adapters(self.hass).values():
                        if (details.get("hci") or "") == hci_name:
                            mac = details.get("address")
                            break
                except Exception:  # pragma: no cover
                    pass

            mac = mac or "00:00:00:00:00:00"

            await self.async_set_unique_id(f"odace_sfsp-{mac}")
            self._abort_if_unique_id_configured()

            # Import automatique des appareils Jeedom connus
            devices = {
                uuid.lower(): {
                    "uuid": uuid.lower(),
                    "mac": info["mac"],
                    "model": info["model"],
                    "name": info["name"],
                }
                for uuid, info in KNOWN_DEVICES.items()
            }

            return self.async_create_entry(
                title=f"Odace SFSP ({hci_name})",
                data={
                    CONF_HCI: hci_name,
                    CONF_MAC: mac,
                    CONF_JEEDOM_KEY: user_input.get(CONF_JEEDOM_KEY) or secrets.token_hex(12),
                    CONF_DEVICES: devices,
                },
            )

        # Si FORCE_DONGLE_MAC est défini, l'afficher à la place de la MAC système
        # dans le sélecteur ET dans la description
        if FORCE_DONGLE_MAC:
            adapters_labels = {k: f"{k} ({FORCE_DONGLE_MAC})" for k in adapters.keys()}
        else:
            adapters_labels = adapters

        schema = vol.Schema(
            {
                vol.Required(CONF_HCI, default=DEFAULT_HCI): vol.In(adapters_labels),
                vol.Optional(CONF_JEEDOM_KEY, default=default_jeedom_key): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            description_placeholders={
                "adapters": ", ".join(adapters_labels.values()),
                "default_key": default_jeedom_key,
            },
        )

    @staticmethod
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> "OdaceSFSPOptionsFlow":
        return OdaceSFSPOptionsFlow(entry)


class OdaceSFSPOptionsFlow(config_entries.OptionsFlow):
    """Ajout / modification / suppression manuelle de devices."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            action = user_input["action"]
            if action == "add":
                return await self.async_step_add()
            if action == "edit":
                return await self.async_step_select_edit()
            if action == "remove":
                return await self.async_step_select_remove()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="add"): vol.In(
                        {"add": "Ajouter un périphérique", "edit": "Modifier", "remove": "Supprimer"}
                    )
                }
            ),
        )

    # ---- Ajout ----
    async def async_step_add(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        errors = {}
        if user_input is not None:
            coord = self.hass.data[DOMAIN][self.entry.entry_id]
            uuid = user_input[CONF_UUID].lower()
            if uuid in coord.devices:
                errors["base"] = "already_exists"
            else:
                await coord.async_add_device(
                    {
                        CONF_UUID: uuid,
                        CONF_MAC: user_input.get(CONF_MAC, ""),
                        CONF_MODEL: user_input[CONF_MODEL],
                        CONF_NAME: user_input[CONF_NAME],
                    }
                )
                return self.async_create_entry(title="", data={})
        schema = vol.Schema(
            {
                vol.Required(CONF_UUID): str,
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_MODEL, default="dcl"): vol.In(SUPPORTED_MODELS),
                vol.Optional(CONF_MAC, default=""): str,
            }
        )
        return self.async_show_form(step_id="add", data_schema=schema, errors=errors)

    # ---- Edition ----
    async def async_step_select_edit(self, user_input=None) -> FlowResult:
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        if user_input is not None:
            self._editing = user_input["uuid"]
            return await self.async_step_edit()
        choices = {uuid: f"{d.get('name','?')} [{d.get('model','?')}]" for uuid, d in coord.devices.items()}
        return self.async_show_form(
            step_id="select_edit",
            data_schema=vol.Schema({vol.Required("uuid"): vol.In(choices)}),
        )

    async def async_step_edit(self, user_input=None) -> FlowResult:
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        current = coord.devices[self._editing]
        if user_input is not None:
            await coord.async_update_device(self._editing, user_input)
            return self.async_create_entry(title="", data={})
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=current.get("name", "")): str,
                vol.Required(CONF_MODEL, default=current.get("model", "dcl")): vol.In(SUPPORTED_MODELS),
                vol.Optional(CONF_MAC, default=current.get("mac", "")): str,
            }
        )
        return self.async_show_form(step_id="edit", data_schema=schema)

    # ---- Suppression ----
    async def async_step_select_remove(self, user_input=None) -> FlowResult:
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        if user_input is not None:
            await coord.async_remove_device(user_input["uuid"])
            return self.async_create_entry(title="", data={})
        choices = {uuid: f"{d.get('name','?')} [{d.get('model','?')}]" for uuid, d in coord.devices.items()}
        return self.async_show_form(
            step_id="select_remove",
            data_schema=vol.Schema({vol.Required("uuid"): vol.In(choices)}),
        )
