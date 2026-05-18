# Schneider Odace SFSP (portage du plugin Jeedom 'beagle') — intégration Home Assistant

[![GitHub Release][releases-shield]][releases]
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square)](https://github.com/hacs/integration)
[![Maintainers](https://img.shields.io/badge/maintainers-@slemeur91-blue.svg?style=flat-square)](#)

Portage avancé du plugin Jeedom `beagle` vers Home Assistant.
Supporte les DCL (lumières pilotables), les switchs Odace (récepteurs), et
laisse la porte ouverte aux modèles shutter / generic / plug / dimmer / scene.

> [!IMPORTANT]
> Ne disposant que de DCL et switchs je n'ai pas pu tester les autre composants.
> Et ne souhaitant pas refaire l'apprentissage cette partie non plus n'a pas était testé.
> Le portage a était éffectué grâce à l'IA et je veux bien aider à enrichir le plugin si il y a des volontaires pour tester.

> [!TIP]
> Pour le fonctionnement j'ai repris le dongle Bluetooth ainsi que la JEEDOM_KEY de Jeedom.
> Il est possible de reprendre la clé JEEDOM_KEY (voir les logs de Jeedom) et l'adresse MAC du dongle Jeedom (même avec un autre dongle) : ce sont les 2 éléments qui servent pour l'associasion des périphériques.
> Ces informations peuvent être forcées dans le fichier const.py ainsi que la liste des périphériques afin qu'ils soient automatiquement renseignés lors du premier lancement de l'intégration.

## Installation

## HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=slemeur91&repository=odace_sfsp&category=integration)

### Manuellement

1. Copier le dossier `custom_components/odace_sfsp/` dans `<config_home_assistant>/custom_components/`.
2. Redémarrer Home Assistant.
3. **Paramètres → Appareils et services → Ajouter une intégration → Schneider Odace SFSP**.
4. Choisir le contrôleur Bluetooth (liste issue de l'intégration `bluetooth` de HA — `hci0` par défaut). La MAC est récupérée automatiquement.

## Fonctionnalités

- **Découverte passive BLE** via l'intégration `bluetooth` de HA : pas de daemon Python séparé, pas de socket, pas de `callback` HTTP.
  Filtrage direct sur le manufacturer id Schneider `0x02B6`.
- **DCL → entité `light`** : commande `on`/`off` et écoute d'état.
  Protection anti-boucle : la réception d'une trame qui confirme l'état fraîchement envoyé ne redéclenche pas l'envoi.
- **Switch → entité `event`** : chaque appui déclenche un évènement (`toggle`, `on`, `off`, `up`, `down`, `stop`, `dim_up`, `dim_down`, `scene_*`). Utilisable directement comme trigger d'automatisation.
- **Mode apprentissage** via service `odace_sfsp.start_learn` (timeout paramétrable, par défaut 60 s). Les trames `binding` reçues pendant ce mode sont enregistrées automatiquement.
- **Ajout / modification / suppression manuelle** depuis le menu *Configurer* de l'intégration ou via les services `odace_sfsp.add_device`, `odace_sfsp.remove_device`.
  Le changement de modèle supprime l'entity précédente (un DCL passant `switch` ne laisse pas d'entity `light` orpheline).
- **Logs** conformes au format Jeedom dans le logger `custom_components.odace_sfsp` (trames hex en debug + description lisible, `Beagle TX uuid=... ac=...` en info pour les émissions).

## Services

| Service | Description |
| --- | --- |
| `odace_sfsp.send_command` | `uuid`, `ac` (`on`,`off`,`toggle`,`up`,`down`,`stop`,`goto`), `options` (0-100) |
| `odace_sfsp.start_learn` | Mode inclusion (`timeout` en secondes) |
| `odace_sfsp.add_device` | `uuid`, `model`, `name`, `mac` |
| `odace_sfsp.remove_device` | `uuid` |

## Exigences système

L'**envoi** de trames BLE se fait via `hcitool` (équivalent au daemon Jeedom).
La **réception** fonctionne nativement via l'intégration `bluetooth` de HA.

## Validation

Un harnais de test (`tests/validate_parser.py`) rejoue les trames réelles extraites de `beagle3.log` (log Jeedom de production) et les compare aux résultats envoyés à Jeedom à l'époque :

```
$ python3 tests/validate_parser.py
8 réussis / 0 échoués sur 8 cas testés
```

Les 8 cas couvrent les DCL état `OFF` / `ON` et les interrupteurs `Toggle` avec leurs groupes `ffffffff`.
