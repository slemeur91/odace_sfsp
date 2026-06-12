"""Parseur de trames BLE Beagle.

Port fidèle du fichier ``resources/beagled/beagle.py`` du plugin Jeedom.

Le parseur peut être utilisé de deux façons :

* ``parse_trame(trame_hex, mac)`` : parse la trame complète telle que reçue
  par l'ancien daemon Jeedom (pybluez). La trame contient toute la payload
  advertising (à partir de ``0201041b...`` ou ``0201061b...``).

* ``parse_manufacturer_data(mfg_hex, mac)`` : parse uniquement les bytes de
  manufacturer-data (sans le préfixe flags / length / type). C'est la forme
  renvoyée par l'intégration ``bluetooth`` de Home Assistant qui fournit un
  ``BluetoothServiceInfoBleak`` avec ``manufacturer_data[0x02B6]``.

Les deux fonctions retournent le même dictionnaire normalisé si la trame est
reconnue, sinon ``None``. Elles produisent également un log de trace au
format Jeedom (``This is a DCL with UUID ... state is ON ...``) via le logger
``custom_components.beagle.parser``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

_LOGGER = logging.getLogger(__name__)

# Mémoire de déduplication (identique à globals.lastevent / globals.lastdata)
_lastevent: Dict[str, str] = {}
_lastdata: Dict[str, dict] = {}


def _reconstruct_full_trame(mfg_hex: str) -> str:
    """Recrée une trame au format ``0201061bffb602<mfg>`` compatible avec l'ancien parseur.

    ``mfg_hex`` est la partie renvoyée par BlueZ/bleak pour le manufacturer
    data Schneider (0x02B6) sans les 2 octets du company-id. On fixe le
    préfixe au format utilisé historiquement ; il sera ignoré si la trame
    décodée est trop courte.
    """
    mfg_hex = mfg_hex.lower()
    return "0201061bffb602" + mfg_hex


def parse_manufacturer_data(mfg_hex: str, mac: str) -> Optional[Dict[str, Any]]:
    """Parse des bytes manufacturer-data hexadecimal (sans company id)."""
    return parse_trame(_reconstruct_full_trame(mfg_hex), mac)


def parse_trame(trame: str, mac: str) -> Optional[Dict[str, Any]]:
    """Parse une trame advertising complète (même logique que Jeedom)."""
    try:
        trame = trame.lower()
        if trame[0:14] not in ("0201041bffb602", "0201061bffb602"):
            return None

        datatrame = trame[22:]
        uuid = datatrame[2:8]
        # Normalisation : on neutralise l'octet de répétition pour la
        # déduplication (globals.lastevent = cleanedtrame).
        cleaned = trame[0:30] + "00" + trame[32:]
        ignore = uuid in _lastevent and _lastevent[uuid] == cleaned
        _lastevent[uuid] = cleaned

        result: Dict[str, Any] = {
            "mac": mac,
            "uuid": uuid,
            "data": {},
        }
        string = "Beagle found "
        dtype = trame[14:18]
        cf = datatrame[:2]

        # Bit de répétition (octet 30:32)
        repetition_bits = bin(int(trame[30:32], 16))[2:].rjust(8, "0")
        repeat = False
        if repetition_bits[1:2] == "0" and repetition_bits[2:4] != "00":
            repeat = True
        elif repetition_bits[1:2] == "1" and repetition_bits[2:4] != "11":
            repeat = True
        if repeat:
            string += " (repeated data) "

        if dtype == "8e44":
            string = _parse_switch(trame, cf, uuid, string, result)
        elif dtype == "9844":
            string = _parse_dcl(trame, cf, uuid, string, result)
        elif dtype == "8f44":
            string = _parse_shutter(trame, cf, uuid, string, result)
        elif dtype == "9244":
            string = _parse_generic(trame, cf, uuid, string, result)
        elif dtype == "9044":
            string = _parse_plug(trame, cf, uuid, string, result)
        elif dtype == "9144":
            string = _parse_dimmer(trame, cf, uuid, string, result)
        elif dtype == "a244":
            # Trame émise par HA (type gateway/contrôleur) captée en retour par
            # le bluetooth_proxy ESPHome — boucle de feedback normale, ignorer
            # silencieusement.
            return None
        else:
            _LOGGER.debug("Unknown type %s", dtype)
            return None

        # Doublon strict : mêmes données qu'avant, et ce n'est pas un binding
        data = result["data"]
        if ignore and data.get("type") != "binding":
            return None
        if (
            result.get("model") != "switch"
            and data.get("type") != "binding"
            and _lastdata.get(uuid) == data
        ):
            return None
        _lastdata[uuid] = dict(data)

        _LOGGER.debug(trame)
        _LOGGER.debug(string)
        return result
    except Exception as err:  # pragma: no cover - parité avec Jeedom
        _LOGGER.debug("parse error: %s", err)
        return None


# ---------------------------------------------------------------------------
# Handlers par type
# ---------------------------------------------------------------------------

def _parse_switch(trame: str, cf: str, uuid: str, string: str, result: dict) -> str:
    result["model"] = "switch"
    string += f"This is a switch with UUID {uuid}"
    data = result["data"]
    if cf == "00":
        data["type"] = "advertisement"
        string += " advertisement"
        data["firmware"] = trame[34:40]
        actions = {
            "00": ("0", "Off", "off"),
            "01": ("1", "On", "on"),
            "02": ("2", "Toggle", "toggle"),
            "03": ("3", "Dim Up", "dim up"),
            "04": ("4", "Dim Down", "dim down"),
            "05": ("5", "Haut", "up"),
            "06": ("6", "Bas", "down"),
            "07": ("7", "Stop", "stop"),
            "08": ("8", "Scene User", "scene user"),
            "09": ("9", "Scene In", "scene in"),
            "0a": ("10", "Scene Out", "scene out"),
        }
        code = trame[32:34]
        if code in actions:
            value, label, action = actions[code]
            data["value"] = value
            data["label"] = label
            string += f" action is {action}"
    elif cf == "01":
        data["type"] = "binding"
        string += " binding"
    return string


def _parse_dcl(trame: str, cf: str, uuid: str, string: str, result: dict) -> str:
    result["model"] = "dcl"
    string += f"This is a DCL with UUID {uuid}"
    data = result["data"]
    if cf == "10":
        data["type"] = "advertisement"
        string += " advertisement"
        data["firmware"] = trame[58:62]
        state_code = trame[32:34]
        if state_code == "01":
            data["value"], data["label"] = "1", "Allumé"
            string += " state is ON"
        elif state_code == "00":
            data["value"], data["label"] = "0", "Eteint"
            string += " state is OFF"
        elif state_code == "10":
            data["paired"] = "denied"
            string += " pairing denied"
        elif state_code == "11":
            data["paired"] = "ok"
            string += " pairing ok"
        elif state_code == "12":
            data["paired"] = "paired"
            string += " paired"
        elif state_code == "13":
            data["paired"] = "unpaired"
            string += " unpaired"
        string = _parse_groups(trame, string, data, offset=36)
    elif cf == "11":
        data["type"] = "binding"
        string += " binding"
    elif cf == "1b":
        data["type"] = "group"
        data["groups"] = [trame[38:46], trame[46:54]]
        string += f" group {data['groups']}"
    elif cf in ("1c", "1d"):
        data["type"] = "scene"
        data["subtype"] = "custom" if cf == "1c" else "schneider"
        data["scenes"] = [trame[38:46], trame[46:54], trame[54:62]]
        string += f" {data['subtype']}scene {data['scenes']}"
    return string


def _parse_generic(trame: str, cf: str, uuid: str, string: str, result: dict) -> str:
    result["model"] = "generic"
    string += f"This is a Generic with UUID {uuid}"
    data = result["data"]
    if cf == "20":
        data["type"] = "advertisement"
        string += " advertisement"
        data["firmware"] = trame[58:62]
        state_code = trame[32:34]
        if state_code == "01":
            data["value"], data["label"] = "1", "Allumé"
        elif state_code == "00":
            data["value"], data["label"] = "0", "Eteint"
        string = _parse_groups(trame, string, data, offset=36)
    elif cf == "21":
        data["type"] = "binding"
        string += " binding"
    return string


def _parse_shutter(trame: str, cf: str, uuid: str, string: str, result: dict) -> str:
    result["model"] = "shutter"
    string += f"This is a Shutter with UUID {uuid}"
    data = result["data"]
    if cf == "30":
        data["type"] = "advertisement"
        string += " advertisement"  # FIX : manquait dans la version précédente
        data["firmware"] = trame[58:62]
        code = trame[32:34]
        if code == "00":
            data["value"], data["label"] = 100, "Ouvert"
            string += " state is OPEN"
        elif code == "01":
            data["value"], data["label"] = 0, "Fermé"
            string += " state is CLOSED"
        elif code in ("05", "06", "07"):
            position = 100 - int(trame[44:46], 16)
            data["value"] = position
            data["label"] = {"05": "Ouverture", "06": "Fermeture", "07": "Arrêté"}[code]
            string += f" state is {data['label']} position={position}"
    elif cf == "31":
        data["type"] = "binding"
        string += " binding"
    return string


def _parse_plug(trame: str, cf: str, uuid: str, string: str, result: dict) -> str:
    """Prise Odace SFSP (type 9044) — même structure que DCL (on/off + groupes).

    cf 40 = advertisement, cf 41 = binding.
    """
    result["model"] = "plug"
    string += f"This is a Plug with UUID {uuid}"
    data = result["data"]
    if cf == "40":
        data["type"] = "advertisement"
        string += " advertisement"
        data["firmware"] = trame[58:62]
        state_code = trame[32:34]
        if state_code == "01":
            data["value"], data["label"] = "1", "Allumé"
            string += " state is ON"
        elif state_code == "00":
            data["value"], data["label"] = "0", "Eteint"
            string += " state is OFF"
        elif state_code == "10":
            data["paired"] = "denied"
            string += " pairing denied"
        elif state_code == "11":
            data["paired"] = "ok"
            string += " pairing ok"
        elif state_code == "12":
            data["paired"] = "paired"
            string += " paired"
        elif state_code == "13":
            data["paired"] = "unpaired"
            string += " unpaired"
        string = _parse_groups(trame, string, data, offset=36)
    elif cf == "41":
        data["type"] = "binding"
        string += " binding"
    return string


def _parse_dimmer(trame: str, cf: str, uuid: str, string: str, result: dict) -> str:
    """Variateur Odace SFSP (type 9144) — même structure que DCL.

    cf 50 = advertisement, cf 51 = binding.
    """
    result["model"] = "dimmer"
    string += f"This is a Dimmer with UUID {uuid}"
    data = result["data"]
    if cf == "50":
        data["type"] = "advertisement"
        string += " advertisement"
        data["firmware"] = trame[58:62]
        state_code = trame[32:34]
        if state_code == "01":
            data["value"], data["label"] = "1", "Allumé"
            string += " state is ON"
        elif state_code == "00":
            data["value"], data["label"] = "0", "Eteint"
            string += " state is OFF"
        elif state_code == "10":
            data["paired"] = "denied"
            string += " pairing denied"
        elif state_code == "11":
            data["paired"] = "ok"
            string += " pairing ok"
        elif state_code == "12":
            data["paired"] = "paired"
            string += " paired"
        elif state_code == "13":
            data["paired"] = "unpaired"
            string += " unpaired"
        string = _parse_groups(trame, string, data, offset=36)
    elif cf == "51":
        data["type"] = "binding"
        string += " binding"
    return string


def _parse_groups(trame: str, string: str, data: dict, offset: int) -> str:
    """Parse deux groupes dcl/generic (8 hex chars uuid + 2 hex chars state)."""
    data["groups"] = {}
    g1_uuid = trame[offset : offset + 8]
    g1_state = trame[offset + 8 : offset + 10]
    g2_uuid = trame[offset + 10 : offset + 18]
    g2_state = trame[offset + 18 : offset + 20]

    for guuid, gstate in ((g1_uuid, g1_state), (g2_uuid, g2_state)):
        entry = {"data": {}}
        if gstate == "01":
            entry["data"]["value"], entry["data"]["label"] = "1", "Allumé"
        elif gstate == "00":
            entry["data"]["value"], entry["data"]["label"] = "0", "Eteint"
        data["groups"][guuid] = entry
    string += f" group1 : {g1_uuid} group2 : {g2_uuid}"
    return string


def reset_dedupe() -> None:
    """Remise à zéro de l'état de déduplication (utile pour les tests)."""
    _lastevent.clear()
    _lastdata.clear()
