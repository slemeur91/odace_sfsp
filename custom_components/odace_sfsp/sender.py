"""Génération et envoi de trames BLE Beagle (port de sendadv.py).

L'envoi utilise ``hcitool`` (identique au daemon Jeedom) car il nécessite une
écriture directe de la charge ``Set Advertising Data`` que ``bleak`` ne sait
pas produire. Sur HAOS / Supervised, HA tourne en root dans le container :
``sudo`` n'existe pas et n'est pas nécessaire — les commandes ``hcitool``
sont appelées directement.
"""
from __future__ import annotations

import asyncio
import binascii
import logging
import os
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


def craft_payload(device: Dict[str, Any], frame_type: str, jeedom_key: str, dongle_mac: str, data: Any = "") -> str:
    """Construit la trame finale (62 caractères hex)."""
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


async def async_send(hci_index: int, payload: str) -> bool:
    """Envoie la trame via ``hcitool`` sur le contrôleur ``hciX``.

    Après l'envoi, le scan passif est restauré explicitement au niveau HCI pour
    resynchroniser l'état hardware avec BlueZ (qui ne voit pas les commandes
    hcitool raw et resterait sinon dans un état incohérent).
    """
    if not validate_payload(payload):
        return False
    payload_spaced = " ".join(payload[i : i + 2] for i in range(0, len(payload), 2)).upper()
    _LOGGER.info("Send to BLE: %s", payload_spaced)
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

    # Désactive l'advertising — attendu explicitement avant de restaurer le scan
    proc = await asyncio.create_subprocess_shell(
        f"hcitool -i hci{hci_index} cmd 0x08 0x000a 00",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        _LOGGER.warning("hcitool disable adv failed: %s", err.decode(errors="ignore"))

    # Restaure le scan passif LE pour resynchroniser l'état hardware avec BlueZ.
    # Sans cela, BlueZ croit toujours que le scan est actif alors que le hardware
    # l'a suspendu pendant la phase d'advertising, ce qui provoque des erreurs
    # "No discovery started" et des tentatives de recovery sur le dongle.
    restore_cmds = [
        # HCI_LE_Set_Scan_Parameters : passif, interval 10 ms, window 10 ms
        f"hcitool -i hci{hci_index} cmd 0x08 0x000b 00 10 00 10 00 00 00",
        # HCI_LE_Set_Scan_Enable : enable, no duplicate filter
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


def hci_index_from_name(name: str) -> int:
    """``hci0`` -> 0, ``hci2`` -> 2."""
    try:
        return int(name.replace("hci", "").strip() or 0)
    except ValueError:
        return 0


def read_controller_mac(hci_name: str) -> str | None:
    """Lit la MAC d'un contrôleur via ``hciconfig`` (lecture synchrone)."""
    try:
        import subprocess

        out = subprocess.check_output(["hciconfig", hci_name], stderr=subprocess.STDOUT).decode()
        for line in out.splitlines():
            line = line.strip()
            if "BD Address:" in line:
                return line.split("BD Address:")[1].split()[0].strip()
    except Exception as err:  # pragma: no cover
        _LOGGER.debug("read_controller_mac failed: %s", err)
    return None
