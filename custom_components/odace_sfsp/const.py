"""Constants for the Beagle (Schneider Odace SFSP) integration.

Ported from Jeedom plugin Beagle (globals.py + beagle.py).
"""
from __future__ import annotations

DOMAIN = "odace_sfsp"

# Identifiant fabricant BLE Schneider Electric
MANUFACTURER_ID = 0x02B6  # 0xB602 en little-endian dans la trame

# Catégories de configuration communes
CONF_HCI = "hci"
CONF_MAC = "mac"
CONF_JEEDOM_KEY = "jeedom_key"
CONF_DEVICES = "devices"
CONF_UUID = "uuid"
CONF_MODEL = "model"
CONF_NAME = "name"

DEFAULT_HCI = "hci0"

# Mode d'envoi des trames BLE
CONF_SEND_MODE = "send_mode"
SEND_MODE_HCI         = "hci"          # Dongle USB local (HAOS, Proxmox, etc.) — hcitool
SEND_MODE_MQTT        = "mqtt"         # ESP32 via MQTT
SEND_MODE_ESPHOME_API = "esphome_api"  # ESP32 BLE proxy via API native ESPHome

# Configuration MQTT (mode ESP32/MQTT)
CONF_MQTT_TOPIC = "mqtt_topic"
DEFAULT_MQTT_TOPIC = "odace_sfsp/send"
# Topic sur lequel l'ESP32 publie sa MAC Bluetooth au démarrage (découverte auto)
DEFAULT_MQTT_MAC_TOPIC = "odace_sfsp/mac"
# Durée max d'attente pour la découverte automatique de la MAC ESP32 (secondes)
MAC_DISCOVERY_TIMEOUT = 8

# Configuration ESPHome API (mode proxy natif)
CONF_ESPHOME_ENTRY_ID = "esphome_entry_id"   # entry_id de la config entry ESPHome
CONF_ESPHOME_SERVICE  = "esphome_service"    # nom du service ESPHome (ex: odace_send)
DEFAULT_ESPHOME_SERVICE = "odace_send"       # service par défaut attendu dans le firmware

# Modèles supportés
MODEL_DCL = "dcl"
MODEL_SWITCH = "switch"
MODEL_SHUTTER = "shutter"
MODEL_GENERIC = "generic"
MODEL_PLUG = "plug"
MODEL_DIMMER = "dimmer"
MODEL_SCENE = "scene"

SUPPORTED_MODELS = [
    MODEL_DCL,
    MODEL_SWITCH,
    MODEL_SHUTTER,
    MODEL_GENERIC,
    MODEL_PLUG,
    MODEL_DIMMER,
    MODEL_SCENE,
]

# Entête de trame (protocole Beagle)
UNIQUE_HEADER = "0201041BFFB602"
HEADER_VV = "01"
HEADER_FS = "01"
UUID_CONTROLLER = "443884"
UNIQUE_KEY = "9f5b9cced150d9d051b0b7da4c4e2de6"

# Type-id hex (2 octets) par modèle
TYPES = {
    "shutter": "8f44",
    "dcl": "9844",
    "generic": "9244",
    "switch": "8e44",
    "plug": "9044",
    "dimmer": "9144",
    "gateway": "A244",
}

# Commands "ac" (action code)
AC = {
    "off": "00",
    "on": "01",
    "toggle": "02",
    "up": "05",
    "down": "06",
    "stop": "07",
    "goto": "20",
    "customerScenes": "0C",
    "schneiderScenes": "0D",
    "groups": "0B",
}

# Control function "cf" par modèle (pour la trame sortante)
CFTARGET = {
    "switch": "0F",
    "dcl": "1F",
    "generic": "2F",
    "shutter": "3F",
    "plug": "4F",
    "dimmer": "5F",
    "scene": "FF",
    "groupdcl": "1F",
    "groupshutter": "3F",
    "groupplug": "4F",
    "groupdimmer": "5F",
}

GATEWAY = {
    "advertisement": "A0",
    "binding": "A1",
}

SCENES = {
    "schneider": "FD",
    "custom": "FC",
}

# Signals dispatcher
SIGNAL_DEVICE_UPDATE = "odace_sfsp_device_update_{uuid}"
SIGNAL_DEVICES_CHANGED = "odace_sfsp_devices_changed"
