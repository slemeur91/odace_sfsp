"""Coordinator : écoute BLE passive + envoi HCI (dongle USB) ou MQTT (ESP32).

La réception BLE (HA Bluetooth API) est identique dans les deux modes.
Seul l'envoi diffère :
  - SEND_MODE_HCI  → craft_payload + hcitool   (dongle USB local)
  - SEND_MODE_MQTT → craft_payload + MQTT pub  (ESP32 via ESPHome)

Le mode est déterminé par la clé CONF_SEND_MODE dans la config entry.
Les installations existantes sans CONF_SEND_MODE utilisent le mode HCI par défaut.
"""
from __future__ import annotations

import asyncio
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

        self.send_mode: str  = entry.data.get(CONF_SEND_MODE, SEND_MODE_HCI)
        self.dongle_mac: str = entry.data.get(CONF_MAC, "00:00:00:00:00:00")
        self.jeedom_key: str = entry.data.get(CONF_JEEDOM_KEY, "")
        self.devices: Dict[str, Dict[str, Any]] = dict(entry.data.get(CONF_DEVICES, {}))

        # Mode HCI — hci_name/hci_index ne sont pertinents qu'en SEND_MODE_HCI
        if self.send_mode == SEND_MODE_HCI:
            self.hci_name:  str = entry.data.get(CONF_HCI, "hci0")
            self.hci_index: int = hci_index_from_name(self.hci_name)
        else:
            self.hci_name  = ""
            self.hci_index = 0

        # Mode MQTT
        self.mqtt_topic: str = entry.data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC)

        # Mode ESPHome API
        self.esphome_entry_id: str = entry.data.get(CONF_ESPHOME_ENTRY_ID, "")
        self.esphome_service:  str = entry.data.get(CONF_ESPHOME_SERVICE, DEFAULT_ESPHOME_SERVICE)

        self.learn_mode: bool = False
        self._learn_expires: float = 0
        self._unsub_bt = None
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        # Dernier ordre envoyé — protection anti-boucle advertising
        self._last_command: Dict[str, Dict[str, Any]] = {}
        # Trames binding reçues hors mode apprentissage (mémorisées _PENDING_BINDING_TTL s)
        self._pending_bindings: Dict[str, Dict[str, Any]] = {}
        # UUIDs en attente de désappariage (bouton "Désappairer et supprimer")
        self._pending_unpair: set = set()
        # UUIDs pour lesquels la trame pair a été envoyée, en attente du code 13 (confirmation désappariage)
        self._pending_unpair_confirm: set = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        """Enregistre le callback BLE."""
        if self.send_mode == SEND_MODE_HCI:
            await self._resolve_hci_by_mac()

        matcher = BluetoothCallbackMatcher(manufacturer_id=MANUFACTURER_ID)
        self._unsub_bt = bluetooth.async_register_callback(
            self.hass, self._on_ble_advertisement, matcher, BluetoothScanningMode.PASSIVE,
        )
        if self.send_mode == SEND_MODE_MQTT:
            _LOGGER.info(
                "Odace SFSP [ESP32/MQTT] — MAC ESP32 %s, topic %s — %d devices",
                self.dongle_mac, self.mqtt_topic, len(self.devices),
            )
        elif self.send_mode == SEND_MODE_ESPHOME_API:
            _LOGGER.info(
                "Odace SFSP [ESPHome API] — entry_id=%s service=%s MAC BT=%s — %d devices",
                self.esphome_entry_id, self.esphome_service, self.dongle_mac, len(self.devices),
            )
        else:
            _LOGGER.info(
                "Odace SFSP [HCI] — %s (MAC %s) — %d devices",
                self.hci_name, self.dongle_mac, len(self.devices),
            )

    async def _resolve_hci_by_mac(self) -> None:
        """Résout dynamiquement l'interface HCI si le dongle a démarré sur un hciX inattendu.

        Stratégie :
        1. Vérifier si l'interface configurée (hci_name) est une interface fantôme (MAC nulle).
        2. Si oui, chercher parmi les adaptateurs connus de HA celui qui a une vraie MAC.
        3. dongle_mac est utilisée uniquement comme indice de priorité si elle correspond
           à un adaptateur réel — elle peut être une MAC d'encodage custom différente de la
           MAC physique, auquel cas elle est ignorée pour la résolution.

        Cette méthode ne modifie pas dongle_mac (utilisée pour l'encodage CMAC des trames).
        """
        _NULL_MAC = "00:00:00:00:00:00"

        try:
            adapters = await bluetooth.async_get_adapters(self.hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("_resolve_hci_by_mac : impossible de lire les adaptateurs BLE : %s", err)
            return

        # Trouver l'adaptateur actuellement configuré
        configured = next((a for a in adapters if a.get("name") == self.hci_name), None)
        if configured is None:
            _LOGGER.warning(
                "Odace SFSP — interface %s introuvable dans les adaptateurs HA"
                " (adaptateurs : %s)",
                self.hci_name, [a.get("name") for a in adapters],
            )
            return

        configured_mac = configured.get("address", _NULL_MAC)
        if configured_mac.upper() != _NULL_MAC:
            # L'interface configurée a une vraie MAC → aucun problème
            _LOGGER.debug(
                "_resolve_hci_by_mac : %s confirmé avec MAC %s", self.hci_name, configured_mac
            )
            return

        # Interface configurée = fantôme (MAC nulle) → chercher une interface réelle
        real_adapters = [
            a for a in adapters
            if a.get("address", _NULL_MAC).upper() != _NULL_MAC
        ]
        if not real_adapters:
            _LOGGER.warning(
                "Odace SFSP — %s est une interface fantôme (MAC nulle) mais"
                " aucun autre adaptateur BLE réel trouvé dans HA",
                self.hci_name,
            )
            return

        # Si dongle_mac est une vraie MAC physique (pas custom), elle peut servir de priorité
        priority = None
        if self.dongle_mac.upper() != _NULL_MAC:
            priority = next(
                (a for a in real_adapters if a.get("address", "").upper() == self.dongle_mac.upper()),
                None,
            )

        resolved_adapter = priority or real_adapters[0]
        resolved_name = resolved_adapter.get("name", "")
        resolved_mac = resolved_adapter.get("address", "")

        _LOGGER.warning(
            "Odace SFSP — %s est une interface fantôme (MAC nulle) →"
            " basculement sur %s (MAC %s)",
            self.hci_name, resolved_name, resolved_mac,
        )
        self.hci_name = resolved_name
        self.hci_index = hci_index_from_name(resolved_name)

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
        _LOGGER.debug("Odace SFSP RX uuid=%s model=%s data=%s", uuid, result.get("model"), result["data"])

        # ---- Trames de binding ----
        if result["data"].get("type") == "binding":
            if uuid not in self.devices:
                if self.learn_mode and time.time() < self._learn_expires:
                    _LOGGER.info("Learn mode: binding reçu pour %s (model=%s)", uuid, result.get("model"))
                    self.learn_mode = False
                    self._pending_bindings.pop(uuid, None)
                    self._schedule_new_device(result)
                else:
                    # Mémoriser pour que start_learn puisse traiter a posteriori
                    self._pending_bindings[uuid] = {"result": result, "expires": time.time() + _PENDING_BINDING_TTL}
                    _LOGGER.debug("Binding de %s mémorisé %.0fs (learn mode off)", uuid, _PENDING_BINDING_TTL)
            else:
                # Périphérique connu qui renvoie une trame binding
                model = self.devices[uuid].get("model", "")
                if uuid in self._pending_unpair:
                    # Mode désappariage actif : envoyer pair (toggle → désappairé) puis retirer
                    self._pending_unpair.discard(uuid)
                    _LOGGER.info(
                        "Désappariage %s : binding reçu → envoi pair (toggle désappairé) puis suppression",
                        uuid,
                    )
                    self.hass.async_create_task(self._async_unpair_and_remove(uuid))
                elif model in ("dcl", "shutter", "plug", "dimmer", "generic"):
                    # Reset usine ou perte d'appairage → ré-appairage automatique
                    _LOGGER.info("Re-binding connu %s → envoi pair", uuid)
                    self.hass.async_create_task(self.async_send_pair(uuid))
            return

        # ---- Trames d'advertisement ----
        if uuid not in self.devices:
            _LOGGER.debug("Frame from unknown device %s - ignored", uuid)
            return

        if not self.devices[uuid].get("mac"):
            self.devices[uuid]["mac"] = service_info.address

        # Confirmation de désappariage : le module diffuse code 13 (paired=unpaired)
        if uuid in self._pending_unpair_confirm and result["data"].get("paired") == "unpaired":
            self._pending_unpair_confirm.discard(uuid)
            _LOGGER.info("Odace SFSP — désappariage confirmé (code 13) pour %s → suppression", uuid)
            self.hass.async_create_task(self.async_remove_device(uuid))
            return

        async_dispatcher_send(self.hass, SIGNAL_DEVICE_UPDATE.format(uuid=uuid), result)

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------
    @callback
    def _schedule_new_device(self, parsed: Dict[str, Any]) -> None:
        uuid  = parsed["uuid"].lower()
        model = parsed.get("model", "dcl")
        self.devices[uuid] = {
            "uuid": uuid, "mac": parsed.get("mac", ""),
            "model": model, "name": f"Odace SFSP {model} {uuid}",
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
        device  = dev_reg.async_get_device(identifiers={(DOMAIN, uuid)})
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
            device  = dev_reg.async_get_device(identifiers={(DOMAIN, uuid)})
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
            await async_send_esphome_api(self.hass, self.esphome_entry_id, self.esphome_service, payload)
        else:
            await hci_send(self.hci_index, payload)

    async def async_send_command(self, uuid: str, ac: str, options: Optional[int] = None) -> None:
        uuid   = uuid.lower()
        device = self.devices.get(uuid)
        if device is None:
            _LOGGER.error("Unknown device %s", uuid)
            return
        data: Dict[str, Any] = {"ac": ac}
        if options is not None:
            data["options"] = options

        # FIX : transmettre "type" au device dict passé à craft_payload.
        # build_frame() lit device.get("type", "custom") pour déterminer le
        # Param BLE des scènes (SCENES["custom"]="FC" ou SCENES["schneider"]="FD").
        # Sans ce champ, le type de scène serait toujours "custom" par défaut.
        device_for_payload: Dict[str, Any] = {
            "uuid":  device["uuid"].upper(),
            "model": device["model"],
        }
        if device.get("type"):
            device_for_payload["type"] = device["type"]

        payload = craft_payload(
            device_for_payload,
            "advertisement", self.jeedom_key, self.dongle_mac, data,
        )
        self._last_command[uuid] = {"ac": ac, "ts": time.time()}
        await self._dispatch_send(payload)
        _LOGGER.info("Odace SFSP TX [%s] uuid=%s ac=%s options=%s", self.send_mode, uuid, ac, options)

    async def async_send_pair(self, uuid: str) -> None:
        """Envoie la trame de pairing pour associer un périphérique commandable.

        Applicable aux modèles : dcl, shutter, plug, dimmer, generic.
        Les switches (réception seule) n'ont pas de mécanisme de pairing.
        """
        uuid   = uuid.lower()
        device = self.devices.get(uuid)
        if device is None:
            _LOGGER.error("async_send_pair: device %s inconnu", uuid)
            return
        payload = craft_payload(
            {"uuid": device["uuid"].upper(), "model": device["model"]},
            "pair", self.jeedom_key, self.dongle_mac,
        )
        await self._dispatch_send(payload)
        _LOGGER.info("Odace SFSP PAIR [%s] envoyé → uuid=%s", self.send_mode, uuid)

    async def start_unpair(self, uuid: str, timeout: float = 60.0) -> None:
        """Active le mode désappariage pour un UUID donné.

        Le coordinateur attend la prochaine trame binding de ce device.
        Quand elle arrive, il envoie une trame pair (toggle → désappairé)
        puis retire le device de HA.
        """
        uuid = uuid.lower()
        if uuid not in self.devices:
            _LOGGER.error("start_unpair: device %s inconnu", uuid)
            return
        self._pending_unpair.add(uuid)
        _LOGGER.info("Odace SFSP — désappariage en attente pour %s (%.0fs)", uuid, timeout)

        async def _cancel_after_timeout() -> None:
            await asyncio.sleep(timeout)
            if uuid in self._pending_unpair:
                self._pending_unpair.discard(uuid)
                _LOGGER.info("Odace SFSP — désappariage annulé (timeout) pour %s", uuid)

        self.hass.async_create_task(_cancel_after_timeout())

    async def _async_unpair_and_remove(self, uuid: str) -> None:
        """Envoie la trame pair (toggle désappairé) puis attend la confirmation (code 13) avant de retirer."""
        uuid = uuid.lower()
        await self.async_send_pair(uuid)
        self._pending_unpair_confirm.add(uuid)
        _LOGGER.info(
            "Odace SFSP — trame pair envoyée pour %s, attente confirmation désappariage (code 13)", uuid
        )

        async def _remove_after_timeout() -> None:
            await asyncio.sleep(15.0)
            if uuid in self._pending_unpair_confirm:
                self._pending_unpair_confirm.discard(uuid)
                _LOGGER.warning(
                    "Odace SFSP — pas de confirmation désappariage (code 13) pour %s après 15s,"
                    " suppression forcée",
                    uuid,
                )
                await self.async_remove_device(uuid)

        self.hass.async_create_task(_remove_after_timeout())

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
        self.learn_mode    = True
        self._learn_expires = time.time() + timeout

        now     = time.time()
        pending = {
            uid: e for uid, e in self._pending_bindings.items()
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
