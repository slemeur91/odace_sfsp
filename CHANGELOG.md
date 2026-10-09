# Changelog

Toutes les évolutions notables de l'intégration Schneider Odace SFSP sont consignées ici.

Le détail de chaque version, bêtas comprises, est disponible sur la page des [releases](https://github.com/slemeur91/odace_sfsp/releases).

## [2.1.0] - 2026-10-09

### Ajouts

- Support des contrôleurs Bluetooth 5 en mode HCI : l'émission passe par l'advertising étendu, avec repli automatique sur les commandes historiques pour les contrôleurs Bluetooth 4 (PR [#3](https://github.com/slemeur91/odace_sfsp/pull/3), merci @NeySlim).
- [Wiki](https://github.com/slemeur91/odace_sfsp/wiki) : installation, contrôleurs, gestion des modules, services, automatisations, migration depuis Jeedom, dépannage et protocole.
- Documentation en anglais (`README_EN.md`).
- Fichier `CHANGELOG.md`.

### Corrections

- Commandes rapprochées en mode HCI : les émissions sont désormais sérialisées par contrôleur. Allumer plusieurs modules en même temps ne corrompt plus les trames.
- Interface `hci` qui change au redémarrage ou en cours de session : l'interface est vérifiée avant chaque envoi, résolue à nouveau si elle a disparu, et le nouveau nom est enregistré dans la configuration.
- Liste des contrôleurs à l'ajout : l'adresse MAC n'est plus affichée `00:00:00:00:00:00` au premier démarrage.
- Advertising étendu : le jeu d'annonces est libéré même en cas d'échec, et un statut HCI illisible est signalé dans les journaux.

### Suppressions

- Dernières références au module `dimmer`, non supporté : option du service `add_device`, description du service `bind_device` et constante `groupdimmer`.

### Documentation

- README : procédure d'ajout d'un module corrigée (appuyer sur le module avant d'ouvrir l'écran, association automatique, saisie manuelle).
- README : export et import précisés (format JSON, écritures acceptées, lignes ignorées, pas de trame d'association à l'import).
- README : remerciements aux contributeurs, lien vers le wiki, captures d'écran mises à jour et affichées côte à côte.

## [2.0.1] - 2026-09-21

Version diffusée en bêta uniquement, de `v2.0.1_beta1` à `v2.0.1_beta11`. Elle porte surtout sur le volet roulant, le désappairage des modules et la détection de l'interface `hci`.

### 2.0.1_beta11 - 2026-09-21

- Suppression du module `dimmer`, non supporté : décodage des trames, constantes et association.
- Association : un délai minimal de 5 secondes entre deux trames d'association évite les envois en rafale quand le bouton du module est maintenu.
- Émission : un type de module inconnu produit une erreur explicite dans les journaux au lieu d'une exception.
- Scènes : un avertissement signale un paramètre `options` qui écrase le type de scène.
- Volet : l'état « fermé » n'est plus affiché pendant la fermeture.
- Services : ils sont désenregistrés au déchargement de l'intégration.
- Réception : la mémoire de déduplication des trames est plafonnée à 200 modules.

### 2.0.1_beta10 - 2026-09-19

- Interface `hci` : l'intégration vérifie que l'interface répond réellement, avec `hcitool`. Elle détecte ainsi une interface fantôme qui porte une adresse MAC valide mais dont la radio est éteinte.
- Changement de contrôleur : le titre de l'intégration reprend le nom de la nouvelle interface.

### 2.0.1_beta9 - 2026-09-18

- Remplacement d'appels dépréciés du registre des appareils de Home Assistant.
- Les modules `dcl`, `generic`, `plug` et `scene` sont rattachés à l'appareil passerelle, comme les volets et les interrupteurs depuis la bêta 8.

### 2.0.1_beta8 - 2026-09-07

- Contrôleur fantôme : l'interface réelle est recherchée directement dans le système, sans passer par la liste des adaptateurs de Home Assistant.
- Désappairage : si le module répond « associé » au lieu de « désappairé », une seconde trame est envoyée.
- Les volets et les interrupteurs sont rattachés à l'appareil passerelle.

### 2.0.1_beta7 - 2026-08-30

- Correction d'une erreur de syntaxe (virgule en trop) dans les fichiers de traduction français et anglais.

### 2.0.1_beta6 - 2026-08-30

- Désappairage : il se lance depuis le menu **Supprimer un module**, qui propose « Supprimer de HA uniquement » ou « Désappairer le module puis supprimer de HA ». Le bouton ajouté dans la bêta 5 est retiré.
- Interface `hci` : au démarrage, l'intégration détecte une interface fantôme (adresse MAC nulle) et bascule sur l'interface réelle.

### 2.0.1_beta5 - 2026-08-30

- Désappairage d'un module : un bouton « Désappairer et supprimer » est ajouté sur chaque module commandable. Après un appui sur le bouton physique du module, celui-ci est désappairé puis supprimé de Home Assistant, avec suppression forcée au bout de 15 secondes sans confirmation.
- Volet : la position demandée est envoyée telle quelle au module, sans la double inversion qui faussait le positionnement.
- Le réappairage automatique introduit dans la bêta 3 est retiré.

### 2.0.1_beta4 - 2026-08-21

- Volet : le format de trame propre au volet (UUID suivi de `FF`) ne s'applique plus qu'à lui. Les autres modules retrouvent le format d'origine (`FF` suivi de l'UUID), que la bêta 3 avait modifié pour tous.

### 2.0.1_beta3 - 2026-08-21

- Encodage de la cible dans les trames de commande : l'UUID est suivi de `FF`.
- Réappairage automatique quand un module se déclare désappairé, limité à une tentative par 30 secondes.

### 2.0.1_beta2 - 2026-08-20

- Émission HCI : le scan Bluetooth est coupé avant l'activation de l'advertising. Sur la plupart des contrôleurs, les deux sont incompatibles, ce qui pouvait produire des trames ignorées ou corrompues.

### 2.0.1_beta1 - 2026-08-19

- Volet : une position inconnue (module non calibré) est gérée, et la position est bornée entre 0 et 100 %.
- Volet : décodage des réponses d'association (refusée, acceptée, associé, désappairé), des groupes et des scènes.

### Hors bêta, entre la 2.0.0 et la 2.0.1_beta1

- Ajout des notices des modules dans le dossier `hardware/`.
- Ajout de la licence.
- Retrait du module `dimmer` de la liste des modules supportés du README.

## [2.0.0] - 2026-06-14

### Ajouts

- Contrôleur Bluetooth distant sur ESP32 via l'API native ESPHome : sélection de l'appareil et du service, détection de l'adresse MAC Bluetooth.
- Émission ESPHome alignée sur la logique du mode HCI.
- Filtrage des trames reçues.

### Modifications

- Vocabulaire de l'interface : « périphérique » devient « module », « dongle » devient « contrôleur ».
- Documentation et captures d'écran refondues.

[2.1.0]: https://github.com/slemeur91/odace_sfsp/compare/v2.0.1_beta11...v2.1.0
[2.0.1]: https://github.com/slemeur91/odace_sfsp/compare/V2.0.0...v2.0.1_beta11
[2.0.0]: https://github.com/slemeur91/odace_sfsp/releases/tag/V2.0.0
