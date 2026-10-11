# Schneider Odace SFSP (portage du plugin Jeedom 'beagle') — intégration Home Assistant

[![GitHub Release][releases-shield]][releases]
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square)](https://github.com/hacs/integration)
[![Maintainers](https://img.shields.io/badge/maintainers-@slemeur91-blue.svg?style=flat-square)](#)

🇬🇧 [English version](README_EN.md) · 📖 [Wiki](https://github.com/slemeur91/odace_sfsp/wiki)

Cette intégration concerne le portage du plugin Jeedom `beagle` vers Home Assistant.

> [!NOTE]
> Un grand merci aux contributeurs :
> - **@rommess** — tests pour la gestion du module `generic`, de l'association des modules et des contrôleurs Bluetooth PROXMOX et ESPHome
> - **@NeySlim** — PR pour le support du Bluetooth 5
> - **@PinPinLeLapin31** — tests pour la gestion du module volet, du désappairage des modules et du problème de `hci` qui change aléatoirement au redémarrage de Home Assistant

## Modules supportés

| Type | Statut | Notice(s) |
|------|--------|-----------|
| `switch` | ✅ | [Notice DCL](hardware/notice_DCL.pdf) |
| `dcl` | ✅ | [Notice DCL](hardware/notice_DCL.pdf) |
| `generic` | ✅ | [Actionneur générique 10A NLR InOut](hardware/manuel%20SCHS520192.pdf) |
| `shutter` | — | [Notice volet roulant](hardware/notice_VR.pdf) · [Moteurs compatibles](hardware/liste%20moteurs%20VR%20compatibles-Actionneur%20SE%20ma_j%209juillet2019.pdf) |
| `plug` | — | [Actionneur faux-plafond](hardware/MFR82543-00%203mai19.pdf) |
| `scene` | — | [Commandes groupées sans fil](hardware/GDE33208-0026Sept19.pdf) |

> ✅ = Testé et validé · — = Non testé (contributions bienvenues)

### Ressources Jeedom

Ces articles expliquent l'installation et la configuration des modules sous Jeedom, dont les principes sont transposables à cette intégration :

- [Recherche SFSP sur Jeedomiser](https://jeedomiser.fr/?s=sfsp)
- [Interrupteur Odace SFSP avec Jeedom](https://jeedomiser.fr/article/votre-interrupteur-odace-sfsp-avec-jeedom/)
- [Éclairage avec la gamme Odace SFSP et Jeedom](https://jeedomiser.fr/article/votre-eclairage-avec-la-gamme-odace-sfsp-et-jeedom/)
- [Actionneur minuteur générique Odace SFSP avec Jeedom](https://jeedomiser.fr/article/actionneur-minuteur-generique-odace-sfsp-avec-jeedom/)
- [Actionneur pour volet roulant Odace SFSP et Jeedom](https://jeedomiser.fr/article/votre-actionneur-pour-volet-roulant-odace-sfsp-et-jeedom/)
- [Interrupteur de scène Odace SFSP avec Jeedom](https://jeedomiser.fr/article/votre-interrupteur-de-scene-odace-sfsp-avec-jeedom/)

## Installation

## HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=slemeur91&repository=odace_sfsp&category=integration)

### Manuellement

1. Copier le dossier `custom_components/odace_sfsp/` dans `<config_home_assistant>/custom_components/`.
2. Redémarrer Home Assistant.
3. **Paramètres → Appareils et services → Ajouter une intégration → Schneider Odace SFSP**.
4. Choisir le contrôleur Bluetooth et suivre les étapes de configuration décrites ci-dessous.

## Configuration

### Choix du contrôleur Bluetooth

Lors de la première configuration, l'intégration propose de choisir le protocole de communication avec le contrôleur Bluetooth :

<img src="images/menu-ajout-controleur.png" alt="Menu sélection du contrôleur Bluetooth" width="480">

- **HCI** : contrôleur Bluetooth branché directement sur la machine qui héberge Home Assistant (Raspberry Pi, NUC, etc.) ou passé en USB à une VM (PROXMOX par exemple). Home Assistant détecte automatiquement les adaptateurs disponibles (typiquement `hci0`). C'est le mode le plus simple et le plus fiable.

<p>
  <img src="images/ajout-controleur-hci-1.png" alt="Ajout contrôleur Bluetooth HCI étape 1" width="49%" align="top">
  <img src="images/ajout-controleur-hci-2.png" alt="Ajout contrôleur Bluetooth HCI étape 2" width="49%" align="top">
</p>

- **ESPHome** : contrôleur Bluetooth distant piloté via un ESP32 flashé avec ESPHome. Ce mode est utile pour étendre la portée en plaçant l'ESP32 près des modules Odace. Voir la section [Configuration ESPHome](#configuration-esphome) pour le script de compilation.

<p>
  <img src="images/ajout-controleur-esphome-1.png" alt="Ajout contrôleur Bluetooth ESPHome étape 1" width="49%" align="top">
  <img src="images/ajout-controleur-esphome-2.png" alt="Ajout contrôleur Bluetooth ESPHome étape 2" width="49%" align="top">
</p>

### Menu de gestion des modules

Depuis **Paramètres → Appareils et services → Schneider Odace SFSP**, cliquer sur **Configurer** pour accéder au menu principal de gestion des modules. Ce menu propose les actions suivantes : ajouter, modifier, supprimer ou réassocier un module, rechercher les scènes d'un module, modifier le contrôleur Bluetooth et accéder aux paramètres avancés.

<img src="images/menu-configuration.png" alt="Menu Configurer" width="480">

### Ajout des modules — Association

L'association d'un module Odace SFSP se fait en mode apprentissage :

1. Appuyez une fois sur le module pour le passer en mode apprentissage. Il restera dans cet état pendant 60 s (clignotant rouge et vert).
2. Dans les 60 s qui suivent, ouvrir le menu **Configurer** et choisir **Ajouter un module**. L'écran liste les modules qui se sont signalés pendant la dernière minute : il faut donc appuyer sur le module **avant** de l'ouvrir. Si la liste est vide, fermer l'écran, appuyer à nouveau sur le module et rouvrir.
3. Sélectionner le module détecté, qui apparaît avec son UUID et son type. Saisir un nom, vérifier le type (`switch`, `dcl`, `generic`, etc.) et valider.

Pour les modules commandables (`dcl`, `generic`, `shutter`, `plug`), la trame d'association est envoyée automatiquement tant que le module clignote encore. Si le délai est dépassé, appuyer à nouveau sur le module : la trame part à l'appui, pendant les 30 minutes qui suivent l'ajout.

Un module peut aussi être ajouté sans mode apprentissage, en choisissant **Saisir manuellement** et en renseignant son UUID (format logs/Jeedom, ou format étiquette du module).

<img src="images/menu-ajout-module.png" alt="Menu ajout d'un module" width="480">

### Modification et suppression d'un module

Depuis le menu **Configurer** de l'intégration il est également possible de :

- **Modifier un module** : changer son nom ou son type. Si le type change (par exemple un DCL reclassé en `switch`), l'entité précédente est supprimée et une nouvelle entité du bon domaine est créée — aucune entité orpheline n'est laissée.

<p>
  <img src="images/menu-modification-module-1.png" alt="Modification d'un module étape 1" width="49%" align="top">
  <img src="images/menu-modification-module-2.png" alt="Modification d'un module étape 2" width="49%" align="top">
</p>

- **Supprimer un module** : retire le module de l'intégration et supprime l'entité associée. Pour un module commandable, l'option **Désappairer le module puis supprimer de HA** rompt aussi l'association côté module : un écran d'attente demande d'appuyer une fois sur son bouton, et le module n'est supprimé que s'il confirme le désappairage.

<p>
  <img src="images/menu-suppression-module-1.png" alt="Suppression d'un module étape 1" width="49%" align="top">
  <img src="images/menu-suppression-module-2.png" alt="Suppression d'un module étape 2" width="49%" align="top">
</p>

### Réassocier un module

La trame d'association est une bascule : elle associe un module libre, mais désassocie un module déjà associé. L'intégration ne l'envoie donc qu'à un module « à associer », c'est-à-dire ajouté ou marqué à réassocier depuis moins de 30 minutes. Un appui sur le bouton d'un module qui fonctionne, par exemple pour lui associer un interrupteur sans fil, ne touche pas à son association avec Home Assistant.

Le menu **Réassocier un module** sert pour un module qui n'obéit plus (réinitialisé, ou associé à un autre contrôleur) :

1. Choisir le module dans la liste et valider.
2. Appuyer **une fois** sur le bouton physique du module dans les 30 minutes : Home Assistant lui envoie la trame d'association.

Si le module était encore associé, il se désassocie au lieu de s'associer. Une alerte apparaît alors dans **Paramètres → Réparations** : un second appui sur son bouton le réassocie et lève l'alerte.

<img src="images/reassocier-module.png" alt="Réassocier un module" width="480">

### Rechercher les scènes d'un module

Une scène n'est pas un module physique : c'est un identifiant de 8 caractères hexadécimaux mémorisé par les modules, de type personnalisée ou Schneider. Le menu **Rechercher les scènes d'un module** les retrouve :

1. Choisir un module `dcl`, `shutter` ou `generic` déjà enregistré.
2. Choisir les scènes à rechercher : personnalisées, Schneider, ou les deux.
3. Home Assistant interroge le module, puis crée une entité `scene` pour chaque scène nouvelle. L'écran suivant affiche les scènes trouvées.

Le type d'une scène se règle ensuite dans **Modifier un module**. Les scènes n'ont pas encore été testées sur du matériel réel.

<img src="images/rechercher-scenes.png" alt="Rechercher les scènes d'un module" width="480">

### Modification du contrôleur Bluetooth

Le menu **Modifier le contrôleur Bluetooth** permet de choisir un autre adaptateur. L'adresse MAC qui signe les trames est conservée : les modules déjà associés continuent d'obéir au nouveau contrôleur, sans réassociation. Elle se modifie dans les paramètres avancés.

### Paramètres avancés

Le menu **Paramètres avancés** propose deux options :

<img src="images/menu-parametre-avance.png" alt="Menu paramètres avancés" width="480">

**1. Clé Jeedom et MAC du contrôleur Bluetooth**

Permet de consulter ou modifier les deux éléments clés hérités de Jeedom :

- La **JEEDOM_KEY** est la clé de chiffrement utilisée pour signer les trames BLE envoyées aux modules Odace. Elle est propre à chaque installation et peut être récupérée dans les logs de Jeedom.
- L'**adresse MAC du contrôleur Bluetooth** est l'identifiant utilisé lors de l'association des modules. Il est possible de réutiliser l'adresse MAC du contrôleur Bluetooth Jeedom d'origine avec un autre adaptateur physique — c'est ce qui permet de migrer une installation Jeedom existante vers Home Assistant sans réassocier les modules.

Le même écran propose, pour un contrôleur Bluetooth local, la **méthode d'émission** :

- **hcitool** (par défaut) : la méthode d'origine, héritée du plugin Jeedom. Les commandes sont envoyées directement à l'adaptateur.
- **Noyau Linux** (expérimentale) : les trames sont confiées au noyau, sans `hcitool` et sans interrompre l'écoute Bluetooth de Home Assistant. En cas d'échec, l'intégration se replie sur `hcitool`.

Ce choix n'existe pas en mode ESPHome, qui n'est pas concerné. Les retours sur la méthode Noyau Linux sont les bienvenus : elle a vocation à devenir la méthode par défaut.

<img src="images/menu-cle-jeedom-mac.png" alt="Clé Jeedom, MAC du contrôleur Bluetooth et méthode d'émission" width="480">

**2. Export / Import de la configuration des modules**

Permet de sauvegarder ou restaurer la liste complète des modules associés au format JSON. Utile pour migrer l'intégration vers une nouvelle instance Home Assistant ou pour pré-renseigner les modules sans repasser par le mode apprentissage.

- **Export** : le champ contient la liste actuelle, une ligne par module, à copier.
- **Import** : coller soit ces lignes telles quelles, soit un objet JSON complet entre accolades. Les UUID déjà présents sont ignorés, de même que les lignes dont l'UUID ou le type est invalide.

L'import crée les entités sans envoyer de trame d'association : il convient aux modules déjà associés avec la clé et la MAC configurées.

Exemple de format :

```json
"943A00": {"uuid": "943A00", "mac": "60:C0:BF:30:2B:16", "model": "switch", "name": "Interrupteur de l'Armoire de toilette de la Salle de bain"},
"123B00": {"uuid": "123B00", "mac": "60:C0:BF:30:2B:75", "model": "switch", "name": "Interrupteur du Plafonnier de la Salle de bain"},
"BF3A00": {"uuid": "BF3A00", "mac": "60:C0:BF:30:2E:39", "model": "switch", "name": "Interrupteur de l'Armoire de toilette de la Salle d'eau"},
"FF3A00": {"uuid": "FF3A00", "mac": "60:C0:BF:30:2B:60", "model": "switch", "name": "Interrupteur du Plafonnier de la Salle d'eau"}
```

<img src="images/menu-export-import.png" alt="Export / Import de la configuration" width="480">

### Configuration ESPHome

Pour utiliser un ESP32 comme relais Bluetooth (mode ESPHome), le firmware doit exposer un service `odace_send` qui envoie une trame BLE brute. Voici le fragment de configuration à ajouter à votre fichier `esphome/*.yaml` :

```yaml
api:
  encryption:
    key: !secret api_doorbell
  services:
    - service: odace_send
      variables:
        payload: string
      then:
        - lambda: |-
            std::vector<uint8_t> data;
            for (size_t i = 0; i + 1 < payload.size(); i += 2)
              data.push_back((uint8_t)strtol(payload.substr(i, 2).c_str(), nullptr, 16));
            esp_ble_gap_config_adv_data_raw(data.data(), data.size());
            esp_ble_adv_params_t p{};
            p.adv_int_min = p.adv_int_max = 0x20;
            p.adv_type    = ADV_TYPE_NONCONN_IND;
            p.own_addr_type = BLE_ADDR_TYPE_PUBLIC;
            p.channel_map = ADV_CHNL_ALL;
            esp_ble_gap_start_advertising(&p);
            delay(500);
            esp_ble_gap_stop_advertising();
```

Ce service reçoit la trame hexadécimale construite par l'intégration, la décode octet par octet et l'émet en BLE non-connectable, reproduisant exactement le comportement du daemon Jeedom.

## Aide au débogage

Si l'intégration ne se comporte pas comme attendu (module non détecté, commande sans effet, erreur au démarrage), voici comment collecter les informations nécessaires pour signaler un problème.

### Activer les logs de débogage

Ajouter les lignes suivantes dans `configuration.yaml` :

```yaml
logger:
  default: warning
  logs:
    custom_components.odace_sfsp: debug
```

Redémarrer Home Assistant (**Paramètres → Système → Redémarrer**).

### Récupérer les logs

1. Aller dans **Paramètres → Système → Journaux**.
2. Filtrer sur `odace_sfsp`.
3. Copier les entrées pertinentes (trames hex reçues ou émises, messages d'erreur).

### Signaler un problème

Ouvrir une issue sur le [dépôt GitHub](https://github.com/slemeur91/odace_sfsp/issues) en incluant :

- La description du dysfonctionnement (ce qui est attendu, ce qui se passe réellement).
- Le type de module concerné (`switch`, `dcl`, `generic`, etc.) et son UUID si connu.
- Les logs de débogage collectés ci-dessus.
- La version de l'intégration et de Home Assistant.

---

[releases-shield]: https://img.shields.io/github/release/slemeur91/odace_sfsp.svg?style=flat-square
[releases]: https://github.com/slemeur91/odace_sfsp/releases
