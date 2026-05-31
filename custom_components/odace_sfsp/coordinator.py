"""Coordinator : écoute BLE passive + envoi, stockage des devices."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from homeassistant.components import bluetooth
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
from .sender import async_send, craft_payload, hci_index_from_name
from .const import (
    CONF_DEVICES,
    CONF_HCI,
    CONF_JEEDOM_KEY,
    CONF_MAC,
    DOMAIN,
    MANUFACTURER_ID,
    SIGNAL_DEVICES_CHANGED,
    SIGNAL_DEVICE_UPDATE,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

# Durée pendant laquelle une trame binding est mémorisée en attente (secondes).
# Si start_learn est appelé dans cette fenêtre, le périphérique est enregistré
# sans que l'utilisateur ait à appuyer à nouveau sur le bouton de binding.
_PENDING_BINDING_TTL = 60.0


class OdaceSFSPCoordinator:
    """Gère le cycle de vie BLE + la persistance des devices."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.hci_name: str = entry.data.get(CONF_HCI, "hci0")
        self.hci_index: int = hci_index_from_name(self.hci_name)
        self.dongle_mac: str = entry.data.get(CONF_MAC, "00:00:00:00:00:00")
        self.jeedom_key: str = entry.data.get(CONF_JEEDOM_KEY, "")
        self.devices: Dict[str, Dict[str, Any]] = dict(entry.data.get(CONF_DEVICES, {}))
        self.learn_mode: bool = False
        self._learn_expires: float = 0
        self._unsub_bt = None
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        # Dernier ordre envoyé pour éviter les boucles d'advertising sur dcl
        self._last_command: Dict[str, Dict[str, Any]] = {}
        # Trames binding reçues d'inconnus alors que learn mode était off.
        # Mémorisées pendant _PENDING_BINDING_TTL secondes pour que start_learn
        # puisse les traiter a posteriori (l'utilisateur a déclenché le binding
        # avant d'activer le mode apprentissage).
        self._pending_bindings: Dict[str, Dict[str, Any]] = {}
        # {uuid: {"result": ..., "expires": float}}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        """Enregistre le callback BLE sur les trames Schneider (0x02B6)."""
        matcher = BluetoothCallbackMatcher(manufacturer_id=MANUFACTURER_ID)
        self._unsub_bt = bluetooth.async_register_callback(
            self.hass,
            self._on_ble_advertisement,
            matcher,
            BluetoothScanningMode.PASSIVE,
        )
        _LOGGER.info(
            "Odace SFSP listening on %s (MAC %s) - %d devices loaded",
            self.hci_name, self.dongle_mac, len(self.devices),
        )

    async def async_stop(self) -> None:
        if self._unsub_bt is not None:
            self._unsub_bt()
            self._unsub_bt = None

    # ------------------------------------------------------------------
    # BLE callback
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
        mfg_hex = mfg_bytes.hex()
        mac = service_info.address
        result = parse_manufacturer_data(mfg_hex, mac)
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
                    # Mode apprentissage actif : enregistrer immédiatement
                    _LOGGER.info("Learn mode: new binding received for %s (model=%s)", uuid, result.get("model"))
                    self.learn_mode = False
                    self._pending_bindings.pop(uuid, None)
                    self._schedule_new_device(result)
                else:
                    # Mémoriser le binding pour que start_learn puisse le traiter
                    # si l'utilisateur active le mode apprentissage sous peu
                    expires = time.time() + _PENDING_BINDING_TTL
                    self._pending_bindings[uuid] = {"result": result, "expires": expires}
                    _LOGGER.debug(
                        "Binding from unknown device %s (learn mode off) — "
                        "mémorisé %.0fs (appeler start_learn pour l'enregistrer)",
                        uuid, _PENDING_BINDING_TTL,
                    )
            else:
                # Périphérique déjà connu qui renvoie une trame binding
                # (ex : reset d'usine, perte d'appairage) → re-pairing automatique
                model = self.devices[uuid].get("model", "")
                if model in ("dcl", "shutter", "plug", "dimmer", "generic"):
                    _LOGGER.info(
                        "Re-binding: périphérique connu %s a redemandé l'appairage → envoi trame pair",
                        uuid,
                    )
                    self.hass.async_create_task(self.async_send_pair(uuid))
            return

        # ---- Trames d'advertisement ----
        if uuid not in self.devices:
            _LOGGER.debug("Frame from unknown device %s - ignored", uuid)
            return

        # Mise à jour de la MAC si elle n'était pas connue
        if not self.devices[uuid].get("mac"):
            self.devices[uuid]["mac"] = mac

        async_dispatcher_send(
            self.hass,
            SIGNAL_DEVICE_UPDATE.format(uuid=uuid),
            result,
        )

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
        # Pour les périphériques commandables (DCL, volet, prise, dimmer, generic),
        # envoyer automatiquement la trame de pairing pour les associer au contrôleur.
        # Les switches n'ont pas de pairing (réception seule).
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
        async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)

    async def async_update_device(self, uuid: str, updates: Dict[str, Any]) -> None:
        """Met à jour un device. Si le model change, ça implique côté plateformes
        la suppression de l'entity précédente (ce que HA fait via le signal)."""
        uuid = uuid.lower()
        if uuid not in self.devices:
            return
        self.devices[uuid].update(updates)
        await self._async_persist()
        async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)

    async def _async_persist(self) -> None:
        new_data = {**self.entry.data, CONF_DEVICES: self.devices}
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------
    async def async_send_command(self, uuid: str, ac: str, options: Optional[int] = None) -> None:
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
        # Mémorise l'ordre pour la protection anti-boucle côté light
        self._last_command[uuid] = {"ac": ac, "ts": time.time()}
        await async_send(self.hci_index, payload)
        _LOGGER.info("Odace SFSP TX uuid=%s ac=%s options=%s", uuid, ac, options)

    async def async_send_pair(self, uuid: str) -> None:
        """Envoie la trame de pairing (``type=pair``) pour associer un périphérique.

        Cette trame informe le périphérique de l'identité du contrôleur (UUID_CONTROLLER
        + jeedom_key chiffrés avec UNIQUE_KEY). Sans elle, le périphérique n'accepte
        pas les commandes on/off/goto/… ultérieures.

        Applicable uniquement aux modèles commandables : dcl, shutter, plug, dimmer,
        generic. Les switches (réception seule) n'ont pas de mécanisme de pairing.
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
        await async_send(self.hci_index, payload)
        _LOGGER.info("Odace SFSP PAIR envoyé → uuid=%s", uuid)

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
        elles sont traitées immédiatement sans que l'utilisateur ait à
        appuyer à nouveau sur le bouton du périphérique.
        """
        self.learn_mode = True
        self._learn_expires = time.time() + timeout

        # Traiter les bindings en attente non expirés
        now = time.time()
        pending = {
            uuid: entry
            for uuid, entry in self._pending_bindings.items()
            if now < entry["expires"] and uuid not in self.devices
        }
        if pending:
            # Prendre le plus récent (dernier reçu)
            uuid, entry = max(pending.items(), key=lambda kv: kv[1]["expires"])
            _LOGGER.info(
                "Learn mode: traitement du binding en attente pour %s (reçu il y a %.0fs)",
                uuid, now - (entry["expires"] - _PENDING_BINDING_TTL),
            )
            self.learn_mode = False
            self._pending_bindings.pop(uuid, None)
            self._schedule_new_device(entry["result"])
        else:
            _LOGGER.info(
                "Learn mode activé pour %ds — appuyer sur le bouton de binding du périphérique",
                timeout,
            )

    def stop_learn(self) -> None:
        self.learn_mode = False
