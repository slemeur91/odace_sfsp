"""Coordinator : écoute BLE passive + envoi, stockage des devices."""
from __future__ import annotations

import asyncio
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

        # Gestion de l'apprentissage (binding)
        if result["data"].get("type") == "binding":
            if uuid not in self.devices:
                if self.learn_mode and time.time() < self._learn_expires:
                    _LOGGER.info("Learn mode: new binding received for %s", uuid)
                    self.learn_mode = False
                    self._schedule_new_device(result)
                else:
                    _LOGGER.debug("Binding for unknown device %s (learn mode off)", uuid)
                return
            # Déjà connu : on ignore les binding repeat
            return

        # Équipement inconnu et pas en mode apprentissage -> on ignore
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
        self.devices[uuid] = {
            "uuid": uuid,
            "mac": parsed.get("mac", ""),
            "model": parsed.get("model", "dcl"),
            "name": f"Odace SFSP {parsed.get('model','dcl')} {uuid}",
        }
        async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)
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
        self.learn_mode = True
        self._learn_expires = time.time() + timeout
        _LOGGER.info("Learn mode enabled for %ds", timeout)

    def stop_learn(self) -> None:
        self.learn_mode = False
