"""Émission par l'interface de gestion du noyau Linux (« mgmt ») et trace HCI.

Alternative à ``hcitool`` pour le mode contrôleur Bluetooth local. Au lieu
d'envoyer des commandes HCI brutes dans le dos de BlueZ, on demande au noyau,
par le canal de gestion que bluetoothd utilise lui-même, de diffuser la trame.

Ce qui a été vérifié sur du matériel réel (DCL, octobre 2026) :
- le module exige les flags ``02 01 04`` EN TÊTE de trame. L'API D-Bus de BlueZ
  (LEAdvertisingManager1) les place après les données fabricant et ne laisse pas
  choisir : elle ne peut pas servir. Le canal de gestion, lui, accepte les
  31 octets tels quels ;
- le module accepte une annonce non connectable émise avec une adresse
  aléatoire, ce que le noyau impose pour ce type d'annonce ;
- le noyau ne coupe pas le scan pendant l'émission, contrairement à la séquence
  hcitool.

Le module fournit aussi ``HciTrace``, qui relève ce que le noyau envoie
réellement à l'adaptateur (comme l'outil btmon) pour le service de diagnostic.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trace HCI : ce que le noyau envoie réellement à l'adaptateur
# ---------------------------------------------------------------------------

_AF_BLUETOOTH = 31
_BTPROTO_HCI = 1
_HCI_DEV_NONE = 0xFFFF
_HCI_CHANNEL_MONITOR = 2
_MON_COMMAND_PKT = 2
_MON_EVENT_PKT = 3

_ADV_COMMANDS = {
    0x2005: "set_random_address",
    0x2006: "set_adv_params",
    0x2008: "set_adv_data",
    0x2009: "set_scan_rsp_data",
    0x200A: "set_adv_enable",
    0x200B: "set_scan_params",
    0x200C: "set_scan_enable",
    0x2035: "ext_set_adv_random_address",
    0x2036: "ext_set_adv_params",
    0x2037: "ext_set_adv_data",
    0x2038: "ext_set_scan_rsp_data",
    0x2039: "ext_set_adv_enable",
    0x203C: "ext_remove_adv_set",
    0x2041: "ext_set_scan_params",
    0x2042: "ext_set_scan_enable",
}
_ADDR_TYPES = {0: "public", 1: "random", 2: "rpa_or_public", 3: "rpa_or_random"}
_ADV_TYPES_LEGACY = {0: "ADV_IND (connectable)", 1: "ADV_DIRECT_IND", 2: "ADV_SCAN_IND", 3: "ADV_NONCONN_IND"}


def decode_hci_command(opcode: int, params: bytes) -> Dict[str, Any]:
    """Décode une commande HCI d'advertising en champs lisibles."""
    entry: Dict[str, Any] = {"cmd": _ADV_COMMANDS.get(opcode, f"0x{opcode:04x}"), "raw": params.hex()}
    try:
        if opcode == 0x2006 and len(params) >= 15:
            entry["interval_ms"] = [
                int.from_bytes(params[0:2], "little") * 0.625,
                int.from_bytes(params[2:4], "little") * 0.625,
            ]
            entry["adv_type"] = _ADV_TYPES_LEGACY.get(params[4], params[4])
            entry["own_address"] = _ADDR_TYPES.get(params[5], params[5])
            entry["channels"] = params[13]
        elif opcode == 0x2008 and params:
            entry["data"] = params[1 : 1 + params[0]].hex()
        elif opcode == 0x200A and params:
            entry["enable"] = bool(params[0])
        elif opcode == 0x2005:
            entry["address"] = ":".join(f"{x:02X}" for x in reversed(params[:6]))
        elif opcode == 0x2036 and len(params) >= 11:
            props = int.from_bytes(params[1:3], "little")
            entry["handle"] = params[0]
            entry["properties"] = f"0x{props:04x}"
            entry["connectable"] = bool(props & 0x01)
            entry["legacy_pdu"] = bool(props & 0x10)
            entry["interval_ms"] = [
                int.from_bytes(params[3:6], "little") * 0.625,
                int.from_bytes(params[6:9], "little") * 0.625,
            ]
            entry["channels"] = params[9]
            entry["own_address"] = _ADDR_TYPES.get(params[10], params[10])
        elif opcode == 0x2037 and len(params) >= 4:
            entry["handle"] = params[0]
            entry["data"] = params[4 : 4 + params[3]].hex()
        elif opcode == 0x2039 and params:
            entry["enable"] = bool(params[0])
        elif opcode == 0x2035 and len(params) >= 7:
            entry["handle"] = params[0]
            entry["address"] = ":".join(f"{x:02X}" for x in reversed(params[1:7]))
    except Exception as err:  # noqa: BLE001 - une trace partielle vaut mieux que rien
        entry["decode_error"] = str(err)
    return entry


class HciTrace:
    """Relève les commandes d'advertising envoyées à un adaptateur pendant un essai."""

    def __init__(self, hci_index: int) -> None:
        self._index = hci_index
        self._sock = None
        self._loop = None
        self.entries: list = []
        self.error: str | None = None

    def start(self) -> None:
        try:
            sock = _open_hci_channel(_HCI_CHANNEL_MONITOR)
            self._sock = sock
            self._loop = asyncio.get_running_loop()
            self._loop.add_reader(sock.fileno(), self._on_readable)
        except Exception as err:  # noqa: BLE001 - la trace est facultative
            self.error = f"{type(err).__name__}: {err}"

    def _on_readable(self) -> None:
        try:
            while True:
                packet = self._sock.recv(4096)
                self.feed(packet)
        except BlockingIOError:
            pass
        except Exception as err:  # noqa: BLE001
            self.error = f"{type(err).__name__}: {err}"

    def feed(self, packet: bytes) -> None:
        """Traite un paquet du canal moniteur (en-tête : opcode, index, longueur)."""
        if len(packet) < 6:
            return
        mon_op = int.from_bytes(packet[0:2], "little")
        index = int.from_bytes(packet[2:4], "little")
        body = packet[6 : 6 + int.from_bytes(packet[4:6], "little")]
        if index != self._index:
            return
        if mon_op == _MON_COMMAND_PKT and len(body) >= 3:
            opcode = int.from_bytes(body[0:2], "little")
            if opcode in _ADV_COMMANDS:
                self.entries.append(decode_hci_command(opcode, body[3 : 3 + body[2]]))
        elif mon_op == _MON_EVENT_PKT and len(body) >= 6 and body[0] == 0x0E:
            # Command Complete : rattacher le status à la dernière commande de même opcode
            opcode = int.from_bytes(body[3:5], "little")
            name = _ADV_COMMANDS.get(opcode)
            if name:
                for entry in reversed(self.entries):
                    if entry["cmd"] == name and "status" not in entry:
                        entry["status"] = body[5]
                        break

    def stop(self) -> Any:
        if self._sock is not None:
            try:
                self._loop.remove_reader(self._sock.fileno())
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._sock = None
        if self.error and not self.entries:
            return f"indisponible : {self.error}"
        return self.entries


# ---------------------------------------------------------------------------
# Canal de gestion du noyau
# ---------------------------------------------------------------------------

_HCI_CHANNEL_CONTROL = 3
_MGMT_EV_CMD_COMPLETE = 0x0001
_MGMT_EV_CMD_STATUS = 0x0002
_MGMT_OP_READ_ADV_FEATURES = 0x003D
_MGMT_OP_ADD_ADVERTISING = 0x003E
_MGMT_OP_REMOVE_ADVERTISING = 0x003F
_MGMT_OP_ADD_EXT_ADV_PARAMS = 0x0054
_MGMT_OP_ADD_EXT_ADV_DATA = 0x0055
_MGMT_OP_READ_INFO = 0x0004
_MGMT_OP_SET_POWERED = 0x0005
_MGMT_ADV_PARAM_TIMEOUT = 1 << 13
_MGMT_ADV_PARAM_INTERVALS = 1 << 14
_MGMT_SETTING_POWERED = 1 << 0
_MGMT_STATUS = {
    0x00: "success", 0x01: "unknown_command", 0x03: "failed", 0x0A: "busy", 0x0B: "rejected",
    0x0C: "not_supported", 0x0D: "invalid_params", 0x0F: "not_powered", 0x11: "invalid_index",
    0x14: "permission_denied",
}


def _open_hci_channel(channel: int):
    """Ouvre un socket HCI sur un canal du noyau (moniteur ou gestion)."""
    import ctypes  # noqa: PLC0415
    import socket  # noqa: PLC0415

    sock = socket.socket(_AF_BLUETOOTH, socket.SOCK_RAW, _BTPROTO_HCI)

    class _SockaddrHci(ctypes.Structure):
        _fields_ = [("family", ctypes.c_ushort), ("dev", ctypes.c_ushort), ("channel", ctypes.c_ushort)]

    addr = _SockaddrHci(_AF_BLUETOOTH, _HCI_DEV_NONE, channel)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.bind(sock.fileno(), ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
        errno = ctypes.get_errno()
        sock.close()
        raise OSError(errno, f"bind du canal HCI {channel} refusé")
    sock.setblocking(False)
    return sock


def mgmt_packet(opcode: int, index: int, params: bytes = b"") -> bytes:
    return opcode.to_bytes(2, "little") + index.to_bytes(2, "little") + len(params).to_bytes(2, "little") + params


def mgmt_parse_reply(packet: bytes, opcode: int, index: int):
    """Retourne (status, données) si ``packet`` répond à la commande, sinon None."""
    if len(packet) < 9:
        return None
    event = int.from_bytes(packet[0:2], "little")
    if event not in (_MGMT_EV_CMD_COMPLETE, _MGMT_EV_CMD_STATUS):
        return None
    if int.from_bytes(packet[2:4], "little") != index or int.from_bytes(packet[6:8], "little") != opcode:
        return None
    return packet[8], packet[9:]


async def _mgmt_command(sock, opcode: int, index: int, params: bytes = b"", timeout: float = 2.0):
    """Envoie une commande de gestion et attend sa réponse. Retourne (status, données)."""
    loop = asyncio.get_running_loop()
    await loop.sock_sendall(sock, mgmt_packet(opcode, index, params))
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"pas de réponse à la commande 0x{opcode:04x}")
        packet = await asyncio.wait_for(loop.sock_recv(sock, 1024), remaining)
        reply = mgmt_parse_reply(packet, opcode, index)
        if reply is not None:
            return reply


def _status_name(status: int) -> str:
    return _MGMT_STATUS.get(status, f"0x{status:02x}")


def parse_controller_info(data: bytes) -> Dict[str, Any]:
    """Décode la réponse « Read Controller Information » : adresse et état d'alimentation."""
    if len(data) < 17:
        return {"error": "réponse trop courte"}
    settings = int.from_bytes(data[13:17], "little")
    return {
        "address": ":".join(f"{x:02X}" for x in reversed(data[0:6])),
        "powered": bool(settings & _MGMT_SETTING_POWERED),
    }


def _mgmt_command_sync(opcode: int, index: int, params: bytes = b"", timeout: float = 2.0):
    """Version synchrone de ``_mgmt_command`` (à appeler hors de la boucle asyncio)."""
    import time  # noqa: PLC0415

    sock = _open_hci_channel(_HCI_CHANNEL_CONTROL)
    try:
        sock.setblocking(True)
        sock.send(mgmt_packet(opcode, index, params))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"pas de réponse à la commande 0x{opcode:04x}")
            sock.settimeout(remaining)
            reply = mgmt_parse_reply(sock.recv(1024), opcode, index)
            if reply is not None:
                return reply
    finally:
        sock.close()


def controller_is_powered(hci_index: int) -> bool | None:
    """Le contrôleur existe-t-il et est-il allumé ? (synchrone)

    Retourne None si le canal de gestion est inutilisable : l'appelant peut
    alors se rabattre sur hcitool.
    """
    try:
        status, data = _mgmt_command_sync(_MGMT_OP_READ_INFO, hci_index)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("mgmt : lecture de l'état de hci%d impossible : %s", hci_index, err)
        return None
    if status != 0:
        return False  # index inconnu : l'interface n'existe pas (ou plus)
    return bool(parse_controller_info(data).get("powered"))


def controller_power_on(hci_index: int) -> str:
    """Allume le contrôleur par le canal de gestion (synchrone).

    Retourne le message d'erreur, vide si la commande est acceptée. bluetoothd
    est informé du changement par le noyau, contrairement à ``hciconfig up``.
    """
    try:
        status, _ = _mgmt_command_sync(_MGMT_OP_SET_POWERED, hci_index, b"\x01", timeout=5.0)
    except Exception as err:  # noqa: BLE001
        return f"{type(err).__name__}: {err}"
    return "" if status == 0 else _status_name(status)


async def async_send_mgmt(
    hci_index: int, payload: str, *, duration: float = 0.5, interval_ms: int = 100,
) -> bool:
    """Diffuse la trame via le canal de gestion. Retourne True si elle a été émise."""
    report = await async_send_mgmt_report(hci_index, payload, duration=duration, interval_ms=interval_ms)
    return report["success"]


async def async_send_mgmt_report(
    hci_index: int, payload: str, *, duration: float = 0.5, interval_ms: int = 100,
) -> Dict[str, Any]:
    """Diffuse la trame exacte via le canal de gestion et retourne un compte rendu.

    L'annonce est non connectable : le noyau l'émet avec une adresse aléatoire,
    ce que les modules acceptent. Ne lève pas d'exception : l'erreur éventuelle
    est rapportée dans ``error``.
    """
    frame = bytes.fromhex(payload.replace(" ", ""))
    report: Dict[str, Any] = {
        "duration": duration, "interval_ms": interval_ms,
        "data": frame.hex(), "success": False, "steps": {},
    }
    flags = 0  # ni connectable, ni flags gérés par le noyau : la trame est émise telle quelle
    sock = None
    instance = None
    try:
        sock = _open_hci_channel(_HCI_CHANNEL_CONTROL)

        # 0. État du contrôleur (diagnostic)
        status, data = await _mgmt_command(sock, _MGMT_OP_READ_INFO, hci_index)
        report["controller"] = parse_controller_info(data) if status == 0 else _status_name(status)

        # 1. Capacités : nombre d'instances d'annonce, instances déjà utilisées
        status, data = await _mgmt_command(sock, _MGMT_OP_READ_ADV_FEATURES, hci_index)
        report["steps"]["read_adv_features"] = _status_name(status)
        if status != 0 or len(data) < 8:
            raise RuntimeError(f"lecture des capacités : {_status_name(status)}")
        max_instances, num_instances = data[6], data[7]
        used = list(data[8 : 8 + num_instances])
        report["instances"] = {"max": max_instances, "used": used}
        # Dernière instance libre : bluetoothd attribue les siennes à partir de 1
        instance = next((i for i in range(max_instances, 0, -1) if i not in used), None)
        if instance is None:
            raise RuntimeError("aucune instance d'annonce libre")
        report["instance"] = instance

        # 2. Paramètres avec intervalle imposé (noyau ≥ 5.11), puis données.
        # Durée de vie limitée côté noyau : si Home Assistant s'arrête avant d'avoir
        # retiré l'annonce, le noyau la retire de lui-même au lieu de diffuser la
        # dernière commande indéfiniment.
        units = max(32, round(interval_ms / 0.625))
        lifetime = int(duration + 0.999) + 2  # secondes
        report["kernel_timeout_s"] = lifetime
        params = (
            bytes([instance])
            + (flags | _MGMT_ADV_PARAM_INTERVALS | _MGMT_ADV_PARAM_TIMEOUT).to_bytes(4, "little")
            + (0).to_bytes(2, "little") + lifetime.to_bytes(2, "little")
            + units.to_bytes(4, "little") + units.to_bytes(4, "little")
            + bytes([0x7F])
        )
        status, _ = await _mgmt_command(sock, _MGMT_OP_ADD_EXT_ADV_PARAMS, hci_index, params)
        report["steps"]["add_ext_adv_params"] = _status_name(status)
        if status == 0:
            status, _ = await _mgmt_command(
                sock, _MGMT_OP_ADD_EXT_ADV_DATA, hci_index, bytes([instance, len(frame), 0]) + frame,
            )
            report["steps"]["add_ext_adv_data"] = _status_name(status)
        else:
            # Repli : commande historique, sans réglage de l'intervalle (1,28 s par défaut)
            legacy = (
                bytes([instance]) + flags.to_bytes(4, "little")
                + (0).to_bytes(2, "little") + lifetime.to_bytes(2, "little")
                + bytes([len(frame), 0]) + frame
            )
            status, _ = await _mgmt_command(sock, _MGMT_OP_ADD_ADVERTISING, hci_index, legacy)
            report["steps"]["add_advertising"] = _status_name(status)
            report["interval_note"] = "intervalle non réglable par ce noyau"
        if status != 0:
            raise RuntimeError(f"ajout de l'annonce : {_status_name(status)}")

        _LOGGER.debug("mgmt : annonce diffusée sur hci%d (instance %d, %.1f s)", hci_index, instance, duration)
        await asyncio.sleep(duration)
        report["success"] = True
    except Exception as err:  # noqa: BLE001 - tout est rapporté à l'utilisateur
        report["error"] = f"{type(err).__name__}: {err}"
        _LOGGER.warning("mgmt : échec de l'émission sur hci%d : %s", hci_index, report["error"])
    finally:
        if sock is not None:
            if instance is not None:
                try:
                    status, _ = await _mgmt_command(
                        sock, _MGMT_OP_REMOVE_ADVERTISING, hci_index, bytes([instance]),
                    )
                    report["steps"]["remove_advertising"] = _status_name(status)
                except Exception as err:  # noqa: BLE001
                    report["steps"]["remove_advertising"] = f"{type(err).__name__}: {err}"
            sock.close()
    return report

