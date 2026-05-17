# Beagle (Schneider Odace SFSP) — intégration Home Assistant 2026.4

Portage complet du plugin Jeedom `beagle` vers Home Assistant.
Supporte les DCL (lumières pilotables), les switchs Odace (récepteurs), et
laisse la porte ouverte aux modèles shutter / generic / plug / dimmer / scene.

## Installation

1. Copier le dossier `custom_components/odace_sfsp/` dans
   `<config_home_assistant>/custom_components/`.
2. Redémarrer Home Assistant.
3. **Paramètres → Appareils et services → Ajouter une intégration → Schneider Odace SFSP**.
4. Choisir le contrôleur Bluetooth (liste issue de l'intégration `bluetooth`
   de HA — `hci0` par défaut). La MAC est récupérée automatiquement.
5. Les 8 périphériques fournis dans `régles.txt` sont importés
   automatiquement (4 DCL + 4 switchs).

## Fonctionnalités

- **Découverte passive BLE** via l'intégration `bluetooth` de HA : pas de
  daemon Python séparé, pas de socket, pas de `callback` HTTP.
  Filtrage direct sur le manufacturer id Schneider `0x02B6`.
- **DCL → entité `light`** : commande `on`/`off` et écoute d'état.
  Protection anti-boucle : la réception d'une trame qui confirme l'état
  fraîchement envoyé ne redéclenche pas l'envoi.
- **Switch → entité `event`** : chaque appui déclenche un évènement
  (`toggle`, `on`, `off`, `up`, `down`, `stop`, `dim_up`, `dim_down`,
  `scene_*`). Utilisable directement comme trigger d'automatisation.
- **Mode apprentissage** via service `odace_sfsp.start_learn` (timeout
  paramétrable, par défaut 60 s). Les trames `binding` reçues pendant ce
  mode sont enregistrées automatiquement.
- **Ajout / modification / suppression manuelle** depuis le menu
  *Configurer* de l'intégration ou via les services
  `odace_sfsp.add_device`, `odace_sfsp.remove_device`. Le changement de modèle
  supprime l'entity précédente (un DCL passant `switch` ne laisse pas
  d'entity `light` orpheline).
- **Logs** conformes au format Jeedom dans le logger
  `custom_components.odace_sfsp` (trames hex en debug + description lisible,
  `Beagle TX uuid=... ac=...` en info pour les émissions).

## Services

| Service | Description |
| --- | --- |
| `odace_sfsp.send_command` | `uuid`, `ac` (`on`,`off`,`toggle`,`up`,`down`,`stop`,`goto`), `options` (0-100) |
| `odace_sfsp.start_learn` | Mode inclusion (`timeout` en secondes) |
| `odace_sfsp.add_device` | `uuid`, `model`, `name`, `mac` |
| `odace_sfsp.remove_device` | `uuid` |

## Exigences système

L'**envoi** de trames BLE se fait via `hcitool` (équivalent au daemon Jeedom).
Sur Home Assistant OS / Supervised, l'add-on doit être lancé avec les droits
root ou l'utilisateur HA doit pouvoir exécuter `sudo hcitool` sans mot de
passe (`/etc/sudoers.d/99-odace_sfsp : homeassistant ALL=NOPASSWD: /usr/bin/hcitool`).
La **réception** fonctionne nativement via l'intégration `bluetooth` de HA.

## Validation

Un harnais de test (`tests/validate_parser.py`) rejoue les trames réelles
extraites de `beagle3.log` (log Jeedom de production) et les compare aux
résultats envoyés à Jeedom à l'époque :

```
$ python3 tests/validate_parser.py
8 réussis / 0 échoués sur 8 cas testés
```

Les 8 cas couvrent les DCL état `OFF` / `ON` et les interrupteurs `Toggle`
avec leurs groupes `ffffffff`.

## Liste des périphériques importés automatiquement

| UUID | Modèle | Nom |
| --- | --- | --- |
| `472500` | dcl | Plafonnier de la Salle de bain |
| `362500` | dcl | Armoire de toilette de la Salle de bain |
| `8E2200` | dcl | Armoire de toilette de la Salle d'eau |
| `832200` | dcl | Plafonnier de la Salle d'eau |
| `BF3A00` | switch | Interrupteur de l'armoire de toilette de la Salle d'eau |
| `FF3A00` | switch | Interrupteur du plafonnier de la Salle d'eau |
| `943A00` | switch | Interrupteur de l'armoire de toilette de la Salle de bain |
| `123B00` | switch | Interrupteur du plafonnier de la Salle de bain |
