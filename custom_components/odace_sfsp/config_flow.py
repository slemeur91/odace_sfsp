"""Config flow : sélection du mode d'envoi BLE.

Étapes pour une nouvelle installation :
  async_step_user        → Choix du mode (HCI / ESP32 MQTT / ESPHome API)
    ↓ HCI
  async_step_hci         → Sélection du dongle + clé Jeedom
    ↓ ESP32/MQTT
  async_step_mqtt_broker → Saisie du topic MQTT
  async_step_mqtt_mac    → Découverte automatique MAC ESP32 (8 s) ou saisie manuelle
    ↓ ESPHome API
  async_step_esphome     → Sélection du device ESPHome (dropdown) + nom du service custom

Découverte automatique de la MAC ESP32 (mode MQTT) :
  L'ESP32 publie sa MAC Bluetooth sur ``odace_sfsp/mac`` à la connexion MQTT.
  Le config flow souscrit à ce topic et attend 8 secondes.

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
    DEFAULT_MQTT_MAC_TOPIC,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    FORCE_DONGLE_MAC,
    FORCE_JEEDOM_KEY,
    KNOWN_DEVICES,
    MAC_DISCOVERY_TIMEOUT,
    SEND_MODE_ESPHOME_API,
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


def _guess_esphome_bt_mac(hass, entry_id: str) -> str:
    """Tente de retrouver la MAC Bluetooth de l'ESP32 par plusieurs méthodes.

    Méthode 1 — aioesphomeapi DeviceInfo.bluetooth_mac_address :
      Disponible si l'ESPHome est connecté et que sa version d'aioesphomeapi
      expose ce champ (ajouté pour les bluetooth_proxy).

    Méthode 2 — Bluetooth manager de HA :
      Les proxies ESPHome s'enregistrent comme scanners BLE dans HA.
      Leur adresse source est la MAC BT de l'ESP32.

    Méthode 3 — device registry CONNECTION_BLUETOOTH :
      Certaines versions HA/ESPHome enregistrent aussi une connexion BT.

    Retourne la MAC en majuscules (ex: "AA:BB:CC:DD:EE:FF") ou "" si introuvable.
    """
    # --- Méthode 1 : RuntimeEntryData ESPHome → device_info.bluetooth_mac_address ---
    try:
        entry_data = hass.data.get("esphome", {}).get(entry_id)
        if entry_data is not None:
            device_info = getattr(entry_data, "device_info", None)
            if device_info is not None:
                bt_mac = getattr(device_info, "bluetooth_mac_address", None) or ""
                if bt_mac and bt_mac not in ("", "00:00:00:00:00:00"):
                    _LOGGER.debug("ESPHome BT MAC (méthode 1 RuntimeEntryData) : %s", bt_mac)
                    return bt_mac.upper()
                _LOGGER.debug(
                    "Méthode 1 : device_info trouvé mais bluetooth_mac_address absent ou nul "
                    "(champs disponibles : %s)",
                    [f for f in dir(device_info) if not f.startswith("_")],
                )
            else:
                _LOGGER.debug("Méthode 1 : entry_data trouvé mais device_info absent")
        else:
            _LOGGER.debug("Méthode 1 : entry_id %s absent de hass.data['esphome']", entry_id)
    except Exception as err:
        _LOGGER.debug("Méthode 1 exception : %s", err)

    # --- Méthode 2 : bluetooth manager → scanner source ---
    try:
        esphome_entry = hass.config_entries.async_get_entry(entry_id)
        if esphome_entry is not None:
            device_slug = (
                esphome_entry.title.lower().replace(" ", "_").replace("-", "_")
            )
            bt_manager = hass.data.get("bluetooth")
            if bt_manager is not None:
                # _adapters : {source_mac: AdapterDetails}
                for source, adapter in getattr(bt_manager, "_adapters", {}).items():
                    adapter_name = (getattr(adapter, "name", "") or "").lower()
                    if device_slug in adapter_name.replace(" ", "_").replace("-", "_"):
                        mac = source.upper()
                        if mac != "00:00:00:00:00:00":
                            _LOGGER.debug("ESPHome BT MAC (méthode 2 adapter) : %s", mac)
                            return mac
                # Fallback : scanners enregistrés
                for scanner in getattr(bt_manager, "_scanners", {}).values():
                    scanner_name = (getattr(scanner, "name", "") or "").lower()
                    if device_slug in scanner_name.replace(" ", "_").replace("-", "_"):
                        src = getattr(scanner, "source", "") or ""
                        if src and src.upper() != "00:00:00:00:00:00":
                            _LOGGER.debug("ESPHome BT MAC (méthode 2 scanner) : %s", src)
                            return src.upper()
                _LOGGER.debug(
                    "Méthode 2 : bt_manager trouvé mais aucun adapter/scanner correspondant à '%s'. "
                    "Adapters : %s — Scanners : %s",
                    device_slug,
                    list(getattr(bt_manager, "_adapters", {}).keys()),
                    [getattr(s, "source", "?") for s in getattr(bt_manager, "_scanners", {}).values()],
                )
            else:
                _LOGGER.debug("Méthode 2 : hass.data['bluetooth'] absent")
    except Exception as err:
        _LOGGER.debug("Méthode 2 exception : %s", err)

    # --- Méthode 3 : device registry CONNECTION_BLUETOOTH ---
    try:
        from homeassistant.helpers import device_registry as dr
        dev_reg = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(dev_reg, entry_id):
            for conn_type, conn_id in device.connections:
                if conn_type == dr.CONNECTION_BLUETOOTH:
                    _LOGGER.debug("ESPHome BT MAC (méthode 3 device registry) : %s", conn_id)
                    return conn_id.upper()
        _LOGGER.debug("Méthode 3 : aucune CONNECTION_BLUETOOTH dans le device registry pour %s", entry_id)
    except Exception as err:
        _LOGGER.debug("Méthode 3 exception : %s", err)

    _LOGGER.debug(
        "MAC BT ESP32 introuvable automatiquement — saisie manuelle requise. "
        "Consulter les logs ESPHome au démarrage : 'Bluetooth controller initialized, address XX:XX:XX:XX:XX:XX'"
    )
    return ""


def _list_esphome_entries(hass) -> Dict[str, str]:
    """Retourne {entry_id: 'Titre'} pour toutes les config entries ESPHome.

    Utilisé dans le config flow pour proposer un dropdown des devices ESPHome
    disponibles, sans que l'utilisateur ait à saisir quoi que ce soit manuellement.
    """
    entries: Dict[str, str] = {}
    for entry in hass.config_entries.async_entries("esphome"):
        entries[entry.entry_id] = entry.title
    return entries


def _list_esphome_services(hass, entry_id: str) -> Dict[str, str]:
    """Retourne {service_name: label} des services custom exposés par un device ESPHome.

    Filtre les services HA du domaine ``esphome`` pour ne garder que ceux
    appartenant au device dont l'entry_id est fourni.
    Si le firmware n'a pas encore été flashé avec les services, retourne {}.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return {}
    device_slug = entry.title.lower().replace(" ", "_").replace("-", "_")
    prefix = f"{device_slug}_"
    services: Dict[str, str] = {}
    for svc_name in hass.services.async_services().get("esphome", {}):
        if svc_name.startswith(prefix):
            # Nom sans le préfixe device, ex : "smart_doorbell_odace_send" → "odace_send"
            short = svc_name[len(prefix):]
            services[short] = short
    return services


async def _list_hci_adapters(hass) -> Dict[str, str]:
    """Retourne {adapter_name: 'name (MAC)'} pour tous les contrôleurs BLE locaux.

    Utilise sysfs (/sys/class/bluetooth/) pour énumérer les adaptateurs HCI
    sans dépendre de l'API bluetooth de HA (dont l'interface a changé entre versions).
    """
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
                            SEND_MODE_MQTT:        "ESP32 via MQTT (sans dongle USB sur HA)",
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
        default_key = FORCE_JEEDOM_KEY if FORCE_JEEDOM_KEY else secrets.token_hex(12)

        if FORCE_DONGLE_MAC:
            adapters_labels = {k: f"{k} ({FORCE_DONGLE_MAC})" for k in adapters}
        else:
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

    # ------------------------------------------------------------------
    # Branche ESPHome API
    # ------------------------------------------------------------------
    async def async_step_esphome(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Sélection du device ESPHome + nom du service custom BLE.

        Liste automatiquement toutes les intégrations ESPHome connues de HA
        pour éviter toute saisie manuelle. L'utilisateur choisit son device
        et confirme (ou modifie) le nom du service déclaré dans son firmware.
        """
        errors: Dict[str, str] = {}
        esphome_entries = _list_esphome_entries(self.hass)

        if not esphome_entries:
            return self.async_abort(reason="no_esphome_device")

        default_key = FORCE_JEEDOM_KEY if FORCE_JEEDOM_KEY else secrets.token_hex(12)

        # Pré-sélectionner le premier device si un seul est disponible
        default_entry = next(iter(esphome_entries))

        # Tentative de détection automatique de la MAC BT de l'ESP32
        guessed_mac = _guess_esphome_bt_mac(self.hass, default_entry)

        if user_input is not None:
            entry_id  = user_input[CONF_ESPHOME_ENTRY_ID]
            service   = user_input.get(CONF_ESPHOME_SERVICE, DEFAULT_ESPHOME_SERVICE).strip()
            esp32_mac = user_input.get(CONF_MAC, "").strip().upper()

            if not service:
                errors[CONF_ESPHOME_SERVICE] = "invalid_service"
            if esp32_mac and not _is_valid_mac(esp32_mac):
                errors[CONF_MAC] = "invalid_mac"

            if not errors:
                entry_title = esphome_entries.get(entry_id, entry_id)
                await self.async_set_unique_id(f"odace_sfsp_esphome-{entry_id}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Odace SFSP ({entry_title})",
                    data={
                        CONF_SEND_MODE:        SEND_MODE_ESPHOME_API,
                        CONF_ESPHOME_ENTRY_ID: entry_id,
                        CONF_ESPHOME_SERVICE:  service,
                        CONF_MAC:              esp32_mac or "00:00:00:00:00:00",
                        CONF_JEEDOM_KEY:       user_input.get(CONF_JEEDOM_KEY) or default_key,
                        CONF_DEVICES:          _import_known_devices(),
                    },
                )

        # Services disponibles pour ce device (vide si firmware pas encore à jour)
        available_services = _list_esphome_services(self.hass, default_entry)

        schema_fields: Dict[Any, Any] = {
            vol.Required(CONF_ESPHOME_ENTRY_ID, default=default_entry): vol.In(
                esphome_entries
            ),
        }

        if available_services:
            default_svc = (
                DEFAULT_ESPHOME_SERVICE
                if DEFAULT_ESPHOME_SERVICE in available_services
                else next(iter(available_services))
            )
            schema_fields[
                vol.Required(CONF_ESPHOME_SERVICE, default=default_svc)
            ] = vol.In(available_services)
        else:
            schema_fields[
                vol.Required(CONF_ESPHOME_SERVICE, default=DEFAULT_ESPHOME_SERVICE)
            ] = str

        # MAC BT de l'ESP32 — pré-remplie si détectée automatiquement
        schema_fields[
            vol.Required(CONF_MAC, default=guessed_mac or "AA:BB:CC:DD:EE:FF")
        ] = str

        schema_fields[vol.Optional(CONF_JEEDOM_KEY, default=default_key)] = str

        return self.async_show_form(
            step_id="esphome",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "service_name": DEFAULT_ESPHOME_SERVICE,
                "mac_hint": guessed_mac or "introuvable — voir les logs ESPHome",
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
        if send_mode == SEND_MODE_MQTT:
            network_label = "Modifier la configuration ESP32/MQTT"
        elif send_mode == SEND_MODE_ESPHOME_API:
            network_label = "Modifier la configuration ESPHome API"
        else:
            network_label = "Modifier le dongle Bluetooth"
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

        elif send_mode == SEND_MODE_ESPHOME_API:
            esphome_entries = _list_esphome_entries(self.hass)
            if not esphome_entries:
                return self.async_abort(reason="no_esphome_device")
            current_entry_id = self.entry.data.get(CONF_ESPHOME_ENTRY_ID, "")
            current_service  = self.entry.data.get(CONF_ESPHOME_SERVICE, DEFAULT_ESPHOME_SERVICE)
            current_mac      = self.entry.data.get(CONF_MAC, "")
            default_entry    = current_entry_id if current_entry_id in esphome_entries else next(iter(esphome_entries))
            available_services = _list_esphome_services(self.hass, default_entry)

            if user_input is not None:
                esp32_mac = user_input.get(CONF_MAC, "").strip().upper()
                service   = user_input.get(CONF_ESPHOME_SERVICE, "").strip()
                if esp32_mac and not _is_valid_mac(esp32_mac):
                    errors[CONF_MAC] = "invalid_mac"
                if not service:
                    errors[CONF_ESPHOME_SERVICE] = "invalid_service"
                if not errors:
                    self.hass.config_entries.async_update_entry(
                        self.entry,
                        data={
                            **self.entry.data,
                            CONF_ESPHOME_ENTRY_ID: user_input[CONF_ESPHOME_ENTRY_ID],
                            CONF_ESPHOME_SERVICE:  service,
                            CONF_MAC:              esp32_mac or current_mac,
                        },
                    )
                    return self.async_create_entry(title="", data={})

            network_schema: Dict[Any, Any] = {
                vol.Required(CONF_ESPHOME_ENTRY_ID, default=default_entry): vol.In(esphome_entries),
            }
            if available_services:
                default_svc = current_service if current_service in available_services else next(iter(available_services))
                network_schema[vol.Required(CONF_ESPHOME_SERVICE, default=default_svc)] = vol.In(available_services)
            else:
                network_schema[vol.Required(CONF_ESPHOME_SERVICE, default=current_service)] = str

            network_schema[vol.Required(CONF_MAC, default=current_mac or "AA:BB:CC:DD:EE:FF")] = str

            return self.async_show_form(
                step_id="network",
                data_schema=vol.Schema(network_schema),
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
