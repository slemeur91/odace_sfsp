"""Coordinator : écoute BLE passive + envoi HCI (dongle USB) ou MQTT (ESP32).

La réception BLE (HA Bluetooth API) est identique dans les deux modes.
Seul l'envoi diffère :
  - SEND_MODE_HCI  → craft_payload + hcitool   (dongle USB local)
  - SEND_MODE_MQTT → craft_payload + MQTT pub  (ESP32 via ESPHome)

Le mode est déterminé par la clé CONF_SEND_MODE dans la config entry.
Les installations existantes sans CONF_SEND_MODE utilisent le mode HCI par défaut.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from homeassistant.components import bluetooth
from homeassistant.helpers import device_registry as dr
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .parser import parse_manufacturer_data
from .sender import (
    async_send as hci_send,
    async_send_esphome_api,
    async_send_mqtt,
    craft_payload,
    hci_index_from_name,
)
from .const import (
    CONF_DEVICES,
    CONF_ESPHOME_ENTRY_ID,
    CONF_ESPHOME_SERVICE,
    CONF_HCI,
    CONF_JEEDOM_KEY,
    CONF_MAC,
    CONF_MQTT_TOPIC,
    CONF_SEND_MODE,
    DEFAULT_ESPHOME_SERVICE,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    MANUFACTURER_ID,
    SEND_MODE_ESPHOME_API,
    SEND_MODE_HCI,
    SEND_MODE_MQTT,
    SIGNAL_DEVICES_CHANGED,
    SIGNAL_DEVICE_UPDATE,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

# Durée pendant laquelle une trame binding est mémorisée en attente (secondes).
# Si start_learn est appelé dans cette fenêtre, le périphérique est enregistré
# sans que l'utilisateur ait à appuyer à nouveau sur le bouton.
_PENDING_BINDING_TTL = 60.0


class OdaceSFSPCoordinator:
    """Gère le cycle de vie BLE + la persistance des devices.

    Supporte deux modes d'envoi :
    - HCI  : dongle USB local (HAOS, Proxmox, VM) via hcitool
    - MQTT : ESP32 via ESPHome Bluetooth Proxy + publication MQTT
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        self.send_mode: str = entry.data.get(CONF_SEND_MODE, SEND_MODE_HCI)
        self.dongle_mac: str = entry.data.get(CONF_MAC, "00:00:00:00:00:00")
        self.jeedom_key: str = entry.data.get(CONF_JEEDOM_KEY, "")
        self.devices: Dict[str, Dict[str, Any]] = dict(entry.data.get(CONF_DEVICES, {}))

        # Mode HCI
        self.hci_name: str = entry.data.get(CONF_HCI, "hci0")
        self.hci_index: int = hci_index_from_name(self.hci_name)

        # Mode ESP32/MQTT
        self.mqtt_topic: str = entry.data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC)

        # Mode ESPHome API
        self.esphome_entry_id: str = entry.data.get(CONF_ESPHOME_ENTRY_ID, "")
        self.esphome_service: str  = entry.data.get(CONF_ESPHOME_SERVICE, DEFAULT_ESPHOME_SERVICE)

        self.learn_mode: bool = False
        self._learn_expires: float = 0
        self._unsub_bt = None
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        # Dernier ordre envoyé — protection anti-boucle advertising
        self._last_command: Dict[str, Dict[str, Any]] = {}
        # Trames binding reçues hors mode apprentissage (mémorisées _PENDING_BINDING_TTL s)
        self._pending_bindings: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        """Enregistre le callback BLE."""
        matcher = BluetoothCallbackMatcher(manufacturer_id=MANUFACTURER_ID)
        self._unsub_bt = bluetooth.async_register_callback(
            self.hass,
            self._on_ble_advertisement,
            matcher,
            BluetoothScanningMode.PASSIVE,
        )
        if self.send_mode == SEND_MODE_MQTT:
            _LOGGER.info(
                "Odace SFSP [ESP32/MQTT] — MAC ESP32 %s, topic %s — %d devices",
                self.dongle_mac, self.mqtt_topic, len(self.devices),
            )
        elif self.send_mode == SEND_MODE_ESPHOME_API:
            _LOGGER.info(
                "Odace SFSP [ESPHome API] — entry_id=%s service=%s — %d devices",
                self.esphome_entry_id, self.esphome_service, len(self.devices),
            )
        else:
            _LOGGER.info(
                "Odace SFSP [HCI] — %s (MAC %s) — %d devices",
                self.hci_name, self.dongle_mac, len(self.devices),
            )

    async def async_stop(self) -> None:
        if self._unsub_bt is not None:
            self._unsub_bt()
            self._unsub_bt = None

    # ------------------------------------------------------------------
    # BLE callback (identique dans les deux modes)
    # ------------------------------------------------------------------
    @callback
    def _on_ble_advertisement(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        mfg_bytes = service_info.manufacturer_data.get(MANUFACTURER_ID)
        if not mfg_bytes:
            return
        result = parse_manufacturer_data(mfg_bytes.hex(), service_info.address)
        if not result:
            return

        uuid = result["uuid"].lower()
        _LOGGER.debug(
            "Odace SFSP RX uuid=%s model=%s data=%s",
            uuid, result.get("model"), result["data"],
        )

        # ---- Trames de binding ----
        if result["data"].get("type") == "binding":
            if uuid not in self.devices:
                if self.learn_mode and time.time() < self._learn_expires:
                    _LOGGER.info(
                        "Learn mode: binding reçu pour %s (model=%s)",
                        uuid, result.get("model"),
                    )
                    self.learn_mode = False
                    self._pending_bindings.pop(uuid, None)
                    self._schedule_new_device(result)
                else:
                    # Mémoriser pour que start_learn puisse traiter a posteriori
                    self._pending_bindings[uuid] = {
                        "result": result,
                        "expires": time.time() + _PENDING_BINDING_TTL,
                    }
                    _LOGGER.debug(
                        "Binding de %s mémorisé %.0fs (learn mode off)",
                        uuid, _PENDING_BINDING_TTL,
                    )
            else:
                # Périphérique connu qui renvoie une trame binding (reset usine, perte d'appairage)
                model = self.devices[uuid].get("model", "")
                if model in ("dcl", "shutter", "plug", "dimmer", "generic"):
                    _LOGGER.info("Re-binding connu %s → envoi pair", uuid)
                    self.hass.async_create_task(self.async_send_pair(uuid))
            return

        # ---- Trames d'advertisement ----
        if uuid not in self.devices:
            _LOGGER.debug("Frame from unknown device %s - ignored", uuid)
            return

        if not self.devices[uuid].get("mac"):
            self.devices[uuid]["mac"] = service_info.address

        async_dispatcher_send(self.hass, SIGNAL_DEVICE_UPDATE.format(uuid=uuid), result)

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------
    @callback
    def _schedule_new_device(self, parsed: Dict[str, Any]) -> None:
        uuid = parsed["uuid"].lower()
        model = parsed.get("model", "dcl")
        self.devices[uuid] = {
            "uuid": uuid,
            "mac": parsed.get("mac", ""),
            "model": model,
            "name": f"Odace SFSP {model} {uuid}",
        }
        async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)
        if model in ("dcl", "shutter", "plug", "dimmer", "generic"):
            self.hass.async_create_task(self.async_send_pair(uuid))
        self.hass.async_create_task(self._async_persist())

    async def async_add_device(self, device: Dict[str, Any]) -> None:
        uuid = device["uuid"].lower()
        self.devices[uuid] = {**device, "uuid": uuid}
        await self._async_persist()
        async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)

    async def async_remove_device(self, uuid: str) -> None:
        uuid = uuid.lower()
        self.devices.pop(uuid, None)
        await self._async_persist()
        # Supprimer le device (et ses entités) du registre HA
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_device(identifiers={(DOMAIN, uuid)})
        if device:
            dev_reg.async_remove_device(device.id)
        async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)

    async def async_update_device(self, uuid: str, updates: Dict[str, Any]) -> None:
        uuid = uuid.lower()
        if uuid not in self.devices:
            return
        self.devices[uuid].update(updates)
        await self._async_persist()
        # Mettre à jour le nom dans le registre HA si besoin
        if "name" in updates:
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get_device(identifiers={(DOMAIN, uuid)})
            if device:
                dev_reg.async_update_device(device.id, name=updates["name"])
        async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)

    async def _async_persist(self) -> None:
        new_data = {**self.entry.data, CONF_DEVICES: self.devices}
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    # ------------------------------------------------------------------
    # Send — aiguillage HCI ou ESP32/MQTT
    # ------------------------------------------------------------------
    async def _dispatch_send(self, payload: str) -> None:
        """Envoie le payload via le mode configuré."""
        if self.send_mode == SEND_MODE_MQTT:
            await async_send_mqtt(self.hass, self.mqtt_topic, payload)
        elif self.send_mode == SEND_MODE_ESPHOME_API:
            await async_send_esphome_api(
                self.hass, self.esphome_entry_id, self.esphome_service, payload
            )
        else:
            await hci_send(self.hci_index, payload)

    async def async_send_command(
        self, uuid: str, ac: str, options: Optional[int] = None
    ) -> None:
        uuid = uuid.lower()
        device = self.devices.get(uuid)
        if device is None:
            _LOGGER.error("Unknown device %s", uuid)
            return
        data: Dict[str, Any] = {"ac": ac}
        if options is not None:
            data["options"] = options
        payload = craft_payload(
            {"uuid": device["uuid"].upper(), "model": device["model"]},
            "advertisement",
            self.jeedom_key,
            self.dongle_mac,
            data,
        )
        self._last_command[uuid] = {"ac": ac, "ts": time.time()}
        await self._dispatch_send(payload)
        _LOGGER.info(
            "Odace SFSP TX [%s] uuid=%s ac=%s options=%s",
            self.send_mode, uuid, ac, options,
        )

    async def async_send_pair(self, uuid: str) -> None:
        """Envoie la trame de pairing pour associer un périphérique commandable.

        Applicable aux modèles : dcl, shutter, plug, dimmer, generic.
        Les switches (réception seule) n'ont pas de mécanisme de pairing.
        """
        uuid = uuid.lower()
        device = self.devices.get(uuid)
        if device is None:
            _LOGGER.error("async_send_pair: device %s inconnu", uuid)
            return
        payload = craft_payload(
            {"uuid": device["uuid"].upper(), "model": device["model"]},
            "pair",
            self.jeedom_key,
            self.dongle_mac,
        )
        await self._dispatch_send(payload)
        _LOGGER.info(
            "Odace SFSP PAIR [%s] envoyé → uuid=%s",
            self.send_mode, uuid,
        )

    def was_commanded_recently(self, uuid: str, ac: str, window: float = 2.0) -> bool:
        """Anti-boucle : True si on vient d'envoyer cette commande pour ce uuid."""
        last = self._last_command.get(uuid.lower())
        if not last:
            return False
        return last["ac"] == ac and (time.time() - last["ts"]) < window

    # ------------------------------------------------------------------
    # Learn mode
    # ------------------------------------------------------------------
    def start_learn(self, timeout: float = 60.0) -> None:
        """Active le mode apprentissage et traite les bindings en attente.

        Si des trames binding ont été reçues récemment (dans la fenêtre
        _PENDING_BINDING_TTL) alors que le mode apprentissage était off,
        elles sont traitées immédiatement.
        """
        self.learn_mode = True
        self._learn_expires = time.time() + timeout

        now = time.time()
        pending = {
            uid: e
            for uid, e in self._pending_bindings.items()
            if now < e["expires"] and uid not in self.devices
        }
        if pending:
            uid, entry = max(pending.items(), key=lambda kv: kv[1]["expires"])
            _LOGGER.info(
                "Learn mode: traitement du binding en attente pour %s (reçu il y a %.0fs)",
                uid, now - (entry["expires"] - _PENDING_BINDING_TTL),
            )
            self.learn_mode = False
            self._pending_bindings.pop(uid, None)
            self._schedule_new_device(entry["result"])
        else:
            _LOGGER.info(
                "Learn mode activé pour %ds [%s] — appuyer sur le bouton de binding",
                timeout, self.send_mode,
            )

    def stop_learn(self) -> None:
        self.learn_mode = False

    def get_pending_uuids(self) -> list:
        """Retourne les UUIDs récemment vus en mode binding (non encore enregistrés).

        Utilisé par le config flow pour pré-remplir l'UUID lors d'un ajout manuel.
        Chaque entrée : {"uuid": str, "model": str, "seconds_ago": int}.
        """
        now = time.time()
        result = []
        for uid, entry in self._pending_bindings.items():
            if now < entry["expires"] and uid not in self.devices:
                result.append({
                    "uuid": uid,
                    "model": entry["result"].get("model", "unknown"),
                    "seconds_ago": int(now - (entry["expires"] - _PENDING_BINDING_TTL)),
                })
        return sorted(result, key=lambda x: x["seconds_ago"])
