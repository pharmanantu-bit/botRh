# Specs — Module Planning d'équipe (façon Mon Planning Pharma + IA) pour botRh

> Document de conception **uniquement** — aucun code applicatif. Base de travail à relire/ajuster ensemble.
> Établi à partir de l'exploration écran par écran de Mon Planning Pharma (MPP) + son changelog complet.

---

## 0. Objectif & principe directeur

Internaliser dans botRh un **planning d'équipe d'officine** équivalent à Mon Planning Pharma, **en mieux** sur deux axes :

1. **Pont avec les relevés botRh** : MPP ne connaît que le *planifié*. botRh connaît déjà le *réel* (relevés d'heures des salariés). → comparaison planifié/réel et **fin de mois automatiques**.
2. **IA génératrice** : MPP *contrôle* (passif) ; botRh *génère et répare* le planning (actif), l'humain valide.

**Règle d'architecture fondamentale (à ne jamais enfreindre) :**
- **Cœur déterministe** = tous les calculs et contrôles (heures, effectifs, écarts). Fiable, exact, **100 % local** → marche sur PythonAnywhere gratuit (pas d'Internet sortant).
- **IA = surcouche** : elle propose, **le moteur vérifie**, **l'humain valide**. L'IA ne calcule jamais elle-même. (Besoin d'Internet → local / PA payant.)

---

## 1. Modèle de données

### 1.1 Collaborateur (réutilise l'Équipe botRh existante + 2 champs)
botRh a déjà `employees.csv` + `profils_rh.json`. On **étend** la fiche, on ne refait pas un module Équipe :
- `couleur` *(nouveau)* — pastille du collaborateur sur la frise (palette fixe).
- `fonction` *(nouveau, structuré)* — **Pharmacien / Préparateur / Autre** (aujourd'hui `poste` est en texte libre ; on garde `poste` et on ajoute une fonction normalisée pour le contrôle des effectifs).
- réutilisés tels quels : nom, prénom, email, `statut` actif/archivé.
- **Heures contractuelles / semaine** (déjà discuté) — pour l'écart planifié vs dû.

> Permissions / « vue du planning » de MPP (accès en ligne des salariés) = **hors V1** (les salariés n'accèdent qu'à leur relevé via lien).
> Archivé = conservé pour l'historique (botRh le fait déjà).

### 1.2 Trame (modèle de semaines en rotation) — **versionnée dans le temps**
- `date_demarrage`
- `nb_semaines_tournantes` (1 à **15**)
- `semaine_demarrage` (A, B, C…)
- `commentaire`
- `activee` (bool) — **seules les trames activées génèrent le planning**
- `horaires_ouverture` (par jour ISO)
- `collaborateurs_inclus` (sous-ensemble de l'équipe ; les autres sont « à inclure »)
- **Horaires** : pour chaque `(collaborateur × semaine_rotation A/B… × jour 1–7)` → **0 à 2 créneaux** `{debut:"HH:MM", fin:"HH:MM"}` (saisie tapée).

**Règles de gestion (vues dans MPP) :**
- Le **planning n'est PAS stocké semaine par semaine** : il est **calculé** depuis la trame.
- Les **anciennes trames sont conservées** (sinon perte d'historique). On ne les écrase pas : pour changer, on **crée une nouvelle trame** à une date → chaîne versionnée.
- Pour une date donnée : trame active applicable = la plus récente activée dont `date_demarrage ≤ date`.
- Semaine de rotation d'une date = `((n° de semaine ISO − semaine de démarrage) mod nb_semaines_tournantes)` mappée sur A/B/C…
- Confort : copier les horaires d'une semaine / d'un autre collaborateur, « remise à zéro » d'un jour, calcul temps réel du total hebdo.

### 1.3 Changement ponctuel (écart à la trame)
Journal des modifications réelles, **par mois**, **par collaborateur** :
- `{ collaborateur, motif, date (ou plage), horaires_trame (avant) → horaires_reels (apres) }`
- **Motifs** observés : `Non catégorisé`, `Heures sup/récup/échanges`, `Contrat ponctuel`, `Repos compensatoire`, `Garde`, et **absences** : `Congés payés`, `Arrêt maladie`, `Congé maternité`, `Accident du travail`, `Formation`, `Autre`.
- Affichage : **avant en rouge (la trame) → après en vert (le réel)**.

### 1.4 Effectifs (paramètres de contrôle)
- Collaborateurs **comptés**, regroupés par **rôle** (Pharmacien / Préparateur / Autre / Non défini).
- **Effectif minimum par jour ET par tranche horaire** : ex. Lundi `09:00–13:00 → min 5`, `13:30–19:00 → min 6`.
- **Présence pharmacien obligatoire** pendant les heures d'ouverture (contrôle dédié).
- Contrôle global activable/désactivable. « Copier un jour ».

### 1.5 Planning effectif (calculé, non stocké)
`Planning(date) = Trame active(date) appliquée → puis Changements(date) superposés`.

---

## 2. Le moteur déterministe (cœur, sans IA, local)

Le « vérificateur » exact. Entrées : trames, changements, profils (rôle, contractuel), paramètres effectifs, relevés. Sorties : totaux + liste d'anomalies précises.

**Calculs**
- Durée d'un créneau (0 si `fin ≤ debut`), total/jour, total/semaine, **cumul/mois** (réparti jour par jour, semaines à cheval gérées).
- Écart **planifié vs contractuel** (par semaine).
- Écart **planifié vs réel** (planning vs relevés) → base de la fin de mois.

**Contrôles (alertes)**
- Créneau invalide, chevauchement, repos < 11h, amplitude, jours consécutifs.
- **Effectifs** : pour chaque tranche d'ouverture, effectif présent (par rôle) vs minimum requis → « découvert » / sous-effectif.
- **Présence pharmacien** sur toute l'amplitude d'ouverture.

**Coloration différentielle** (comme MPP) : par rapport à la trame, *clair = heures en moins*, *foncé = heures en plus* ; **losange** sur les jours modifiés ; bord = aujourd'hui.

---

## 3. La couche IA (surcouche)

- **Sources de contraintes (DÉCISION Q4)** : pas de module de disponibilités séparé. La **trame de chaque collaborateur EST l'accord validé avec lui** (ses jours/horaires habituels = sa disponibilité). Les **congés/absences** sont simplement **notés dans les Changements**. L'IA travaille donc à partir de : **trame + absences notées + règles d'effectifs (rôles)**.
- **Génère / répare** un planning depuis ces contraintes (trame, contractuel, absences, effectifs mini, rôles). En pratique, surtout de la **réparation** : quand une absence crée un trou, l'IA propose un remplacement/échange respectant les trames et les effectifs.
- **Boucle qualité** : IA propose → **moteur vérifie** → IA corrige jusqu'au « tout vert ».
- **Langage naturel** : *« équilibre les heures »*, *« il manque un pharmacien vendredi soir, propose une solution »*, *« remplace Marie par Paul mardi »*.
- **Validation humaine obligatoire** — jamais d'écriture automatique (présence pharmacien = obligation légale).
- Réutilise l'infra IA existante de botRh (`assistant_rh.py` transport, pattern `agent_recrutement.py`, moteurs fake/mistral/claude, contrat chat `{reply, outils_utilises, actions}`).
- **Dégradation** : sans Internet (PA gratuit), l'IA renvoie un 502 clair ; **le cœur reste pleinement utilisable**.

---

## 4. Les écrans (UI)

### 4.1 La frise (composant visuel UNIQUE, réutilisé partout)
- Axe horaire horizontal **calé automatiquement sur les horaires d'ouverture** (DÉCISION : amplitude auto, pas fixe), **1 ligne par collaborateur**, regroupées **par jour**.
- **Barres colorées proportionnelles** (couleur = collaborateur), horaire écrit dedans (optionnel).
- Sert pour : l'aperçu d'une **trame** (par semaine A/B…) **et** le **planning** réel.
- Modes d'affichage : **frise** / **tableau** / **texte** (1 collaborateur, pour copier-coller dans un mail).

### 4.2 UNE SEULE entrée de menu « Planning » + sous-onglets (DÉCISION)
**Règle d'interface** : **une seule entrée `🗓️ Planning` dans la barre latérale** (sidebar). **Aucun autre item de menu** pour le planning. Tout le reste vit en **sous-onglets à l'intérieur de la page Planning** (comme MPP : `Planning | Options | Effectifs | …`).

Sous-onglets internes (un seul blueprint, sous-routes type `?onglet=` ou `/admin/planning/<onglet>`) :
- **Planning** *(défaut)* : la frise + changements + alertes de couverture. Navigation semaine/mois.
- **Trame** : config (date démarrage, nb semaines A/B, semaine départ, commentaire, horaires d'ouverture) + collaborateurs inclus + saisie Horaires + aperçu frise par semaine.
- **Équipe** : réglages planning par collaborateur (couleur, fonction, inclusion/exclusion de la trame, accès à ses horaires). *(Comme l'Équipe de MPP.)*
- **Effectifs** : rôles comptés + effectifs mini par créneau/jour + contrôle pharmacien.
- **Changements** : journal mensuel (absences, heures sup, etc.).
- **Totaux / Fin de mois** : récap période (26→25) planifié vs réel + clôture payer.
- **Options** : filtres (collaborateurs, jours), période, mode d'affichage, masquer/afficher.

### 4.3 Double affichage du planning d'un collaborateur (DÉCISION)
Le planning d'un collaborateur est **surfacé à deux endroits**, avec **une seule source de vérité** (les données du salarié stockées une fois) :
1. dans le **sous-onglet « Équipe » du module Planning** (gestion) ;
2. **sur sa fiche dans « 👥 Équipe & RH »** : on **affiche son planning** (sa frise / ses horaires de la semaine + ses totaux planifié/réel) directement dans son dossier, avec un lien vers le module Planning complet.

> Pas de duplication de données : `couleur`/`fonction` et les horaires viennent du même stockage ; les deux pages sont juste deux **vues**.

---

## 5. 🌉 Le pont botRh (la valeur unique)

### 5.1 Totaux horaires = planifié ✕ réel automatique
Colonnes (comme MPP) : Collaborateur · **Trame (planifié)** · **Heures travaillées (réel)** · Férié chômé · Absences (typées) · Total · Heures sup · Jours travaillés.
- **Planifié** = la trame (nouveau module).
- **Réel** = les **relevés botRh** (`reponses_*.json`, `heures_plus`/`heures_moins`) — **déjà collectés**.
- **Écart / heures sup** = calculés automatiquement. *(MPP ne peut pas : il ignore le réel.)*

### 5.2 Période de paie (DÉCISION — ancrée sur le 25, pas le mois civil)
**Confirmé dans le code botRh** (`app.py`, route `/releve` + `extraire_detail_jours`) :
- La période d'un relevé court **du 26 du mois précédent au 25 du mois courant inclus** (`JOUR_DEBUT_PERIODE = 26`, `JOUR_FIN_PERIODE = 25`) — aucun jour compté deux fois, aucun oublié.
- **Clôture unique le 25** (`JOUR_CLOTURE = 25`, identique à la date annoncée — plus de tolérance cachée au 28).
- Les heures faites **après le 25 sont payées le mois suivant**.
- Fenêtre administrative **25 → 30** : rappel de validation le 25 au soir (runner), récap paie le 26 au matin, envoi expert-comptable, génération des paies, virements reçus avant la fin du mois.

➡️ **Conséquence pour le planning** : les **Totaux horaires** et la **Fin de mois** doivent utiliser **la même période que les relevés** (26 → 25), **pas le mois calendaire**. Le rapprochement planifié/réel se fait jour à jour sur cette même fenêtre. *(MPP raisonne en mois civil → incompatible. botRh est déjà calé dessus : avantage décisif.)*

**Coupure de paie au 25 (RÈGLE) :** pour les **paiements**, toutes les heures **+ / −** faites **après le 25 sont reportées sur la paie du mois suivant** (elles appartiennent, par date, à la période suivante). Ce n'est **pas une banque d'heures** (pas de solde discrétionnaire) : c'est une **coupure de date déterministe**. Le moteur affecte donc chaque jour à la bonne période de paie selon qu'il est avant/après le 25.
> La fenêtre du relevé (26 M-1 → 25 M, dans `reponses_{mois}_{annee}.json`) et la **fenêtre de paie** (coupure au 25) coïncident désormais exactement : rien n'est compté deux fois ni oublié.

### 5.3 Fin de mois = heures sup/moins PAYÉES chaque période (pas de banque annuelle)
**DÉCISION (Q3)** : pas de compteur d'heures cumulé sur l'année. À chaque clôture, les heures **+ / −** sont **payées** (ou ajustées) sur la période, point. Donc **pas de report/banque** à gérer.

**Version botRh** : le net **h+ / h−** de la période se calcule **tout seul** depuis les relevés (`heures_plus`/`heures_moins`) comparés au **planifié** (trame). Écran **clair et lisible** pour la fenêtre 25→30 (valider → exporter expert-comptable → paies). → on supprime le point faible « complexe et moche » de MPP, sans banque d'heures.

### 5.4 Modèle de confiance & réconciliation (PRINCIPE CLÉ — anti-abus)
- **Source de vérité = le planning du GESTIONNAIRE** (trame + changements saisis **par le gestionnaire**). C'est lui qui détermine les heures dues/payées — **PAS** le relevé du salarié.
- Le **relevé du salarié = déclaration de contrôle**, **croisée** avec le planning. On **ne paie que ce que le planning du gestionnaire justifie**.
- Réconciliation par salarié et par période (26→25) : comparer **h+/h− du planning (gestionnaire)** vs **h+/h− du relevé (salarié)**. Trois cas :
  1. **Concordance** → OK, on paie les heures sup **validées**.
  2. **Salarié > planning** (déclare plus que l'enregistré) → 🚩 : soit **surévaluation** (malveillance → on **ne paie pas** l'injustifié), soit **oubli du gestionnaire** d'inscrire un vrai changement → le **gestionnaire tranche**.
  3. **Planning > salarié** (retard/absence noté par le gestionnaire mais non déclaré par le salarié) → le salarié **ne peut pas masquer** retard/absence → la valeur du gestionnaire **fait foi**.
- **But** : accord des deux côtés → paie juste, **pas de litige** salarié.
- **Implication produit** : l'écran **Totaux / Fin de mois** doit afficher **planning vs relevé côte à côte** (par jour et par période) et **surligner les écarts** (cas 2 et 3) pour validation rapide.
- Ce double enregistrement (gestionnaire **et** salarié) est volontaire : il rattrape aussi bien les **oublis du gestionnaire** que les **omissions/abus du salarié**.

---

## 6. Périmètre

### V1 (à viser)
Équipe (réutilisée + couleur + fonction) · Trames en rotation · Frise colorée · Effectifs auto (+ pharmacien) · Changements typés · Totaux horaires (planifié vs réel) · Fin de mois auto · export/impression.

### Différé (utile surtout avec accès en ligne des salariés)
Notifications · Demandes de changement des salariés · Permissions / visibilité par salarié · Périodes bloquées (brouillon) · Vacances scolaires · Mémo.

### En plus de MPP (différenciateurs)
IA génératrice/réparatrice · Pont relevés (totaux + fin de mois automatiques).

---

## 7. Phasage proposé (cœur d'abord, IA ensuite)

- **Phase 0** — Équipe : ajouter `couleur` + `fonction` à la fiche existante.
- **Phase 1** — Modèle trames **avec rotation dès le départ** (2 semaines A/B, généralisable à N) + moteur de calcul d'heures (créneaux, totaux, rotation, cumul sur la période de paie 26→25).
- **Phase 2** — Frise (rendu) + saisie des horaires de trame.
- **Phase 3** — Effectifs (rôles + mini par créneau + contrôle pharmacien) + alertes sur la frise.
- **Phase 4** — Changements ponctuels (absences/heures sup typées) → planning = trame + changements.
- **Phase 5** — Totaux horaires + Fin de mois **branchés sur les relevés** (le pont).
- **Phase 6** — Surcouche IA (génération/équilibrage/réparation), avec validation humaine.

Chaque phase est livrable et testable seule (tests de fumée comme le reste de botRh).

---

## 8. Contraintes techniques (botRh)

- **PythonAnywhere gratuit = pas d'Internet sortant** → cœur 100 % local ; IA seulement en local / PA payant (dégradation propre).
- Stockage : fichiers JSON via `_lire_json`/`_ecrire_json` (cache mono-thread, « dernier qui écrit gagne » — pas de verrou).
- Réutiliser : CSRF, pattern Blueprint, export openpyxl, UI (`.carte/.btn/.stat-card`, palette `#1F4E79`), infra IA.
- Règle projet : **valider en local, commit local, PAS de push** (push = auto-deploy GitHub Actions).

---

## 9. Glossaire MPP → botRh

| Mon Planning Pharma | botRh |
|---|---|
| Collaborateur (fonction, couleur) | Salarié (`profils_rh.json`) + `couleur` + `fonction` |
| Trame (rotation) | Nouveau : modèle versionné `planning_trame_*.json` |
| Planning (frise) | Nouveau : rendu calculé (trame + changements) |
| Effectifs | Nouveau : `parametres_effectifs.json` |
| Changements | Nouveau : journal `planning_changements_*.json` |
| Heures travaillées | **Relevés existants** (`reponses_*.json`) |
| Totaux horaires / Fin de mois | Nouveau, **alimenté par les relevés** |

---

## 10. Questions ouvertes (à trancher ensemble)
1. ✅ **DÉCIDÉ** — Couleurs : **palette imposée** (pas de choix libre par salarié).
2. ✅ **DÉCIDÉ** — Amplitude de la frise : **auto selon les horaires d'ouverture**.
3. ✅ **DÉCIDÉ** — Pas de banque d'heures annuelle : les h+/h− sont **payées chaque période**. Période **ancrée sur le 25** (clôture unique le 25, période 26→25), comme les relevés (voir §5.2/§5.3).
4. ✅ **DÉCIDÉ** — Pas de module de dispos séparé : la **trame = l'accord/disponibilité** de chaque collaborateur ; les **congés se notent en Changements**. L'IA = trame + absences + effectifs (voir §3).
5. ✅ **DÉCIDÉ** — **Rotation indispensable dès la V1** : l'officine tourne en **Semaine A / Semaine B (2 semaines tournantes)**, car les semaines ne sont pas identiques. Le modèle reste généralisable à N semaines (jusqu'à 15), mais **2** est le cas réel par défaut.
