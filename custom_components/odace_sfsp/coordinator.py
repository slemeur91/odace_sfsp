"""Coordinator : écoute BLE passive, envoi des trames et gestion des modules.

La réception (API Bluetooth de Home Assistant) est identique dans tous les modes.
Seul l'envoi diffère, selon CONF_SEND_MODE :
  - SEND_MODE_HCI         → contrôleur Bluetooth local. La trame est émise par
                            hcitool (défaut) ou par le canal de gestion du noyau,
                            selon CONF_HCI_METHOD.
  - SEND_MODE_ESPHOME_API → ESP32 via un service de l'API native ESPHome.
  - SEND_MODE_MQTT        → ESP32 via MQTT (installations existantes uniquement).

Les installations sans CONF_SEND_MODE utilisent le mode HCI par défaut.

Le coordinator gère aussi : la liste des modules et sa persistance, l'association
(modules « à associer »), le désappairage, la recherche de scènes et le suivi de
l'interface HCI (renumérotation, interface éteinte).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from homeassistant.components import bluetooth
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .parser import forget_dedupe, parse_manufacturer_data
from .sender import (
    async_send as hci_send,
    async_send_esphome_api,
    async_send_mqtt,
    craft_payload,
    hci_index_from_name,
)
from .const import (
    CONF_ADAPTER_MAC,
    CONF_DEVICES,
    CONF_ESPHOME_ENTRY_ID,
    CONF_ESPHOME_SERVICE,
    CONF_HCI,
    CONF_HCI_METHOD,
    CONF_JEEDOM_KEY,
    CONF_MAC,
    CONF_MQTT_TOPIC,
    CONF_SEND_MODE,
    DEFAULT_ESPHOME_SERVICE,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    HCI_METHOD_HCITOOL,
    HCI_METHOD_MGMT,
    HCI_METHODS,
    MANUFACTURER_ID,
    PAIRABLE_MODELS,
    SCENE_QUERY_AC,
    SEND_MODE_ESPHOME_API,
    SEND_MODE_HCI,
    SEND_MODE_MQTT,
    SIGNAL_DEVICES_CHANGED,
    SIGNAL_DEVICE_UPDATE,
)

_LOGGER = logging.getLogger(__name__)

_NULL_MAC = "00:00:00:00:00:00"

# Durée pendant laquelle une trame binding est mémorisée en attente (secondes).
# Si start_learn est appelé dans cette fenêtre, le périphérique est enregistré
# sans que l'utilisateur ait à appuyer à nouveau sur le bouton.
_PENDING_BINDING_TTL = 60.0

# Durée pendant laquelle un module reste « à associer » après son ajout ou une
# demande de réassociation. Hors de cette fenêtre, ses trames binding sont
# ignorées : la trame pair étant une bascule, la renvoyer à un module déjà
# associé le désassocierait.
_AWAITING_PAIR_TTL = 30 * 60.0

# Une réponse d'association (codes 10 à 13) n'est attribuée à ce contrôleur que
# si elle suit de près une trame pair qu'il a lui-même envoyée.
_PAIR_REPLY_WINDOW = 30.0

# Résultats possibles d'un désappairage (voir start_unpair)
UNPAIR_UNPAIRED    = "unpaired"     # code 13 reçu, module supprimé
UNPAIR_PAIRED      = "paired"       # code 12 reçu : le module s'est associé, il est conservé
UNPAIR_NO_PRESS    = "no_press"     # aucun appui sur le bouton du module
UNPAIR_NO_ANSWER   = "no_answer"    # trame envoyée, pas de réponse : module conservé
UNPAIR_SEND_FAILED = "send_failed"  # la trame n'a pas pu être émise
UNPAIR_UNKNOWN     = "unknown"      # module inconnu


def _hci_is_operational(name: str) -> bool:
    """Teste si hcitool répond sur cette interface (synchrone, timeout 2 s).

    Échoue pour une interface absente comme pour une interface éteinte (DOWN).
    """
    import subprocess  # noqa: PLC0415
    try:
        result = subprocess.run(["hcitool", "-i", name, "dev"], capture_output=True, timeout=2)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _hciconfig_up(name: str) -> str:
    """Rallume une interface HCI (synchrone). Retourne le message d'erreur, vide si OK."""
    import subprocess  # noqa: PLC0415
    try:
        result = subprocess.run(["hciconfig", name, "up"], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as err:
        return str(err)
    if result.returncode != 0:
        return (result.stderr or result.stdout).decode(errors="ignore").strip() or "échec"
    return ""


class OdaceSFSPCoordinator:
    """Gère le cycle de vie BLE, l'envoi des trames et la persistance des modules.

    Trois modes d'envoi (voir l'en-tête du module) : contrôleur local (hcitool ou
    noyau), ESPHome API, MQTT.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        self.send_mode: str  = entry.data.get(CONF_SEND_MODE, SEND_MODE_HCI)
        self.dongle_mac: str = entry.data.get(CONF_MAC, "00:00:00:00:00:00")
        self.jeedom_key: str = entry.data.get(CONF_JEEDOM_KEY, "")
        # Copie indépendante : entry.data ne doit jamais être modifié en place, sinon
        # async_update_entry ne voit aucune différence et n'écrit rien sur disque.
        self.devices: Dict[str, Dict[str, Any]] = {
            uid: dict(dev) for uid, dev in entry.data.get(CONF_DEVICES, {}).items()
        }

        # Mode HCI — hci_name/hci_index ne sont pertinents qu'en SEND_MODE_HCI
        if self.send_mode == SEND_MODE_HCI:
            self.hci_name:  str = entry.data.get(CONF_HCI, "hci0")
            self.hci_index: int = hci_index_from_name(self.hci_name)
        else:
            self.hci_name  = ""
            self.hci_index = 0
        # Méthode d'émission locale : hcitool (défaut) ou canal de gestion du noyau
        self.hci_method: str = entry.data.get(CONF_HCI_METHOD, HCI_METHOD_HCITOOL)
        # MAC physique de l'adaptateur (distincte de dongle_mac, qui signe les trames)
        self.adapter_mac: str = entry.data.get(CONF_ADAPTER_MAC, "")

        # Mode MQTT
        self.mqtt_topic: str = entry.data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC)

        # Mode ESPHome API
        self.esphome_entry_id: str = entry.data.get(CONF_ESPHOME_ENTRY_ID, "")
        self.esphome_service:  str = entry.data.get(CONF_ESPHOME_SERVICE, DEFAULT_ESPHOME_SERVICE)

        self.learn_mode: bool = False
        self._learn_expires: float = 0
        self._unsub_bt = None
        # Dernier ordre envoyé — protection anti-boucle advertising
        self._last_command: Dict[str, Dict[str, Any]] = {}
        # Trames binding reçues hors mode apprentissage (mémorisées _PENDING_BINDING_TTL s)
        self._pending_bindings: Dict[str, Dict[str, Any]] = {}
        # UUIDs en attente de désappariage (bouton "Désappairer et supprimer")
        self._pending_unpair: set = set()
        # UUIDs pour lesquels la trame pair est en cours d'envoi ou envoyée, en attente
        # du code 13 (confirmation désappariage)
        self._pending_unpair_confirm: set = set()
        # Résultat attendu par l'écran de désappairage, par UUID
        self._unpair_futures: Dict[str, asyncio.Future] = {}
        self._unpair_confirm_timeout: float = 15.0
        # Dernière tentative de rallumage de l'interface HCI (voir _power_on_hci)
        self._last_power_on_attempt: float = 0.0
        # Modules « à associer » : UUID → échéance (voir _AWAITING_PAIR_TTL)
        self._awaiting_pair: Dict[str, float] = {}
        # Horodatage du dernier envoi de trame pair par UUID (anti-tempête re-pairing)
        self._last_pair_sent: Dict[str, float] = {}
        # Recherche de scènes en cours : UUID du module interrogé → échéance.
        # Les réponses « scene » ne sont exploitées que dans cette fenêtre.
        self._scene_discovery_until: Dict[str, float] = {}
        self._scenes_found: list[str] = []
        self._scenes_created: list[str] = []

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
        """Résout dynamiquement l'interface HCI si le dongle est sur un hciX inattendu.

        Teste via hcitool si l'interface configurée est opérationnelle.
        Gère le cas Linux/BlueZ où hci0 a la même MAC que hci1 mais est DOWN
        (interface fantôme avec MAC valide, pas uniquement MAC nulle).
        Gère aussi le cas où l'interface a disparu en cours de session (re-numérotation
        kernel lors d'un restart BlueZ ou d'un événement USB).

        Met à jour CONF_HCI dans la config entry quand une interface de remplacement
        est trouvée, afin que l'UI et les redémarrages futurs utilisent le bon nom.

        L'adaptateur est reconnu par sa MAC physique (CONF_ADAPTER_MAC), mémorisée
        la première fois que l'interface configurée répond. Cette méthode ne modifie
        pas dongle_mac (utilisée pour l'encodage CMAC des trames).
        """
        resolved, mac = await self.hass.async_add_executor_job(self._find_real_hci)
        if resolved is None:
            _LOGGER.error(
                "Odace SFSP — %s ne répond pas et aucune autre interface Bluetooth"
                " opérationnelle n'a été trouvée",
                self.hci_name,
            )
            return

        updates: Dict[str, Any] = {}
        if resolved != self.hci_name:
            _LOGGER.warning(
                "Odace SFSP — %s n'est pas opérationnelle (DOWN ou disparue)"
                " → basculement sur %s",
                self.hci_name, resolved,
            )
            self.hci_name = resolved
            self.hci_index = hci_index_from_name(resolved)
            # Persiste le nouveau nom dans la config entry — l'UI et les
            # redémarrages futurs utiliseront directement la bonne interface.
            updates[CONF_HCI] = resolved
        else:
            _LOGGER.debug(
                "_resolve_hci_by_mac : %s confirmée opérationnelle",
                self.hci_name,
            )
            # Première résolution réussie : mémoriser la MAC physique de l'adaptateur
            if not self.adapter_mac and mac:
                self.adapter_mac = mac
                updates[CONF_ADAPTER_MAC] = mac

        if updates:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, **updates},
            )

    def _find_real_hci(self) -> tuple[str | None, str]:
        """Test réel de l'interface HCI via hcitool (synchrone, executor).

        Contrairement à la lecture sysfs seule, cette méthode détecte les interfaces
        qui ont une MAC valide en sysfs mais dont la radio est DOWN (Network is down).
        C'est le cas typique du dongle USB qui s'enregistre sur hci1 mais crée aussi
        une entrée hci0 fantôme avec la même MAC.

        Retourne ``(nom, mac)`` :
        - l'interface configurée si elle répond ;
        - sinon une interface opérationnelle de remplacement ;
        - ``(None, "")`` si aucune interface ne répond.
        """
        import glob  # noqa: PLC0415

        _is_hci_operational = self._interface_operational

        def _sysfs_mac(name: str) -> str:
            try:
                with open(f"/sys/class/bluetooth/{name}/address") as fh:
                    mac = fh.read().strip().upper()
            except OSError:
                return ""
            return "" if mac == _NULL_MAC else mac

        # 1. Tester l'interface configurée
        if _is_hci_operational(self.hci_name):
            return self.hci_name, _sysfs_mac(self.hci_name)

        # 2. L'interface configurée ne répond pas → chercher une alternative.
        # MAC de référence : celle de l'adaptateur physique si elle est connue,
        # sinon la MAC de signature (identique sur une installation sans migration).
        wanted = (self.adapter_mac or self.dongle_mac or "").upper()
        fallback: tuple[str | None, str] = (None, "")

        for path in sorted(glob.glob("/sys/class/bluetooth/hci*/address")):
            hci_name = path.split("/")[-2]
            if hci_name == self.hci_name:
                continue  # Déjà testé
            mac = _sysfs_mac(hci_name)
            if not mac or not _is_hci_operational(hci_name):
                continue
            if wanted not in ("", _NULL_MAC) and mac == wanted:
                return hci_name, mac  # Même adaptateur physique → prioritaire
            if fallback[0] is None:
                fallback = (hci_name, mac)

        return fallback

    async def async_stop(self) -> None:
        if self._unsub_bt is not None:
            self._unsub_bt()
            self._unsub_bt = None
        # Libérer les écrans de désappairage encore en attente
        for uuid in list(self._unpair_futures):
            self._finish_unpair(uuid, UNPAIR_NO_PRESS)
        self._pending_unpair.clear()
        self._pending_unpair_confirm.clear()

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
                    # Désappairage demandé : une seule trame pair (bascule → désappairé),
                    # puis attente de la confirmation du module.
                    self._pending_unpair.discard(uuid)
                    _LOGGER.info("Désappariage %s : binding reçu → envoi de la trame pair", uuid)
                    self.hass.async_create_task(self._async_unpair_send(uuid))
                elif model in PAIRABLE_MODELS:
                    if not self.is_awaiting_pair(uuid):
                        # Module considéré comme associé : ne rien envoyer, la trame
                        # pair le désassocierait.
                        _LOGGER.debug(
                            "Binding de %s ignoré : module déjà associé"
                            " (« Réassocier un module » pour forcer)", uuid,
                        )
                    else:
                        # Debounce : évite d'envoyer plusieurs trames pair si plusieurs
                        # frames binding arrivent en rafale (bouton tenu, etc.)
                        _now = time.time()
                        if _now - self._last_pair_sent.get(uuid, 0.0) > 5.0:
                            self._last_pair_sent[uuid] = _now
                            _LOGGER.info("Module %s à associer : binding reçu → envoi pair", uuid)
                            self.hass.async_create_task(self.async_send_pair(uuid))
                        else:
                            _LOGGER.debug("Binding %s ignoré (debounce 5s)", uuid)
            return

        # ---- Trames d'advertisement ----
        if uuid not in self.devices:
            _LOGGER.debug("Frame from unknown device %s - ignored", uuid)
            return

        if not self.devices[uuid].get("mac"):
            self.devices[uuid]["mac"] = service_info.address

        # ---- Réponse à une recherche de scènes ----
        if result["data"].get("type") == "scene":
            if time.time() < self._scene_discovery_until.get(uuid, 0.0):
                self._register_scenes(result["data"])
            return

        paired_state = result["data"].get("paired")

        # Suivi du désappariage en attente de confirmation
        if uuid in self._pending_unpair_confirm and paired_state in ("unpaired", "paired", "ok"):
            self._pending_unpair_confirm.discard(uuid)
            if paired_state == "unpaired":
                # Code 13 : module bien désappairé → suppression, rien d'autre n'est attendu
                _LOGGER.info(
                    "Odace SFSP — désappariage confirmé (code 13) pour %s → suppression", uuid
                )
                self.hass.async_create_task(self._async_remove_unpaired(uuid))
            else:
                # Code 12 : le module n'était pas associé, la trame pair vient de
                # l'associer. Aucune seconde trame : il n'est plus en binding et ne
                # l'accepterait pas. Le module est conservé.
                _LOGGER.warning(
                    "Odace SFSP — %s s'est associé (code 12) au lieu de se désappairer :"
                    " module conservé, relancer le désappairage si nécessaire", uuid,
                )
                self._clear_awaiting_pair(uuid)
                self._finish_unpair(uuid, UNPAIR_PAIRED)
            return

        if paired_state:
            self._handle_pair_reply(uuid, paired_state)

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
        if model in PAIRABLE_MODELS:
            self.mark_awaiting_pair(uuid)
            self.hass.async_create_task(self.async_send_pair(uuid))
        self.hass.async_create_task(self._async_persist())

    # ------------------------------------------------------------------
    # Association : modules « à associer » et réponses du module
    # ------------------------------------------------------------------
    def mark_awaiting_pair(self, uuid: str) -> bool:
        """Marque un module « à associer » pendant _AWAITING_PAIR_TTL.

        Tant que la marque est posée, un appui sur le bouton du module (trame
        binding) déclenche l'envoi de la trame pair. Retourne False si le module
        est inconnu ou n'est pas d'un modèle commandable.
        """
        uuid = uuid.lower()
        device = self.devices.get(uuid)
        if device is None or device.get("model") not in PAIRABLE_MODELS:
            return False
        self._awaiting_pair[uuid] = time.time() + _AWAITING_PAIR_TTL
        return True

    def is_awaiting_pair(self, uuid: str) -> bool:
        expires = self._awaiting_pair.get(uuid.lower())
        if expires is None:
            return False
        if time.time() >= expires:
            self._awaiting_pair.pop(uuid.lower(), None)
            return False
        return True

    @callback
    def _clear_awaiting_pair(self, uuid: str) -> None:
        """Le module est associé : retirer la marque et l'alerte éventuelle."""
        self._awaiting_pair.pop(uuid, None)
        ir.async_delete_issue(self.hass, DOMAIN, f"module_unpaired_{uuid}")

    @callback
    def _handle_pair_reply(self, uuid: str, paired_state: str) -> None:
        """Traite la réponse d'un module à une trame pair (hors désappairage)."""
        if time.time() - self._last_pair_sent.get(uuid, 0.0) > _PAIR_REPLY_WINDOW:
            return  # réponse à un autre contrôleur : ne nous concerne pas
        if paired_state in ("paired", "ok"):
            _LOGGER.info("Odace SFSP — module %s associé (%s)", uuid, paired_state)
            self._clear_awaiting_pair(uuid)
        elif paired_state == "unpaired":
            # La trame pair a désassocié un module qui l'était déjà. Il reste « à
            # associer » : un nouvel appui sur son bouton le réassociera.
            _LOGGER.warning(
                "Odace SFSP — module %s désassocié (code 13) : appuyer à nouveau sur"
                " son bouton pour le réassocier", uuid,
            )
            self.mark_awaiting_pair(uuid)
            ir.async_create_issue(
                self.hass, DOMAIN, f"module_unpaired_{uuid}",
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="module_unpaired",
                translation_placeholders={
                    "name": self.devices.get(uuid, {}).get("name", uuid),
                    "uuid": uuid,
                },
            )
        elif paired_state == "denied":
            _LOGGER.warning("Odace SFSP — module %s : association refusée (code 10)", uuid)

    @callback
    def _register_scenes(self, data: Dict[str, Any]) -> None:
        """Crée les scènes listées par un module (même logique que Jeedom).

        Une scène n'est pas un module : c'est un identifiant de 4 octets
        (8 caractères hex) mémorisé par les modules. ``ffffffff`` = emplacement vide.
        """
        scene_type = data.get("subtype", "custom")
        created = False
        for scene_uuid in data.get("scenes", []):
            scene_uuid = scene_uuid.lower()
            if scene_uuid == "ffffffff" or len(scene_uuid) != 8:
                continue
            if scene_uuid not in self._scenes_found:
                self._scenes_found.append(scene_uuid)
            if scene_uuid in self.devices:
                continue
            self.devices[scene_uuid] = {
                "uuid": scene_uuid, "mac": "", "model": "scene",
                "type": scene_type, "name": f"Odace SFSP scene {scene_uuid}",
            }
            self._scenes_created.append(scene_uuid)
            created = True
            _LOGGER.info("Odace SFSP — scène %s (%s) ajoutée", scene_uuid, scene_type)
        if created:
            async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)
            self.hass.async_create_task(self._async_persist())

    async def async_discover_scenes(
        self, uuid: str, scene_types: list[str], wait: float = 2.0
    ) -> Dict[str, list[str]]:
        """Demande à un module la liste de ses scènes et crée celles qui manquent.

        Retourne ``{"found": [...], "created": [...]}`` (identifiants de scènes).
        Lève HomeAssistantError si la demande n'a pas pu être émise.
        """
        uuid = uuid.lower()
        self._scenes_found = []
        self._scenes_created = []
        try:
            for scene_type in scene_types:
                self._scene_discovery_until[uuid] = time.time() + wait + 1.0
                await self.async_send_command(uuid, SCENE_QUERY_AC[scene_type])
                await asyncio.sleep(wait)
        finally:
            self._scene_discovery_until.pop(uuid, None)
        return {"found": list(self._scenes_found), "created": list(self._scenes_created)}

    async def async_add_device(self, device: Dict[str, Any]) -> None:
        uuid = device["uuid"].lower()
        self.devices[uuid] = {**device, "uuid": uuid}
        await self._async_persist()
        async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)

    async def async_remove_device(self, uuid: str) -> None:
        uuid = uuid.lower()
        self.devices.pop(uuid, None)
        self._awaiting_pair.pop(uuid, None)
        ir.async_delete_issue(self.hass, DOMAIN, f"module_unpaired_{uuid}")
        await self._async_persist()
        # Supprimer le device (et ses entités) du registre HA
        dev_reg = dr.async_get(self.hass)
        device  = dev_reg.async_get_device_by_identifier((DOMAIN, uuid), self.entry.entry_id)
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
            device  = dev_reg.async_get_device_by_identifier((DOMAIN, uuid), self.entry.entry_id)
            if device:
                dev_reg.async_update_device(device.id, name=updates["name"])
        async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)

    async def async_add_devices(self, devices: list[Dict[str, Any]]) -> None:
        """Ajoute plusieurs modules : une seule persistance et un seul signal."""
        for device in devices:
            uuid = device["uuid"].lower()
            self.devices[uuid] = {**device, "uuid": uuid}
        await self._async_persist()
        async_dispatcher_send(self.hass, SIGNAL_DEVICES_CHANGED)

    async def _async_persist(self) -> None:
        # Copie : entry.data doit recevoir un objet distinct de self.devices, sinon
        # les modifications suivantes seraient faites « en place » et HA, ne voyant
        # aucune différence, ne les écrirait pas sur disque.
        devices = {uid: dict(dev) for uid, dev in self.devices.items()}
        new_data = {**self.entry.data, CONF_DEVICES: devices}
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    # ------------------------------------------------------------------
    # Send — aiguillage HCI ou ESP32/MQTT
    # ------------------------------------------------------------------
    async def _dispatch_send(self, payload: str) -> bool:
        """Envoie le payload via le mode configuré. Retourne False si l'envoi a échoué."""
        if not payload:
            return False
        if self.send_mode == SEND_MODE_MQTT:
            return await async_send_mqtt(self.hass, self.mqtt_topic, payload)
        if self.send_mode == SEND_MODE_ESPHOME_API:
            return await async_send_esphome_api(
                self.hass, self.esphome_entry_id, self.esphome_service, payload
            )
        # Avant chaque envoi HCI, vérifier que l'interface existe encore dans
        # sysfs.  Le kernel Linux peut re-numéroter l'adaptateur BT (ex. hci1 →
        # hci0) lors d'un restart BlueZ ou d'un événement USB, sans que HA ne
        # soit relancé.  Détection légère (stat sysfs) et re-résolution si
        # besoin, pour éviter les "Invalid device: No such device" silencieux.
        import os  # noqa: PLC0415
        if not os.path.exists(f"/sys/class/bluetooth/{self.hci_name}"):
            _LOGGER.warning(
                "Odace SFSP — %s n'est plus présente dans sysfs, re-résolution...",
                self.hci_name,
            )
            await self._resolve_hci_by_mac()
        if await hci_send(self.hci_index, payload, self.hci_method):
            return True
        # Échec d'émission : l'interface peut exister dans sysfs tout en étant DOWN
        # (« Network is down »), par exemple quand l'adaptateur a été renuméroté et
        # qu'il reste une entrée fantôme. Re-tester l'interface et, si une autre
        # répond, réessayer une fois dessus.
        previous = self.hci_name
        await self._resolve_hci_by_mac()
        if self.hci_name != previous:
            return await hci_send(self.hci_index, payload, self.hci_method)
        # Aucune autre interface : si la nôtre est éteinte, tenter de la rallumer.
        if await self.hass.async_add_executor_job(self._power_on_hci):
            return await hci_send(self.hci_index, payload, self.hci_method)
        return False

    def _interface_operational(self, name: str) -> bool:
        """L'interface existe-t-elle et répond-elle ? (synchrone, executor)

        Avec la méthode d'émission « noyau », l'état est lu par le canal de
        gestion, sans hcitool. hcitool ne sert que de secours si ce canal est
        inutilisable, ou quand c'est la méthode d'émission configurée.
        """
        if self.hci_method == HCI_METHOD_MGMT:
            from .mgmt import controller_is_powered  # noqa: PLC0415
            powered = controller_is_powered(hci_index_from_name(name))
            if powered is not None:
                return powered
        return _hci_is_operational(name)

    def _interface_power_on(self, name: str) -> str:
        """Rallume l'interface. Retourne le message d'erreur, vide si OK. (synchrone)"""
        if self.hci_method == HCI_METHOD_MGMT:
            from .mgmt import controller_power_on  # noqa: PLC0415
            error = controller_power_on(hci_index_from_name(name))
            if not error:
                return ""
            _LOGGER.debug("Odace SFSP — rallumage de %s par le noyau refusé (%s), essai hciconfig", name, error)
        return _hciconfig_up(name)

    def _power_on_hci(self) -> bool:
        """Rallume l'interface configurée si elle est éteinte (synchrone, executor).

        Retourne True si l'interface était éteinte et répond de nouveau. Une seule
        tentative par minute, pour ne pas relancer le rallumage à chaque commande
        quand l'interface refuse de s'allumer.
        """
        import os  # noqa: PLC0415
        name = self.hci_name
        if not os.path.exists(f"/sys/class/bluetooth/{name}"):
            return False  # interface absente : rien à rallumer
        if self._interface_operational(name):
            return False  # elle répond : l'échec d'émission a une autre cause
        now = time.time()
        if now - self._last_power_on_attempt < 60.0:
            return False
        self._last_power_on_attempt = now
        _LOGGER.warning("Odace SFSP — %s est éteinte (DOWN) → tentative de rallumage", name)
        error = self._interface_power_on(name)
        if error:
            _LOGGER.error("Odace SFSP — impossible de rallumer %s : %s", name, error)
            return False
        time.sleep(1.0)  # laisse le contrôleur terminer son initialisation
        if self._interface_operational(name):
            _LOGGER.warning("Odace SFSP — %s rallumée, la commande est rejouée", name)
            return True
        _LOGGER.error("Odace SFSP — %s ne répond toujours pas après rallumage", name)
        return False

    async def async_send_command(self, uuid: str, ac: str, options: Optional[int] = None) -> None:
        """Envoie une commande à un module.

        Lève HomeAssistantError si le module est inconnu ou si la trame n'a pas pu
        être émise : l'appelant (entité, service) ne doit alors pas considérer la
        commande comme partie.
        """
        uuid   = uuid.lower()
        device = self.devices.get(uuid)
        if device is None:
            raise HomeAssistantError(f"Odace SFSP : module {uuid} inconnu")
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
        if not await self._dispatch_send(payload):
            raise HomeAssistantError(
                f"Odace SFSP : la commande {ac} n'a pas pu être envoyée au module {uuid}"
                " (voir les journaux)"
            )
        self._last_command[uuid] = {"ac": ac, "ts": time.time()}
        # La prochaine annonce du module doit passer le filtre anti-doublon, même si
        # elle est identique à la précédente : c'est elle qui confirme (ou dément)
        # l'état affiché de façon optimiste par l'entité.
        forget_dedupe(uuid)
        _LOGGER.info("Odace SFSP TX [%s] uuid=%s ac=%s options=%s", self.send_mode, uuid, ac, options)

    async def async_test_send(
        self, uuid: str, ac: str, method: str | None, duration: float, interval_ms: int,
    ) -> Dict[str, Any]:
        """Diagnostic — envoie une commande et retourne ce qui a été émis.

        Utilisé par le service ``test_send``. Même trame et même signature que
        ``async_send_command``. ``method`` choisit le moyen d'émission (par
        défaut celui configuré). Le compte rendu contient la trace HCI : les
        commandes réellement passées à l'adaptateur. L'état de l'entité n'est pas
        modifié : c'est l'annonce du module qui le mettra à jour.
        """
        from .mgmt import HciTrace, async_send_mgmt_report  # noqa: PLC0415
        from .sender import _get_hci_lock  # noqa: PLC0415

        uuid   = uuid.lower()
        device = self.devices.get(uuid)
        if device is None:
            raise HomeAssistantError(f"Odace SFSP : module {uuid} inconnu")
        if self.send_mode != SEND_MODE_HCI:
            raise HomeAssistantError(
                "Odace SFSP : ce diagnostic ne concerne que le mode contrôleur Bluetooth local"
            )
        method = method if method in HCI_METHODS else self.hci_method
        device_for_payload: Dict[str, Any] = {"uuid": device["uuid"].upper(), "model": device["model"]}
        if device.get("type"):
            device_for_payload["type"] = device["type"]
        payload = craft_payload(
            device_for_payload, "advertisement", self.jeedom_key, self.dongle_mac, {"ac": ac},
        )
        if not payload:
            raise HomeAssistantError("Odace SFSP : trame impossible à construire (voir les journaux)")
        forget_dedupe(uuid)  # la réponse du module doit remonter même si l'état est inchangé

        trace = HciTrace(self.hci_index)
        trace.start()
        try:
            if method == HCI_METHOD_HCITOOL:
                # Chemin normal, sans repli (il prend lui-même le verrou)
                report: Dict[str, Any] = {"success": await hci_send(self.hci_index, payload, HCI_METHOD_HCITOOL)}
            else:
                # Canal de gestion seul, sans repli sur hcitool, pour voir son résultat propre
                async with _get_hci_lock(self.hci_index):
                    report = await async_send_mgmt_report(
                        self.hci_index, payload, duration=duration, interval_ms=interval_ms,
                    )
            await asyncio.sleep(0.2)  # laisse arriver les dernières commandes (arrêt de l'annonce)
        finally:
            hci_trace = trace.stop()
        report.update({
            "adapter": self.hci_name, "method": method, "configured_method": self.hci_method,
            "uuid": uuid, "ac": ac, "payload": payload, "hci_trace": hci_trace,
        })
        return report

    async def async_send_pair(self, uuid: str) -> bool:
        """Envoie la trame de pairing à un périphérique commandable.

        Applicable aux modèles : dcl, shutter, plug, generic.
        Les switches (réception seule) n'ont pas de mécanisme de pairing.
        La trame est une bascule : elle associe un module libre et désassocie un
        module déjà associé. Retourne True si la trame a été émise.
        """
        uuid   = uuid.lower()
        device = self.devices.get(uuid)
        if device is None:
            _LOGGER.error("async_send_pair: device %s inconnu", uuid)
            return False
        payload = craft_payload(
            {"uuid": device["uuid"].upper(), "model": device["model"]},
            "pair", self.jeedom_key, self.dongle_mac,
        )
        if not payload:
            _LOGGER.error("async_send_pair: payload vide pour uuid=%s", uuid)
            return False
        self._last_pair_sent[uuid] = time.time()
        # La réponse du module (codes 12/13) ne doit pas être écartée comme doublon
        forget_dedupe(uuid)
        if not await self._dispatch_send(payload):
            _LOGGER.error("Odace SFSP PAIR [%s] : échec de l'envoi → uuid=%s", self.send_mode, uuid)
            return False
        _LOGGER.info("Odace SFSP PAIR [%s] envoyé → uuid=%s", self.send_mode, uuid)
        return True

    def start_unpair(
        self, uuid: str, press_timeout: float = 60.0, confirm_timeout: float = 15.0
    ) -> asyncio.Future:
        """Lance le désappairage d'un module et retourne un Future portant son résultat.

        Déroulement :
        1. attente d'un appui sur le bouton du module (trame binding), ``press_timeout`` s ;
        2. envoi d'une seule trame pair (bascule → désappairé) ;
        3. attente de la réponse du module, ``confirm_timeout`` s.

        Le module n'est supprimé de HA que s'il confirme (code 13). Dans tous les
        autres cas il est conservé. Résultat : une des constantes ``UNPAIR_*``.
        """
        uuid = uuid.lower()
        future: asyncio.Future = self.hass.loop.create_future()
        if uuid not in self.devices:
            _LOGGER.error("start_unpair: device %s inconnu", uuid)
            future.set_result(UNPAIR_UNKNOWN)
            return future

        # Un désappairage déjà en cours pour ce module est remplacé
        self._finish_unpair(uuid, UNPAIR_NO_PRESS)
        self._unpair_futures[uuid] = future
        self._pending_unpair_confirm.discard(uuid)
        self._pending_unpair.add(uuid)
        self._unpair_confirm_timeout = confirm_timeout
        _LOGGER.info("Odace SFSP — désappariage en attente pour %s (%.0fs)", uuid, press_timeout)

        async def _cancel_after_timeout() -> None:
            await asyncio.sleep(press_timeout)
            if uuid in self._pending_unpair and self._unpair_futures.get(uuid) is future:
                self._pending_unpair.discard(uuid)
                _LOGGER.info("Odace SFSP — désappariage annulé (pas d'appui) pour %s", uuid)
                self._finish_unpair(uuid, UNPAIR_NO_PRESS)

        self.hass.async_create_task(_cancel_after_timeout())
        return future

    @callback
    def _finish_unpair(self, uuid: str, result: str) -> None:
        """Communique le résultat du désappairage à l'écran qui l'attend."""
        future = self._unpair_futures.pop(uuid, None)
        if future is not None and not future.done():
            future.set_result(result)

    async def _async_unpair_send(self, uuid: str) -> None:
        """Envoie la trame pair du désappairage et attend la réponse du module."""
        uuid = uuid.lower()
        future = self._unpair_futures.get(uuid)
        # Guetter la réponse AVANT l'envoi : le module peut répondre pendant les
        # ~0,5 s que dure l'émission.
        self._pending_unpair_confirm.add(uuid)
        if not await self.async_send_pair(uuid):
            self._pending_unpair_confirm.discard(uuid)
            self._finish_unpair(uuid, UNPAIR_SEND_FAILED)
            return
        _LOGGER.info(
            "Odace SFSP — trame pair envoyée pour %s, attente confirmation désappariage (code 13)", uuid
        )
        await asyncio.sleep(self._unpair_confirm_timeout)
        if uuid in self._pending_unpair_confirm and self._unpair_futures.get(uuid) is future:
            self._pending_unpair_confirm.discard(uuid)
            _LOGGER.warning(
                "Odace SFSP — pas de confirmation de désappariage pour %s : module conservé", uuid,
            )
            self._finish_unpair(uuid, UNPAIR_NO_ANSWER)

    async def _async_remove_unpaired(self, uuid: str) -> None:
        """Supprime un module dont le désappairage est confirmé."""
        await self.async_remove_device(uuid)
        self._finish_unpair(uuid, UNPAIR_UNPAIRED)

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
