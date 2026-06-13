"""Config flow : sélection du mode d'envoi BLE.

Étapes pour une nouvelle installation :
  async_step_user    → Choix du mode (HCI / ESPHome API)
    ↓ HCI
  async_step_hci     → Sélection du dongle + clé Jeedom
    ↓ ESPHome API
  async_step_esphome → Sélection du device ESPHome (dropdown avec MAC BT) + nom du service

Changements v2 :
  - Mode ESP32/MQTT supprimé du choix initial (async_step_user).
    Les installations existantes en mode MQTT continuent de fonctionner ;
    leur écran "réseau" dans les options est conservé pour modification.
  - Écran ESPHome : champ MAC supprimé — la MAC est détectée automatiquement
    depuis runtime_data de la config entry ESPHome et stockée sans exposition
    dans l'interface.

Rétrocompatibilité :
  Les installations existantes (sans CONF_SEND_MODE) continuent de fonctionner
  en mode HCI grâce aux valeurs par défaut du coordinator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from typing import Any, Dict

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.components import mqtt as ha_mqtt
from homeassistant.const import CONF_MAC
from homeassistant.data_entry_flow import FlowResult

from .sender import read_controller_mac
from .const import (
    CONF_DEVICES,
    CONF_ESPHOME_ENTRY_ID,
    CONF_ESPHOME_SERVICE,
    CONF_HCI,
    CONF_JEEDOM_KEY,
    CONF_MODEL,
    CONF_MQTT_TOPIC,
    CONF_NAME,
    CONF_SEND_MODE,
    CONF_UUID,
    DEFAULT_ESPHOME_SERVICE,
    DEFAULT_HCI,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    SEND_MODE_ESPHOME_API,
    SEND_MODE_HCI,
    SEND_MODE_MQTT,
    SIGNAL_DEVICES_CHANGED,
    SUPPORTED_MODELS,
)

_LOGGER = logging.getLogger(__name__)
_MAC_RE  = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_UUID_RE = re.compile(r"^[0-9A-Fa-f]{6}$")

_UUID_FORMAT_LOGS  = "logs"
_UUID_FORMAT_LABEL = "label"


def _is_valid_mac(mac: str) -> bool:
    return bool(_MAC_RE.match(mac.strip()))


def _is_valid_uuid(uuid: str) -> bool:
    return bool(_UUID_RE.match(uuid.strip()))


def _reverse_uuid(uuid_hex: str) -> str:
    h = uuid_hex.lower()
    return h[4:6] + h[2:4] + h[0:2]


def _normalize_uuid(uuid_raw: str, fmt: str) -> str:
    uuid = uuid_raw.strip().lower()
    if fmt == _UUID_FORMAT_LABEL:
        uuid = _reverse_uuid(uuid)
    return uuid


# ---------------------------------------------------------------------------
# Helpers partagés
# ---------------------------------------------------------------------------

def _scan_sysfs_adapters() -> Dict[str, str]:
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


def _guess_esphome_bt_mac(hass, entry_id: str) -> str:
    """Détecte la MAC Bluetooth de l'ESP32 via config_entry.runtime_data (HA 2024+)."""
    try:
        esphome_entry = hass.config_entries.async_get_entry(entry_id)
        if esphome_entry is None:
            return ""
        runtime_data = getattr(esphome_entry, "runtime_data", None)
        if runtime_data is None:
            return ""
        device_info = getattr(runtime_data, "device_info", None)
        if device_info is None:
            return ""

        bt_mac = getattr(device_info, "bluetooth_mac_address", None) or ""
        if bt_mac and bt_mac not in ("", "00:00:00:00:00:00"):
            return bt_mac.upper()

        wifi_mac = getattr(device_info, "mac_address", None) or ""
        if wifi_mac and wifi_mac not in ("", "00:00:00:00:00:00"):
            mac_int = int(wifi_mac.replace(":", ""), 16)
            bt_int  = mac_int + 2
            return ":".join(f"{(bt_int >> (8 * i)) & 0xFF:02X}" for i in range(5, -1, -1))
    except Exception as err:
        _LOGGER.debug("ESPHome BT MAC exception : %s", err)
    return ""


def _list_esphome_entries(hass) -> Dict[str, str]:
    return {e.entry_id: e.title for e in hass.config_entries.async_entries("esphome")}


def _list_esphome_services(hass, entry_id: str) -> Dict[str, str]:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return {}
    prefix = entry.title.lower().replace(" ", "_").replace("-", "_") + "_"
    services: Dict[str, str] = {}
    for svc in hass.services.async_services().get("esphome", {}):
        if svc.startswith(prefix):
            short = svc[len(prefix):]
            services[short] = short
    return services


async def _list_hci_adapters(hass) -> Dict[str, str]:
    adapters = await hass.async_add_executor_job(_scan_sysfs_adapters)
    if not adapters:
        adapters = {DEFAULT_HCI: f"{DEFAULT_HCI} (00:00:00:00:00:00)"}
    return adapters


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class OdaceSFSPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow Odace SFSP — mode HCI ou ESPHome API."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._send_mode: str = SEND_MODE_HCI

    # ------------------------------------------------------------------
    # Étape 1 : choix du mode
    # v2 : SEND_MODE_MQTT retiré — seuls HCI et ESPHome API sont proposés
    # ------------------------------------------------------------------
    async def async_step_user(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._send_mode = user_input[CONF_SEND_MODE]
            if self._send_mode == SEND_MODE_ESPHOME_API:
                return await self.async_step_esphome()
            return await self.async_step_hci()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SEND_MODE, default=SEND_MODE_HCI): vol.In(
                        {
                            SEND_MODE_HCI:         "Dongle Bluetooth local (HAOS, Proxmox, USB)",
                            SEND_MODE_ESPHOME_API: "ESP32 BLE Proxy via API native ESPHome",
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
        adapters_labels = adapters

        if user_input is not None:
            hci_name = user_input[CONF_HCI]
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
                    CONF_JEEDOM_KEY: secrets.token_hex(12),
                    CONF_DEVICES: {},
                },
            )

        return self.async_show_form(
            step_id="hci",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HCI, default=DEFAULT_HCI): vol.In(adapters_labels),
                }
            ),
            description_placeholders={
                "adapters": ", ".join(adapters_labels.values()),
            },
        )

    # ------------------------------------------------------------------
    # Branche ESPHome API
    # v2 : champ MAC supprimé — détection automatique silencieuse uniquement
    # ------------------------------------------------------------------
    async def async_step_esphome(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Sélection du device ESPHome + nom du service custom BLE.

        La MAC Bluetooth est détectée automatiquement depuis runtime_data
        et affichée dans le label du dropdown pour identification visuelle.
        Elle est stockée en config sans être exposée dans le formulaire.
        """
        errors: Dict[str, str] = {}
        esphome_entries = _list_esphome_entries(self.hass)
        if not esphome_entries:
            return self.async_abort(reason="no_esphome_device")

        esphome_labels: Dict[str, str] = {}
        esphome_macs:   Dict[str, str] = {}
        for eid, title in esphome_entries.items():
            mac = _guess_esphome_bt_mac(self.hass, eid)
            esphome_macs[eid] = mac
            esphome_labels[eid] = f"{title} — BT: {mac}" if mac else f"{title} — BT: inconnue"

        default_entry = next(iter(esphome_entries))

        if user_input is not None:
            entry_id = user_input[CONF_ESPHOME_ENTRY_ID]
            service  = user_input.get(CONF_ESPHOME_SERVICE, DEFAULT_ESPHOME_SERVICE).strip()
            # MAC auto-détectée — pas de saisie manuelle en v2
            esp32_mac = esphome_macs.get(entry_id, "")

            if not service:
                errors[CONF_ESPHOME_SERVICE] = "invalid_service"

            if not errors:
                entry_title = esphome_entries.get(entry_id, entry_id)
                await self.async_set_unique_id(f"odace_sfsp_esphome-{entry_id}")
                self._abort_if_unique_id_configured()
                if not esp32_mac:
                    _LOGGER.warning(
                        "MAC BT ESP32 introuvable pour %s — le pairing sera impossible. "
                        "Consulter les logs ESPHome : 'Bluetooth controller initialized, address XX:XX:XX:XX:XX:XX'",
                        entry_title,
                    )
                return self.async_create_entry(
                    title=f"Odace SFSP ({entry_title})",
                    data={
                        CONF_SEND_MODE:        SEND_MODE_ESPHOME_API,
                        CONF_ESPHOME_ENTRY_ID: entry_id,
                        CONF_ESPHOME_SERVICE:  service,
                        CONF_MAC:              esp32_mac,
                        CONF_JEEDOM_KEY:       secrets.token_hex(12),
                        CONF_DEVICES:          {},
                    },
                )

        available_services = _list_esphome_services(self.hass, default_entry)
        schema_fields: Dict[Any, Any] = {
            vol.Required(CONF_ESPHOME_ENTRY_ID, default=default_entry): vol.In(esphome_labels),
        }
        if available_services:
            default_svc = (
                DEFAULT_ESPHOME_SERVICE if DEFAULT_ESPHOME_SERVICE in available_services
                else next(iter(available_services))
            )
            schema_fields[vol.Required(CONF_ESPHOME_SERVICE, default=default_svc)] = vol.In(available_services)
        else:
            schema_fields[vol.Required(CONF_ESPHOME_SERVICE, default=DEFAULT_ESPHOME_SERVICE)] = str

        return self.async_show_form(
            step_id="esphome",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "service_name": DEFAULT_ESPHOME_SERVICE,
            },
        )

    @staticmethod
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> "OdaceSFSPOptionsFlow":
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
        if send_mode == SEND_MODE_MQTT:
            network_label = "Modifier la configuration ESP32/MQTT"
        elif send_mode == SEND_MODE_ESPHOME_API:
            network_label = "Modifier la configuration ESPHome API"
        else:
            network_label = "Modifier le dongle Bluetooth"

        if user_input is not None:
            action = user_input["action"]
            if action == "network":  return await self.async_step_network()
            if action == "add":      return await self.async_step_add()
            if action == "edit":     return await self.async_step_select_edit()
            if action == "remove":   return await self.async_step_select_remove()
            if action == "advanced": return await self.async_step_advanced()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="add"): vol.In(
                        {
                            "add":      "Ajouter un module",
                            "edit":     "Modifier un module",
                            "remove":   "Supprimer un module",
                            "network":  network_label,
                            "advanced": "Paramètres avancés",
                        }
                    )
                }
            ),
        )

    # ------------------------------------------------------------------
    # Configuration réseau
    # ------------------------------------------------------------------
    async def async_step_network(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        send_mode = self.entry.data.get(CONF_SEND_MODE, SEND_MODE_HCI)
        errors: Dict[str, str] = {}

        # Branche MQTT conservée pour les installations existantes uniquement.
        # Aucune nouvelle installation MQTT n'est possible depuis le config flow v2.
        if send_mode == SEND_MODE_MQTT:
            current_mac   = self.entry.data.get(CONF_MAC, "")
            current_topic = self.entry.data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC)
            if user_input is not None:
                esp32_mac = user_input[CONF_MAC].upper().strip()
                if not _is_valid_mac(esp32_mac):
                    errors[CONF_MAC] = "invalid_mac"
                if not errors:
                    self.hass.config_entries.async_update_entry(
                        self.entry,
                        data={**self.entry.data, CONF_MAC: esp32_mac, CONF_MQTT_TOPIC: user_input[CONF_MQTT_TOPIC].strip()},
                    )
                    return self.async_create_entry(title="", data={})
            return self.async_show_form(
                step_id="network",
                data_schema=vol.Schema(
                    {vol.Required(CONF_MAC, default=current_mac): str, vol.Required(CONF_MQTT_TOPIC, default=current_topic): str}
                ),
                errors=errors,
            )

        elif send_mode == SEND_MODE_ESPHOME_API:
            esphome_entries = _list_esphome_entries(self.hass)
            if not esphome_entries:
                return self.async_abort(reason="no_esphome_device")
            current_entry_id = self.entry.data.get(CONF_ESPHOME_ENTRY_ID, "")
            current_service  = self.entry.data.get(CONF_ESPHOME_SERVICE, DEFAULT_ESPHOME_SERVICE)
            default_entry    = current_entry_id if current_entry_id in esphome_entries else next(iter(esphome_entries))

            esphome_labels: Dict[str, str] = {}
            esphome_macs:   Dict[str, str] = {}
            for eid, title in esphome_entries.items():
                mac = _guess_esphome_bt_mac(self.hass, eid)
                esphome_macs[eid] = mac
                esphome_labels[eid] = f"{title} — BT: {mac}" if mac else f"{title} — BT: inconnue"

            available_services = _list_esphome_services(self.hass, default_entry)

            if user_input is not None:
                entry_id  = user_input[CONF_ESPHOME_ENTRY_ID]
                service   = user_input.get(CONF_ESPHOME_SERVICE, "").strip()
                # MAC auto-détectée — pas de saisie manuelle en v2
                esp32_mac = esphome_macs.get(entry_id, self.entry.data.get(CONF_MAC, ""))
                if not service:
                    errors[CONF_ESPHOME_SERVICE] = "invalid_service"
                if not errors:
                    self.hass.config_entries.async_update_entry(
                        self.entry,
                        data={
                            **self.entry.data,
                            CONF_ESPHOME_ENTRY_ID: entry_id,
                            CONF_ESPHOME_SERVICE:  service,
                            CONF_MAC:              esp32_mac,
                        },
                    )
                    return self.async_create_entry(title="", data={})

            network_schema: Dict[Any, Any] = {
                vol.Required(CONF_ESPHOME_ENTRY_ID, default=default_entry): vol.In(esphome_labels),
            }
            if available_services:
                default_svc = current_service if current_service in available_services else next(iter(available_services))
                network_schema[vol.Required(CONF_ESPHOME_SERVICE, default=default_svc)] = vol.In(available_services)
            else:
                network_schema[vol.Required(CONF_ESPHOME_SERVICE, default=current_service)] = str

            return self.async_show_form(
                step_id="network",
                data_schema=vol.Schema(network_schema),
                errors=errors,
            )

        else:  # HCI
            adapters    = await _list_hci_adapters(self.hass)
            current_hci = self.entry.data.get(CONF_HCI, DEFAULT_HCI)
            if user_input is not None:
                hci_name = user_input[CONF_HCI]
                mac = await self.hass.async_add_executor_job(read_controller_mac, hci_name)
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={**self.entry.data, CONF_HCI: hci_name, CONF_MAC: mac or "00:00:00:00:00:00"},
                )
                return self.async_create_entry(title="", data={})
            return self.async_show_form(
                step_id="network",
                data_schema=vol.Schema({vol.Required(CONF_HCI, default=current_hci): vol.In(adapters)}),
            )

    # ------------------------------------------------------------------
    # Paramètres avancés : menu avec sous-étapes
    # ------------------------------------------------------------------
    async def async_step_advanced(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu des paramètres avancés."""
        if user_input is not None:
            action = user_input["action"]
            if action == "settings":      return await self.async_step_advanced_settings()
            if action == "import_export": return await self.async_step_import_export()

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="settings"): vol.In(
                        {
                            "settings":      "Modifier la clé Jeedom / MAC contrôleur",
                            "import_export": "Exporter / Importer les modules",
                        }
                    )
                }
            ),
        )

    async def async_step_advanced_settings(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Clé Jeedom + MAC contrôleur."""
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
            step_id="advanced_settings",
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
    # Export / Import des périphériques
    # ------------------------------------------------------------------
    async def async_step_import_export(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Exporte les périphériques actuels en JSON et permet d'en importer.

        Export : le champ ``export_json`` est pré-rempli avec le JSON des
        devices actuels. L'utilisateur peut le copier.

        Import : coller un JSON dans ``import_json``.
        Les UUIDs déjà présents sont ignorés ; seuls les nouveaux sont ajoutés.
        Format attendu :
          {
            "UUID": {"uuid": "UUID", "mac": "XX:XX", "model": "dcl", "name": "..."},
            ...
          }
        """
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        errors: Dict[str, str] = {}

        # Construit le JSON d'export — une entrée par ligne, sans accolades externes
        # Format : "UUID": {"uuid": "...", "mac": "...", "model": "...", "name": "..."},
        export_lines = []
        for uid, d in sorted(coord.devices.items()):
            entry = {
                "uuid":  d.get("uuid", uid),
                "mac":   d.get("mac", ""),
                "model": d.get("model", ""),
                "name":  d.get("name", ""),
            }
            export_lines.append(
                f'"{uid}": {json.dumps(entry, ensure_ascii=False)},'
            )
        export_json = "\n".join(export_lines)

        if user_input is not None:
            import_raw = (user_input.get("import_json") or "").strip()

            if not import_raw:
                # Rien à importer — sortie normale
                return self.async_create_entry(title="", data={})

            # Validation et import
            # Accepte deux formats :
            #   1. JSON complet : {"uuid": {...}, ...}
            #   2. Lignes sans accolades : "uuid": {...},\n"uuid2": {...},
            def _parse_import(raw: str) -> Dict[str, Any]:
                raw = raw.strip()
                # Format 1 : JSON dict complet
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        return obj
                    raise ValueError("pas un dict")
                except json.JSONDecodeError:
                    pass
                # Format 2 : lignes "uuid": {...},  — on enveloppe dans {}
                # Supprimer la virgule finale éventuelle, puis wrapper
                cleaned = raw.rstrip().rstrip(",")
                try:
                    obj = json.loads("{" + cleaned + "}")
                    if isinstance(obj, dict):
                        return obj
                    raise ValueError("pas un dict")
                except json.JSONDecodeError as exc:
                    raise ValueError(f"JSON non parsable : {exc}") from exc

            try:
                imported: Dict[str, Any] = _parse_import(import_raw)
            except ValueError as err:
                errors["import_json"] = "invalid_json"
                _LOGGER.debug("Import JSON invalide : %s", err)
            else:
                to_add: list[Dict[str, Any]] = []
                skipped = 0
                invalid: list[str] = []

                for key, entry in imported.items():
                    if not isinstance(entry, dict):
                        invalid.append(key)
                        continue
                    uuid  = (entry.get("uuid") or key).strip().lower()
                    model = entry.get("model", "").strip()
                    name  = entry.get("name", "").strip()
                    mac   = entry.get("mac", "").strip()

                    if not _is_valid_uuid(uuid):
                        invalid.append(key)
                        continue
                    if model not in SUPPORTED_MODELS:
                        invalid.append(key)
                        continue
                    if not name:
                        name = f"Odace SFSP {model} {uuid}"

                    if uuid in coord.devices:
                        skipped += 1
                        continue

                    to_add.append({"uuid": uuid, "mac": mac, "model": model, "name": name})

                # Ajout en masse : une seule persistance + un seul signal
                for device in to_add:
                    coord.devices[device["uuid"]] = {**device}
                if to_add:
                    new_data = {**self.entry.data, CONF_DEVICES: coord.devices}
                    self.hass.config_entries.async_update_entry(self.entry, data=new_data)
                    async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)

                if invalid:
                    _LOGGER.warning(
                        "Import : %d entrée(s) ignorée(s) (uuid/model invalide) : %s",
                        len(invalid), invalid,
                    )
                _LOGGER.info(
                    "Import périphériques : %d ajouté(s), %d ignoré(s) (déjà présents), "
                    "%d invalide(s)",
                    len(to_add), skipped, len(invalid),
                )
                return self.async_create_entry(title="", data={})

        _textarea = TextSelector(TextSelectorConfig(multiline=True))
        return self.async_show_form(
            step_id="import_export",
            data_schema=vol.Schema(
                {
                    vol.Optional("export_json", default=export_json): _textarea,
                    vol.Optional("import_json", default=""): _textarea,
                }
            ),
            errors=errors,
            description_placeholders={
                "device_count": str(len(coord.devices)),
            },
        )

    # ------------------------------------------------------------------
    # Ajout d'un périphérique
    # ------------------------------------------------------------------
    async def async_step_add(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        errors: Dict[str, str] = {}

        pending = coord.get_pending_uuids()
        detected_choices: Dict[str, str] = {
            p["uuid"]: f"{p['uuid']} ({p['model']}, il y a {p['seconds_ago']}s)"
            for p in pending
        }
        detected_choices["manual"] = "Saisir manuellement"

        if user_input is not None:
            selected = user_input.get("detected_uuid", "manual")
            if selected != "manual":
                raw_uuid = selected
                uuid_fmt = _UUID_FORMAT_LOGS
            else:
                raw_uuid = user_input.get(CONF_UUID, "").strip()
                uuid_fmt = user_input.get("uuid_format", _UUID_FORMAT_LOGS)

            uuid = _normalize_uuid(raw_uuid, uuid_fmt) if raw_uuid else ""

            if not uuid or not _is_valid_uuid(uuid):
                errors[CONF_UUID] = "invalid_uuid"
            elif uuid in coord.devices:
                errors["base"] = "already_exists"
            else:
                name  = user_input.get(CONF_NAME, "").strip()
                model = user_input.get(CONF_MODEL, "dcl")
                if not name:
                    name = f"Odace SFSP {model} {uuid}"
                await coord.async_add_device(
                    {CONF_UUID: uuid, CONF_MAC: user_input.get(CONF_MAC, ""), CONF_MODEL: model, CONF_NAME: name}
                )
                return self.async_create_entry(title="", data={})

        default_uuid  = pending[0]["uuid"]  if len(pending) == 1 else ""
        default_model = pending[0]["model"] if len(pending) == 1 else "dcl"

        schema_fields: Dict[Any, Any] = {}
        if detected_choices:
            default_detected = pending[0]["uuid"] if len(pending) == 1 else "manual"
            schema_fields[vol.Required("detected_uuid", default=default_detected)] = vol.In(detected_choices)

        schema_fields.update(
            {
                vol.Required("uuid_format", default=_UUID_FORMAT_LOGS): vol.In(
                    {_UUID_FORMAT_LOGS: "Format logs/Jeedom (ex : 9c0300)", _UUID_FORMAT_LABEL: "Format étiquette module (ex : 00039c)"}
                ),
                vol.Optional(CONF_UUID, default=default_uuid): str,
                vol.Optional(CONF_NAME, default=""): str,
                vol.Required(CONF_MODEL, default=default_model): vol.In(SUPPORTED_MODELS),
                vol.Optional(CONF_MAC, default=""): str,
            }
        )

        pending_info  = (
            ", ".join(f"{p['uuid']} ({p['model']})" for p in pending)
            if pending
            else "aucun (appuyer sur le bouton de binding puis appeler start_learn)"
        )
        reversed_hint = _reverse_uuid(pending[0]["uuid"]) if len(pending) == 1 else "—"

        return self.async_show_form(
            step_id="add",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={"pending": pending_info, "reversed_hint": reversed_hint},
        )

    # ------------------------------------------------------------------
    # Édition d'un périphérique
    # ------------------------------------------------------------------
    async def async_step_select_edit(self, user_input=None) -> FlowResult:
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        if user_input is not None:
            self._editing = user_input["uuid"]
            return await self.async_step_edit()
        choices = {uid: f"{d.get('name','?')} [{d.get('model','?')}]" for uid, d in coord.devices.items()}
        return self.async_show_form(
            step_id="select_edit",
            data_schema=vol.Schema({vol.Required("uuid"): vol.In(choices)}),
        )

    async def async_step_edit(self, user_input=None) -> FlowResult:
        coord   = self.hass.data[DOMAIN][self.entry.entry_id]
        current = coord.devices[self._editing]
        errors: Dict[str, str] = {}

        if user_input is not None:
            raw_uuid = user_input.get(CONF_UUID, self._editing).strip()
            uuid_fmt = user_input.get("uuid_format", _UUID_FORMAT_LOGS)
            new_uuid = _normalize_uuid(raw_uuid, uuid_fmt)

            if not _is_valid_uuid(new_uuid):
                errors[CONF_UUID] = "invalid_uuid"
            else:
                updates = {CONF_NAME: user_input[CONF_NAME], CONF_MODEL: user_input[CONF_MODEL], CONF_MAC: user_input.get(CONF_MAC, "")}
                model_changed = user_input[CONF_MODEL] != current.get("model")
                uuid_changed  = new_uuid != self._editing
                if uuid_changed or model_changed:
                    # UUID ou modèle changé : suppression complète (efface toutes les
                    # entités liées à l'ancien modèle) puis recréation propre.
                    await coord.async_remove_device(self._editing)
                    await coord.async_add_device({CONF_UUID: new_uuid, **updates})
                    if model_changed and not uuid_changed:
                        # Même UUID, modèle différent : les sets 'added' des plateformes
                        # contiennent encore l'ancien UUID → _sync() skiperait la recréation.
                        # Un rechargement complet remet ces sets à zéro et recrée les
                        # entités du nouveau modèle depuis les données déjà persistées.
                        self.hass.async_create_task(
                            self.hass.config_entries.async_reload(self.entry.entry_id)
                        )
                else:
                    await coord.async_update_device(self._editing, updates)
                return self.async_create_entry(title="", data={})

        reversed_current = _reverse_uuid(self._editing)
        return self.async_show_form(
            step_id="edit",
            data_schema=vol.Schema(
                {
                    vol.Required("uuid_format", default=_UUID_FORMAT_LOGS): vol.In(
                        {_UUID_FORMAT_LOGS: "Format logs/Jeedom (ex : 9c0300)", _UUID_FORMAT_LABEL: "Format étiquette module (ex : 00039c)"}
                    ),
                    vol.Required(CONF_UUID, default=self._editing): str,
                    vol.Required(CONF_NAME, default=current.get("name", "")): str,
                    vol.Required(CONF_MODEL, default=current.get("model", "dcl")): vol.In(SUPPORTED_MODELS),
                    vol.Optional(CONF_MAC, default=current.get("mac", "")): str,
                }
            ),
            errors=errors,
            description_placeholders={"current_uuid": self._editing, "reversed_uuid": reversed_current},
        )

    # ------------------------------------------------------------------
    # Suppression d'un périphérique
    # ------------------------------------------------------------------
    async def async_step_select_remove(self, user_input=None) -> FlowResult:
        coord = self.hass.data[DOMAIN][self.entry.entry_id]
        if user_input is not None:
            await coord.async_remove_device(user_input["uuid"])
            return self.async_create_entry(title="", data={})
        choices = {uid: f"{d.get('name','?')} [{d.get('model','?')}]" for uid, d in coord.devices.items()}
        return self.async_show_form(
            step_id="select_remove",
            data_schema=vol.Schema({vol.Required("uuid"): vol.In(choices)}),
        )
