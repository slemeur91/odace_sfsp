"""Config flow : sélection du mode d'envoi BLE (dongle HCI ou ESP32/MQTT).

Étapes pour une nouvelle installation :
  async_step_user        → Choix du mode (HCI ou ESP32/MQTT)
    ↓ HCI
  async_step_hci         → Sélection du dongle + clé Jeedom
    ↓ ESP32
  async_step_mqtt_broker → Saisie du topic MQTT
  async_step_mqtt_mac    → Découverte automatique MAC ESP32 (8 s) ou saisie manuelle

Découverte automatique de la MAC ESP32 :
  L'ESP32 publie sa MAC Bluetooth sur ``odace_sfsp/mac`` à la connexion MQTT.
  Le config flow souscrit à ce topic et attend 8 secondes.
  Si reçue → champ pré-rempli (modifiable). Si timeout → saisie manuelle.

Rétrocompatibilité :
  Les installations existantes (sans CONF_SEND_MODE) continuent de fonctionner
  en mode HCI grâce aux valeurs par défaut du coordinator.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Any, Dict

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components import mqtt as ha_mqtt
from homeassistant.const import CONF_MAC
from homeassistant.data_entry_flow import FlowResult

from .sender import read_controller_mac
from .const import (
    CONF_DEVICES,
    CONF_HCI,
    CONF_JEEDOM_KEY,
    CONF_MODEL,
    CONF_MQTT_TOPIC,
    CONF_NAME,
    CONF_SEND_MODE,
    CONF_UUID,
    DEFAULT_HCI,
    DEFAULT_MQTT_MAC_TOPIC,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    FORCE_DONGLE_MAC,
    FORCE_JEEDOM_KEY,
    KNOWN_DEVICES,
    MAC_DISCOVERY_TIMEOUT,
    SEND_MODE_HCI,
    SEND_MODE_MQTT,
    SUPPORTED_MODELS,
)

_LOGGER = logging.getLogger(__name__)
_MAC_RE  = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_UUID_RE = re.compile(r"^[0-9A-Fa-f]{6}$")

# Clé utilisée dans le formulaire pour indiquer le format de l'UUID saisi
_UUID_FORMAT_LOGS  = "logs"   # octets dans l'ordre BLE  (ex : 9c0300)
_UUID_FORMAT_LABEL = "label"  # octets inversés / étiquette (ex : 00039c)


def _is_valid_mac(mac: str) -> bool:
    return bool(_MAC_RE.match(mac.strip()))


def _is_valid_uuid(uuid: str) -> bool:
    """Valide que l'UUID est bien 6 caractères hexadécimaux (3 octets)."""
    return bool(_UUID_RE.match(uuid.strip()))


def _reverse_uuid(uuid_hex: str) -> str:
    """Inverse l'ordre des 3 octets d'un UUID 6-char hex. 9c0300 ↔ 00039c"""
    h = uuid_hex.lower()
    return h[4:6] + h[2:4] + h[0:2]


def _normalize_uuid(uuid_raw: str, fmt: str) -> str:
    """Retourne l'UUID en format logs (parser BLE), quel que soit le format saisi."""
    uuid = uuid_raw.strip().lower()
    if fmt == _UUID_FORMAT_LABEL:
        uuid = _reverse_uuid(uuid)
    return uuid


# ---------------------------------------------------------------------------
# Helpers partagés
# ---------------------------------------------------------------------------

def _get_adapter_address_sync(adapter_name: str, details: Any) -> str:
    """Extrait la MAC depuis un AdapterDetails HA — sans I/O bloquant."""
    if hasattr(details, "address"):
        address = details.address or ""
    elif isinstance(details, dict):
        address = details.get("address") or ""
    else:
        address = ""
    return address.upper() if address and address != "00:00:00:00:00:00" else ""


def _scan_sysfs_adapters() -> Dict[str, str]:
    """Énumère les adaptateurs HCI depuis sysfs (exécution synchrone via executor)."""
    import os
    result: Dict[str, str] = {}
    try:
        for name in sorted(os.listdir("/sys/class/bluetooth/")):
            if name.startswith("hci"):
                address = read_controller_mac(name) or "00:00:00:00:00:00"
                result[name] = f"{name} ({address})"
    except Exception:
        pass
    return result


async def _list_hci_adapters(hass) -> Dict[str, str]:
    """Retourne {adapter_name: 'name (MAC)'} pour tous les contrôleurs BLE."""
    adapters: Dict[str, str] = {}
    try:
        for adapter_name, details in bluetooth.async_get_adapters(hass).items():
            address = _get_adapter_address_sync(adapter_name, details)
            if not address and adapter_name.startswith("hci"):
                address = (
                    await hass.async_add_executor_job(read_controller_mac, adapter_name)
                    or "00:00:00:00:00:00"
                )
            adapters[adapter_name] = f"{adapter_name} ({address or '00:00:00:00:00:00'})"
    except Exception as err:
        _LOGGER.debug("Unable to list adapters: %s", err)
    if not adapters:
        adapters = await hass.async_add_executor_job(_scan_sysfs_adapters)
    if not adapters:
        adapters = {DEFAULT_HCI: f"{DEFAULT_HCI} (00:00:00:00:00:00)"}
    return adapters


async def _discover_esp32_mac(hass, mac_topic: str, timeout: int) -> str | None:
    """Souscrit au topic MQTT et attend la MAC Bluetooth de l'ESP32.

    L'ESP32 (ESPHome) publie sa MAC sur ce topic à la connexion MQTT.
    Retourne la MAC si reçue dans le délai, sinon None.
    """
    discovered: asyncio.Future = hass.loop.create_future()

    def _on_mac_message(msg) -> None:
        raw = msg.payload.strip() if isinstance(msg.payload, str) else msg.payload.decode().strip()
        if _is_valid_mac(raw) and not discovered.done():
            discovered.set_result(raw.upper())

    try:
        unsubscribe = await ha_mqtt.async_subscribe(hass, mac_topic, _on_mac_message)
        try:
            return await asyncio.wait_for(asyncio.shield(discovered), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            unsubscribe()
    except Exception as err:
        _LOGGER.debug("ESP32 MAC discovery failed: %s", err)
        return None


def _import_known_devices() -> Dict[str, Any]:
    return {
        uuid.lower(): {
            "uuid": uuid.lower(),
            "mac": info["mac"],
            "model": info["model"],
            "name": info["name"],
        }
        for uuid, info in KNOWN_DEVICES.items()
    }


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class OdaceSFSPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow Odace SFSP — mode HCI ou ESP32/MQTT."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._send_mode: str = SEND_MODE_HCI
        self._mqtt_topic: str = DEFAULT_MQTT_TOPIC
        self._esp32_mac: str = ""

    # ------------------------------------------------------------------
    # Étape 1 : choix du mode
    # ------------------------------------------------------------------
    async def async_step_user(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._send_mode = user_input[CONF_SEND_MODE]
            if self._send_mode == SEND_MODE_MQTT:
                return await self.async_step_mqtt_broker()
            return await self.async_step_hci()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SEND_MODE, default=SEND_MODE_HCI): vol.In(
                        {
                            SEND_MODE_HCI:  "Dongle Bluetooth local (HAOS, Proxmox, USB)",
                            SEND_MODE_MQTT: "ESP32 via MQTT (sans dongle USB sur HA)",
                        }
                    )
                }
            ),
        )

    # ------------------------------------------------------------------
    # Branche HCI
    # ------------------------------------------------------------------
    async def async_step_hci(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        adapters = await _list_hci_adapters(self.hass)
        default_key = FORCE_JEEDOM_KEY if FORCE_JEEDOM_KEY else secrets.token_hex(12)

        if FORCE_DONGLE_MAC:
            adapters_labels = {k: f"{k} ({FORCE_DONGLE_MAC})" for k in adapters}
        else:
            adapters_labels = adapters

        if user_input is not None:
            hci_name = user_input[CONF_HCI]
            mac: str | None = None
            try:
                ha_adapters = bluetooth.async_get_adapters(self.hass)
                if hci_name in ha_adapters:
                    mac = _get_adapter_address_sync(hci_name, ha_adapters[hci_name]) or None
            except Exception:
                pass
            if not mac:
                mac = await self.hass.async_add_executor_job(read_controller_mac, hci_name)
            mac = mac or "00:00:00:00:00:00"

            await self.async_set_unique_id(f"odace_sfsp-{mac}")
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Odace SFSP ({hci_name})",
                data={
                    CONF_SEND_MODE: SEND_MODE_HCI,
                    CONF_HCI: hci_name,
                    CONF_MAC: mac,
                    CONF_JEEDOM_KEY: user_input.get(CONF_JEEDOM_KEY) or secrets.token_hex(12),
                    CONF_DEVICES: _import_known_devices(),
                },
            )

        return self.async_show_form(
            step_id="hci",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HCI, default=DEFAULT_HCI): vol.In(adapters_labels),
                    vol.Optional(CONF_JEEDOM_KEY, default=default_key): str,
                }
            ),
            description_placeholders={
                "adapters": ", ".join(adapters_labels.values()),
                "default_key": default_key,
            },
        )

    # ------------------------------------------------------------------
    # Branche ESP32/MQTT — Étape 2a : topic
    # ------------------------------------------------------------------
    async def async_step_mqtt_broker(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        if not await ha_mqtt.async_wait_for_mqtt_client(self.hass):
            return self.async_abort(reason="mqtt_not_configured")

        if user_input is not None:
            self._mqtt_topic = user_input[CONF_MQTT_TOPIC].strip()
            return await self.async_step_mqtt_mac()

        return self.async_show_form(
            step_id="mqtt_broker",
            data_schema=vol.Schema(
                {vol.Required(CONF_MQTT_TOPIC, default=DEFAULT_MQTT_TOPIC): str}
            ),
            description_placeholders={
                "mac_topic": DEFAULT_MQTT_MAC_TOPIC,
                "timeout": str(MAC_DISCOVERY_TIMEOUT),
            },
        )

    # ------------------------------------------------------------------
    # Branche ESP32/MQTT — Étape 2b : découverte + saisie MAC ESP32
    # ------------------------------------------------------------------
    async def async_step_mqtt_mac(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        errors: Dict[str, str] = {}
        default_key = FORCE_JEEDOM_KEY if FORCE_JEEDOM_KEY else secrets.token_hex(12)

        if user_input is not None:
            esp32_mac = user_input[CONF_MAC].upper().strip()
            if not _is_valid_mac(esp32_mac):
                errors[CONF_MAC] = "invalid_mac"
            if not errors:
                await self.async_set_unique_id(f"odace_sfsp_esp32-{esp32_mac}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Odace SFSP ESP32 ({esp32_mac})",
                    data={
                        CONF_SEND_MODE: SEND_MODE_MQTT,
                        CONF_MAC: esp32_mac,
                        CONF_MQTT_TOPIC: self._mqtt_topic,
                        CONF_JEEDOM_KEY: user_input.get(CONF_JEEDOM_KEY) or secrets.token_hex(12),
                        CONF_DEVICES: _import_known_devices(),
                    },
                )

        # Tentative de découverte automatique de la MAC ESP32
        mac_topic = f"{self._mqtt_topic.rsplit('/', 1)[0]}/mac"
        _LOGGER.debug(
            "Découverte MAC ESP32 : souscription à %s (%ds)...",
            mac_topic, MAC_DISCOVERY_TIMEOUT,
        )
        discovered_mac = await _discover_esp32_mac(self.hass, mac_topic, MAC_DISCOVERY_TIMEOUT)
        if discovered_mac:
            _LOGGER.info("MAC ESP32 découverte automatiquement : %s", discovered_mac)
        else:
            _LOGGER.debug("Aucune réponse sur %s — saisie manuelle", mac_topic)
        self._esp32_mac = discovered_mac or ""

        return self.async_show_form(
            step_id="mqtt_mac",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MAC,
                        description={"suggested_value": self._esp32_mac or "AA:BB:CC:DD:EE:FF"},
                    ): str,
                    vol.Optional(CONF_JEEDOM_KEY, default=default_key): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "mac_found": discovered_mac or "",
                "mac_topic": mac_topic,
                "topic": self._mqtt_topic,
            },
        )

    @staticmethod
    def async_get_options_flow(
        entry: config_entries.ConfigEntry,
    ) -> "OdaceSFSPOptionsFlow":
        return OdaceSFSPOptionsFlow(entry)


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------

class OdaceSFSPOptionsFlow(config_entries.OptionsFlow):
    """Gestion des devices + configuration réseau + paramètres avancés."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        self._editing: str = ""

    # ------------------------------------------------------------------
    # Menu principal
    # ------------------------------------------------------------------
    async def async_step_init(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        send_mode = self.entry.data.get(CONF_SEND_MODE, SEND_MODE_HCI)
        network_label = (
            "Modifier la configuration ESP32/MQTT"
            if send_mode == SEND_MODE_MQTT
            else "Modifier le dongle Bluetooth"
        )
        if user_input is not None:
            action = user_input["action"]
            if action == "network":
                return await self.async_step_network()
            if action == "add":
                return await self.async_step_add()
            if action == "edit":
                return await self.async_step_select_edit()
            if action == "remove":
                return await self.async_step_select_remove()
            if action == "advanced":
                return await self.async_step_advanced()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="add"): vol.In(
                        {
                            "add":      "Ajouter un périphérique",
                            "edit":     "Modifier un périphérique",
                            "remove":   "Supprimer un périphérique",
                            "network":  network_label,
                            "advanced": "Paramètres avancés (clé Jeedom, MAC)",
                        }
                    )
                }
            ),
        )

    # ------------------------------------------------------------------
    # Configuration réseau (HCI ou ESP32/MQTT)
    # ------------------------------------------------------------------
    async def async_step_network(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        send_mode = self.entry.data.get(CONF_SEND_MODE, SEND_MODE_HCI)
        errors: Dict[str, str] = {}

        if send_mode == SEND_MODE_MQTT:
            current_mac = self.entry.data.get(CONF_MAC, "")
            current_topic = self.entry.data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC)
            if user_input is not None:
                esp32_mac = user_input[CONF_MAC].upper().strip()
                if not _is_valid_mac(esp32_mac):
                    errors[CONF_MAC] = "invalid_mac"
                if not errors:
                    self.hass.config_entries.async_update_entry(
                        self.entry,
                        data={
                            **self.entry.data,
                            CONF_MAC: esp32_mac,
                            CONF_MQTT_TOPIC: user_input[CONF_MQTT_TOPIC].strip(),
                        },
                    )
                    return self.async_create_entry(title="", data={})
            return self.async_show_form(
                step_id="network",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_MAC, default=current_mac): str,
                        vol.Required(CONF_MQTT_TOPIC, default=current_topic): str,
                    }
                ),
                errors=errors,
            )

        else:  # HCI
            adapters = await _list_hci_adapters(self.hass)
            current_hci = self.entry.data.get(CONF_HCI, DEFAULT_HCI)
            if user_input is not None:
                hci_name = user_input[CONF_HCI]
                mac = await self.hass.async_add_executor_job(read_controller_mac, hci_name)
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={
                        **self.entry.data,
                        CONF_HCI: hci_name,
                        CONF_MAC: mac or "00:00:00:00:00:00",
                    },
                )
                return self.async_create_entry(title="", data={})
            return self.async_show_form(
                step_id="network",
                data_schema=vol.Schema(
                    {vol.Required(CONF_HCI, default=current_hci): vol.In(adapters)}
                ),
            )

    # ------------------------------------------------------------------
    # Paramètres avancés : clé Jeedom + MAC contrôleur
    # ------------------------------------------------------------------
    async def async_step_advanced(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Permet de visualiser et modifier la clé Jeedom et la MAC du contrôleur.

        La clé Jeedom est utilisée pour le chiffrement CMAC des trames BLE.
        La MAC est celle du dongle HCI ou de l'ESP32 selon le mode.
        Modifier ces valeurs nécessite un re-pairing (bind_device) des périphériques.
        """
        current_key = self.entry.data.get(CONF_JEEDOM_KEY, "")
        current_mac = self.entry.data.get(CONF_MAC, "00:00:00:00:00:00")
        errors: Dict[str, str] = {}

        if user_input is not None:
            new_key = user_input.get(CONF_JEEDOM_KEY, "").strip()
            new_mac = user_input.get(CONF_MAC, "").strip()
            if new_mac and not _is_valid_mac(new_mac):
                errors[CONF_MAC] = "invalid_mac"
            if not errors:
                new_data = {**self.entry.data}
                if new_key:
                    new_data[CONF_JEEDOM_KEY] = new_key
                if new_mac:
                    new_data[CONF_MAC] = new_mac.upper()
                self.hass.config_entries.async_update_entry(self.entry, data=new_data)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_JEEDOM_KEY, default=current_key): str,
                    vol.Optional(CONF_MAC, default=current_mac): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "current_key": current_key or "(non définie)",
                "current_mac": current_mac,
            },
        )

    # ------------------------------------------------------------------
    # Ajout d'un périphérique (avec détection automatique depuis BLE)
    # ------------------------------------------------------------------
    async def async_step_add(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Ajout d'un périphérique.

        Si des trames de binding ont été reçues récemment (mode learn),
        les UUIDs détectés sont proposés en pré-remplissage.
        Le format de l'UUID peut être saisi :
          - Depuis les logs/Jeedom : octets dans l'ordre BLE (ex: 9c0300)
          - Depuis l'étiquette du module : octets inversés (ex: 00039c)
        L'intégration convertit automatiquement selon le format choisi.
        """
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        errors: Dict[str, str] = {}

        # Périphériques détectés récemment (pending bindings)
        pending = coord.get_pending_uuids()
        detected_choices: Dict[str, str] = {}
        for p in pending:
            label = (
                f"{p['uuid']} ({p['model']}, il y a {p['seconds_ago']}s)"
            )
            detected_choices[p["uuid"]] = label
        detected_choices["manual"] = "Saisir manuellement"

        if user_input is not None:
            # Résolution UUID selon format et source
            selected = user_input.get("detected_uuid", "manual")
            if selected != "manual":
                raw_uuid = selected
                uuid_fmt = _UUID_FORMAT_LOGS  # déjà en format logs
            else:
                raw_uuid = user_input.get(CONF_UUID, "").strip()
                uuid_fmt = user_input.get("uuid_format", _UUID_FORMAT_LOGS)

            uuid = _normalize_uuid(raw_uuid, uuid_fmt) if raw_uuid else ""

            if not uuid or not _is_valid_uuid(uuid):
                errors[CONF_UUID] = "invalid_uuid"
            elif uuid in coord.devices:
                errors["base"] = "already_exists"
            else:
                name = user_input.get(CONF_NAME, "").strip()
                model = user_input.get(CONF_MODEL, "dcl")
                if not name:
                    name = f"Odace SFSP {model} {uuid}"
                await coord.async_add_device(
                    {
                        CONF_UUID: uuid,
                        CONF_MAC: user_input.get(CONF_MAC, ""),
                        CONF_MODEL: model,
                        CONF_NAME: name,
                    }
                )
                return self.async_create_entry(title="", data={})

        # Pré-remplir l'UUID si un seul périphérique est en attente
        default_uuid = pending[0]["uuid"] if len(pending) == 1 else ""
        default_model = pending[0]["model"] if len(pending) == 1 else "dcl"

        schema_fields: Dict[Any, Any] = {}
        if detected_choices:
            default_detected = pending[0]["uuid"] if len(pending) == 1 else "manual"
            schema_fields[vol.Required("detected_uuid", default=default_detected)] = vol.In(detected_choices)

        schema_fields.update(
            {
                vol.Required("uuid_format", default=_UUID_FORMAT_LOGS): vol.In(
                    {
                        _UUID_FORMAT_LOGS:  "Format logs/Jeedom (ex : 9c0300)",
                        _UUID_FORMAT_LABEL: "Format étiquette module (octets inversés, ex : 00039c)",
                    }
                ),
                vol.Optional(CONF_UUID, default=default_uuid): str,
                vol.Optional(CONF_NAME, default=""): str,
                vol.Required(CONF_MODEL, default=default_model): vol.In(SUPPORTED_MODELS),
                vol.Optional(CONF_MAC, default=""): str,
            }
        )

        # Préparer les placeholders pour les UUIDs en attente
        pending_info = (
            ", ".join(f"{p['uuid']} ({p['model']})" for p in pending)
            if pending
            else "aucun (appuyer sur le bouton de binding puis appeler start_learn)"
        )
        reversed_hint = (
            f"{_reverse_uuid(pending[0]['uuid'])}" if len(pending) == 1
            else "—"
        )

        return self.async_show_form(
            step_id="add",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "pending": pending_info,
                "reversed_hint": reversed_hint,
            },
        )

    # ------------------------------------------------------------------
    # Édition d'un périphérique
    # ------------------------------------------------------------------
    async def async_step_select_edit(self, user_input=None) -> FlowResult:
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        if user_input is not None:
            self._editing = user_input["uuid"]
            return await self.async_step_edit()
        choices = {
            uid: f"{d.get('name', '?')} [{d.get('model', '?')}]"
            for uid, d in coord.devices.items()
        }
        return self.async_show_form(
            step_id="select_edit",
            data_schema=vol.Schema({vol.Required("uuid"): vol.In(choices)}),
        )

    async def async_step_edit(self, user_input=None) -> FlowResult:
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        current = coord.devices[self._editing]
        errors: Dict[str, str] = {}

        if user_input is not None:
            raw_uuid = user_input.get(CONF_UUID, self._editing).strip()
            uuid_fmt = user_input.get("uuid_format", _UUID_FORMAT_LOGS)
            new_uuid = _normalize_uuid(raw_uuid, uuid_fmt)

            if not _is_valid_uuid(new_uuid):
                errors[CONF_UUID] = "invalid_uuid"
            else:
                updates = {
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_MODEL: user_input[CONF_MODEL],
                    CONF_MAC: user_input.get(CONF_MAC, ""),
                }
                if new_uuid != self._editing:
                    await coord.async_remove_device(self._editing)
                    await coord.async_add_device({CONF_UUID: new_uuid, **updates})
                else:
                    await coord.async_update_device(self._editing, updates)
                return self.async_create_entry(title="", data={})

        reversed_current = _reverse_uuid(self._editing)
        return self.async_show_form(
            step_id="edit",
            data_schema=vol.Schema(
                {
                    vol.Required("uuid_format", default=_UUID_FORMAT_LOGS): vol.In(
                        {
                            _UUID_FORMAT_LOGS:  "Format logs/Jeedom (ex : 9c0300)",
                            _UUID_FORMAT_LABEL: "Format étiquette module (octets inversés, ex : 00039c)",
                        }
                    ),
                    vol.Required(CONF_UUID, default=self._editing): str,
                    vol.Required(CONF_NAME, default=current.get("name", "")): str,
                    vol.Required(
                        CONF_MODEL, default=current.get("model", "dcl")
                    ): vol.In(SUPPORTED_MODELS),
                    vol.Optional(CONF_MAC, default=current.get("mac", "")): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "current_uuid": self._editing,
                "reversed_uuid": reversed_current,
            },
        )

    # ------------------------------------------------------------------
    # Suppression d'un périphérique
    # ------------------------------------------------------------------
    async def async_step_select_remove(self, user_input=None) -> FlowResult:
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        if user_input is not None:
            await coord.async_remove_device(user_input["uuid"])
            return self.async_create_entry(title="", data={})
        choices = {
            uid: f"{d.get('name', '?')} [{d.get('model', '?')}]"
            for uid, d in coord.devices.items()
        }
        return self.async_show_form(
            step_id="select_remove",
            data_schema=vol.Schema({vol.Required("uuid"): vol.In(choices)}),
        )
