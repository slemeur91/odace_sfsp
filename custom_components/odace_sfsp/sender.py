"""Génération et envoi de trames BLE Beagle.

Deux modes d'envoi sont disponibles :

- ``async_send`` : envoi via ``hcitool`` sur un contrôleur HCI local
  (dongle USB sur HAOS, Proxmox, etc.). Utilisé quand CONF_SEND_MODE == "hci".

- ``async_send_mqtt`` : publication sur un topic MQTT, l'ESP32 souscrit
  et diffuse le paquet BLE advertising. Utilisé quand CONF_SEND_MODE == "mqtt".

La construction et le chiffrement des trames (build_frame, craft_payload)
sont communs aux deux modes.
"""
from __future__ import annotations

import asyncio
import binascii
import logging
import secrets
from typing import Any, Dict

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import cmac
from cryptography.hazmat.primitives.ciphers import algorithms

from .const import (
    AC,
    CFTARGET,
    GATEWAY,
    HEADER_FS,
    HEADER_VV,
    SCENES,
    TYPES,
    UNIQUE_HEADER,
    UNIQUE_KEY,
    UUID_CONTROLLER,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Construction de trame (commune aux deux modes d'envoi)
# ---------------------------------------------------------------------------

def _random_counter() -> str:
    return binascii.hexlify(secrets.token_bytes(2)).decode().upper()


def build_frame(device: Dict[str, Any], frame_type: str, jeedom_key: str, data: Any = "") -> str:
    """Recrée la trame binaire avant chiffrement."""
    param = "FF"
    target_uuid = device["uuid"]
    cf_model = CFTARGET[device["model"]]
    header = UNIQUE_HEADER + TYPES["gateway"] + HEADER_VV + HEADER_FS

    if frame_type == "pair":
        _LOGGER.debug("Building pairing data with key %s", jeedom_key)
        payload = GATEWAY["binding"] + UUID_CONTROLLER + str(jeedom_key)
    else:
        data_ac = AC[data["ac"]]
        if device["model"] == "scene":
            param = SCENES[device.get("type", "custom")]
        elif device["model"].startswith("group"):
            param = "FB"
        else:
            target_uuid = "FF" + target_uuid
        if "options" in data:
            param = hex(100 - int(data["options"]))[2:]
        payload = (
            GATEWAY["advertisement"]
            + UUID_CONTROLLER
            + "01"
            + data_ac
            + cf_model
            + target_uuid
            + param
            + "FFFF"
        )
        payload = payload + _random_counter()
    return header + payload


def _compute_buffer(dongle_mac: str, trame: str) -> bytes:
    mac = "".join(
        reversed(
            [
                dongle_mac.replace(":", "")[i : i + 2]
                for i in range(0, len(dongle_mac.replace(":", "")), 2)
            ]
        )
    ).lower()
    replaced = trame[22:30] + "FF" + trame[32:]
    payload = replaced.replace(" ", "").lower()
    _LOGGER.debug("Mac payload is %s%s", mac, payload)
    return binascii.unhexlify(mac + payload)


def _cmac_hash(secret: str, buffer: bytes) -> str:
    c = cmac.CMAC(algorithms.AES(binascii.unhexlify(secret)), backend=default_backend())
    c.update(buffer)
    return binascii.hexlify(bytearray(c.finalize())).decode()


def craft_payload(
    device: Dict[str, Any],
    frame_type: str,
    jeedom_key: str,
    dongle_mac: str,
    data: Any = "",
) -> str:
    """Construit la trame finale (62 caractères hex).

    ``dongle_mac`` est la MAC du contrôleur BLE :
    - Mode HCI  : MAC du dongle USB (lue depuis sysfs/hciconfig)
    - Mode ESP32 : MAC Bluetooth de l'ESP32 (saisie dans le config flow)
    """
    frame = build_frame(device, frame_type, jeedom_key, data)
    buffer = _compute_buffer(dongle_mac, frame)
    if frame_type == "pair":
        key = UNIQUE_KEY.replace(" ", "").lower()
    else:
        key = (GATEWAY["binding"] + UUID_CONTROLLER + str(jeedom_key)).replace(" ", "").lower()
    hashed = _cmac_hash(key, buffer)
    cmac_short = hashed[:8]
    payload = (frame + cmac_short).upper()
    _LOGGER.debug("Final payload is %s", payload)
    return payload


def validate_payload(payload: str) -> bool:
    """Vérifie la longueur et le header de la trame finale."""
    p = payload.replace(" ", "")
    if len(p) != 62:
        _LOGGER.debug("Invalid length %d (expected 62)", len(p))
        return False
    expected = UNIQUE_HEADER + TYPES["gateway"] + HEADER_VV + HEADER_FS
    if p[:22].lower() != expected.lower():
        _LOGGER.debug("Invalid header %s (expected %s)", p[:22], expected)
        return False
    return True


# ---------------------------------------------------------------------------
# Mode HCI — envoi via hcitool (dongle USB local)
# ---------------------------------------------------------------------------

async def async_send(hci_index: int, payload: str) -> bool:
    """Envoie la trame via ``hcitool`` sur le contrôleur ``hciX``.

    Après l'envoi, le scan passif est restauré explicitement au niveau HCI pour
    resynchroniser l'état hardware avec BlueZ.
    """
    if not validate_payload(payload):
        return False
    payload_spaced = " ".join(payload[i : i + 2] for i in range(0, len(payload), 2)).upper()
    _LOGGER.info("Send to BLE [HCI hci%d]: %s", hci_index, payload_spaced)
    cmds = [
        f"hcitool -i hci{hci_index} cmd 0x08 0x0008 1F {payload_spaced}",
        f"hcitool -i hci{hci_index} cmd 0x08 0x0006 A0 00 A0 00 03 00 00 00 00 00 00 00 00 07 00",
        f"hcitool -i hci{hci_index} cmd 0x08 0x000a 01",
    ]
    for cmd in cmds:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            _LOGGER.warning("hcitool failed (%s): %s", cmd, err.decode(errors="ignore"))
    await asyncio.sleep(0.5)

    # Désactive l'advertising
    proc = await asyncio.create_subprocess_shell(
        f"hcitool -i hci{hci_index} cmd 0x08 0x000a 00",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        _LOGGER.warning("hcitool disable adv failed: %s", err.decode(errors="ignore"))

    # Restaure le scan passif LE
    restore_cmds = [
        f"hcitool -i hci{hci_index} cmd 0x08 0x000b 00 10 00 10 00 00 00",
        f"hcitool -i hci{hci_index} cmd 0x08 0x000c 01 00",
    ]
    for cmd in restore_cmds:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            _LOGGER.debug("hcitool scan restore failed (%s): %s", cmd, err.decode(errors="ignore"))

    return True


# ---------------------------------------------------------------------------
# Mode ESP32 — envoi via MQTT
# ---------------------------------------------------------------------------

async def async_send_mqtt(hass, topic: str, payload: str) -> bool:
    """Publie la trame BLE sur un topic MQTT pour qu'un ESP32 la diffuse.

    L'ESP32 (ESPHome) souscrit à ``topic``, reçoit le payload hex (62 chars)
    et le diffuse comme un paquet BLE advertising non-connectable.

    Retourne True si la publication a réussi, False sinon.
    """
    if not validate_payload(payload):
        _LOGGER.warning("Payload invalide, envoi MQTT annulé : %s", payload)
        return False

    payload_clean = payload.replace(" ", "").upper()

    try:
        from homeassistant.components import mqtt as ha_mqtt

        if not await ha_mqtt.async_wait_for_mqtt_client(hass):
            _LOGGER.error(
                "Client MQTT non disponible — vérifier la configuration MQTT dans HA"
            )
            return False

        await ha_mqtt.async_publish(hass, topic, payload_clean, qos=1, retain=False)
        _LOGGER.info("Send to BLE [ESP32 MQTT %s]: %s", topic, payload_clean)
        return True

    except Exception as err:
        _LOGGER.error("Erreur lors de la publication MQTT : %s", err)
        return False


# ---------------------------------------------------------------------------
# Utilitaires HCI
# ---------------------------------------------------------------------------

def hci_index_from_name(name: str) -> int:
    """``hci0`` → 0, ``hci2`` → 2. Retourne 0 pour les noms non-HCI (ESP32)."""
    try:
        return int(name.replace("hci", "").strip() or 0)
    except ValueError:
        return 0


def read_controller_mac(hci_name: str) -> str | None:
    """Lit la MAC d'un contrôleur Bluetooth (lecture synchrone).

    Ordre de tentatives :
    1. sysfs ``/sys/class/bluetooth/<hci>/address`` (sans outil externe)
    2. ``hciconfig <hci>`` (fallback si sysfs indisponible)
    """
    # 1. sysfs
    try:
        sysfs = f"/sys/class/bluetooth/{hci_name}/address"
        with open(sysfs) as fh:
            mac = fh.read().strip().upper()
        if mac and mac != "00:00:00:00:00:00":
            _LOGGER.debug("read_controller_mac %s via sysfs : %s", hci_name, mac)
            return mac
    except Exception:
        pass

    # 2. hciconfig
    try:
        import subprocess
        out = subprocess.check_output(
            ["hciconfig", hci_name], stderr=subprocess.STDOUT
        ).decode()
        for line in out.splitlines():
            line = line.strip()
            if "BD Address:" in line:
                return line.split("BD Address:")[1].split()[0].strip()
    except Exception as err:
        _LOGGER.debug("read_controller_mac hciconfig failed: %s", err)

    return None
