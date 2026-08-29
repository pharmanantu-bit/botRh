"""Agent RH outillé (function calling) — le chaînon « conseil → action ».

L'assistant peut désormais LIRE les données de l'officine via des outils :
relevés manquants, fiche salarié, échéances, écart heures/planning, annuaire.

Principe RGPD (identique au reste de botRh, « option B ») : la boucle agent
tourne ENTIÈREMENT côté serveur, là où vivent les données. Seul le RAISONNEMENT
passe par le LLM. Avant tout envoi, les identités sont pseudonymisées
(« Employé A/B... ») — dans les messages de l'utilisateur ET dans les résultats
d'outils. Le modèle ne voit donc jamais un vrai nom. À l'affichage, on
ré-identifie en local (les étiquettes redeviennent les prénoms).

Périmètre : LECTURE SEULE. Aucune écriture/suppression. Les implémentations
d'outils vivent dans app.py (accès aux données) et sont injectées ici via le
callback `executer(nom, args, annuaire)` — pas d'import circulaire : ce module
n'importe que assistant_rh.
"""
import os
import re
import json

from assistant_rh import (
    construire_table, annuaire_pseudo, pseudonymiser_texte, reidentifier, _post_json,
)

MAX_TOURS = 8  # borne le nombre d'allers-retours d'outils (coût/latence)

SYSTEM_AGENT = (
    "Tu es l'assistant RH d'une pharmacie d'officine en France : à la fois expert "
    "RH / droit du travail (Code du travail, CCN pharmacie d'officine IDCC 1996) ET "
    "agent capable d'AGIR sur les données de l'officine grâce à des outils.\n"
    "RÈGLES :\n"
    "- Pour toute question portant sur les salariés, les relevés d'heures, les "
    "échéances ou le planning, APPELLE l'outil adapté plutôt que de deviner. "
    "N'invente jamais une donnée : si un outil ne renvoie rien, dis-le.\n"
    "- Les salariés sont anonymisés en « Employé A », « Employé B »... Utilise ces "
    "étiquettes telles quelles et TOUJOURS complètes (« Employé C », jamais « C » ni "
    "« Employés C, D ») dans tes appels d'outils comme dans ta réponse ; n'écris JAMAIS "
    "de nom de famille. L'affichage ré-identifiera localement.\n"
    "- Pour les questions purement juridiques/RH (sans donnée nominative), réponds "
    "directement, en français, de façon concrète et actionnable, et signale quand "
    "un point délicat relève de l'avocat ou de l'expert-comptable. Information "
    "générale, pas un conseil juridique engageant. Aucun conseil médical.\n"
    "- Tu peux AGIR sur le planning et les dossiers avec les outils d'ÉCRITURE "
    "(ajouter_absence, modifier_horaires_jour, retablir_horaires_jour, supprimer_absence, "
    "traiter_demande_conges, envoyer_demande_collaborateur, ajouter_note_journal, "
    "mettre_a_jour_profil, envoyer_mail, envoyer_relance). Avant d'écrire, vérifie ce "
    "qu'il faut (planning du jour, solde de congés, demandes en attente) avec les outils "
    "de LECTURE. Si une information indispensable manque (date, motif, horaires…), "
    "demande-la au lieu de deviner.\n"
    "- Quand l'utilisateur t'ANNONCE un fait RH (« X est malade jusqu'à mercredi », "
    "« Y sera en formation lundi », « Z arrive à 10h demain », « accepte les congés de "
    "W »), c'est une demande d'ENREGISTREMENT : appelle l'outil d'écriture adapté "
    "(ajouter_absence pour plusieurs jours, modifier_horaires_jour pour un jour) dès "
    "que tu as les informations, puis réponds à sa question. Ne te contente JAMAIS de "
    "reformuler le fait comme s'il était déjà enregistré.\n"
    "- Chaque outil d'écriture renvoie son résultat réel : « FAIT » (exécuté) ou "
    "« PROPOSITION » (l'utilisateur confirmera d'un clic). Rapporte EXACTEMENT ce "
    "statut : ne dis jamais qu'une chose est faite si l'outil a répondu PROPOSITION, "
    "et ne redemande pas confirmation si l'outil a répondu FAIT.\n"
    "- RELEVÉS D'HEURES & PAIE : releve_du_mois / stats_heures répondent aux questions "
    "chiffrées (heures sup, soldes) ; corriger_releve, valider_releve et "
    "envoyer_recap_comptable touchent à la PAIE : ils renvoient TOUJOURS une "
    "PROPOSITION à confirmer, même en mode autonome. Avant d'envoyer au comptable, "
    "appelle apercu_recap_comptable et résume-le à l'utilisateur.\n"
    "- DOSSIER SALARIÉ : dossier_salarie donne documents, suggestions, checklists et "
    "statut ; appliquer_suggestion / ignorer_suggestion, cocher_checklist, "
    "changer_statut, valider_document, retyper_document, generer_attestation et "
    "envoyer_attestation agissent dessus. Utilise les ids renvoyés par dossier_salarie.\n"
    "- MAILS RH : mails_rh_du_jour donne la synthèse des e-mails reçus (comptable, "
    "salariés, administratif). Quand une tâche en découle (préparer un document, "
    "relancer quelqu'un, noter une échéance), PROPOSE l'action avec l'outil adapté au "
    "lieu de seulement la citer. Si la synthèse est ancienne, propose actualiser_mails.\n"
    "- ANNULATION : si l'utilisateur veut revenir en arrière sur ce que tu viens de "
    "faire, appelle annuler_derniere_action (ne refais pas l'inverse à la main).\n"
    "- Les outils preparer_relance / preparer_attestation / preparer_mail ne font que "
    "PRÉPARER un brouillon ou un lien à ouvrir : ne prétends jamais que c'est envoyé.\n"
    "- Réponds toujours en français, clairement, et synthétise le résultat des "
    "outils au lieu de le recracher brut. TEXTE BRUT façon messagerie : pas de "
    "Markdown (aucun astérisque, aucun #), listes avec des tirets, phrases courtes."
)

# --- Catalogue d'outils (format neutre, converti par moteur) ---
# Lecture seule + ACTION (préparent un livrable à confirmer). `params` : nom ->
# (type JSON, description). `requis` : args obligatoires.
OUTILS_SPECS = [
    {
        "nom": "releves_manquants",
        "description": "Liste les salariés actifs qui n'ont PAS encore rendu leur "
                       "relevé d'heures pour un mois. Sans argument : mois en cours.",
        "params": {
            "mois": ("integer", "Mois 1-12 (optionnel, défaut = mois courant)"),
            "annee": ("integer", "Année (optionnel, défaut = année courante)"),
        },
        "requis": [],
    },
    {
        "nom": "profil_salarie",
        "description": "Fiche d'un salarié : poste, type de contrat, dates clés "
                       "(entrée, fin de CDD, fin de période d'essai, prochaine visite "
                       "médicale), alertes en cours et documents obligatoires manquants.",
        "params": {
            "employe": ("string", "Étiquette du salarié, ex. « Employé A »"),
        },
        "requis": ["employe"],
    },
    {
        "nom": "echeances_a_venir",
        "description": "Échéances RH à venir sur tous les salariés actifs : fins de "
                       "CDD, fins de période d'essai, visites médicales, documents qui "
                       "expirent, anniversaires d'ancienneté.",
        "params": {
            "jours": ("integer", "Horizon en jours (optionnel ; informatif, les "
                                 "alertes ont déjà leur propre seuil)"),
        },
        "requis": [],
    },
    {
        "nom": "lister_employes",
        "description": "Annuaire des salariés actifs (étiquette, poste, type de contrat).",
        "params": {},
        "requis": [],
    },
    # --- Outils ACTION : préparent un livrable à CONFIRMER (jamais d'envoi/écriture auto) ---
    {
        "nom": "preparer_relance",
        "description": "Prépare un brouillon d'e-mail de relance (rappel du relevé "
                       "d'heures) pour un salarié retardataire. Ne l'envoie PAS : produit "
                       "un brouillon que l'utilisateur relira et enverra lui-même.",
        "params": {"employe": ("string", "Étiquette du salarié, ex. « Employé A »")},
        "requis": ["employe"],
    },
    {
        "nom": "preparer_attestation",
        "description": "Prépare l'attestation de travail (page imprimable pré-remplie) "
                       "d'un salarié. Produit un lien à ouvrir, n'imprime ni n'envoie rien.",
        "params": {"employe": ("string", "Étiquette du salarié")},
        "requis": ["employe"],
    },
    {
        "nom": "preparer_mail",
        "description": "Rédige un brouillon d'e-mail RH libre à un salarié (convocation, "
                       "information…). Ne l'envoie PAS : brouillon à relire et envoyer soi-même.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "sujet": ("string", "Objet du mail"),
                   "corps": ("string", "Corps du mail")},
        "requis": ["employe", "corps"],
    },
    # --- Outils LECTURE planning (contexte avant d'agir) ---
    {
        "nom": "planning_jour",
        "description": "Qui travaille à une date donnée et à quels horaires (trame + "
                       "changements ponctuels − absences − fériés), avec les absents.",
        "params": {"date": ("string", "Date AAAA-MM-JJ (défaut : aujourd'hui)")},
        "requis": [],
    },
    {
        "nom": "planning_collaborateur",
        "description": "Horaires effectifs d'un salarié sur la semaine contenant une date "
                       "(défaut : semaine en cours), jour par jour.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "date": ("string", "Une date AAAA-MM-JJ de la semaine voulue (optionnel)")},
        "requis": ["employe"],
    },
    {
        "nom": "solde_conges",
        "description": "Solde de congés payés d'un salarié sur la période en cours "
                       "(droit, report, posés, restant) et ses plages de CP.",
        "params": {"employe": ("string", "Étiquette du salarié")},
        "requis": ["employe"],
    },
    {
        "nom": "demandes_conges_en_attente",
        "description": "Demandes de congés déposées par les salariés et non encore "
                       "traitées (id, salarié, dates, commentaire).",
        "params": {},
        "requis": [],
    },
    {
        "nom": "absences_en_cours",
        "description": "Absences prolongées en cours ou à venir (congés, arrêts…) "
                       "sur les 60 prochains jours.",
        "params": {},
        "requis": [],
    },
    # --- Outils ÉCRITURE : exécutés (mode autonome) ou proposés (mode validation) ---
    {
        "nom": "ajouter_absence",
        "description": "Déclare une absence prolongée au planning (congés payés, arrêt "
                       "maladie, formation…) sur une plage de dates. Refusée si elle "
                       "chevauche une absence existante.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "debut": ("string", "Premier jour AAAA-MM-JJ"),
                   "fin": ("string", "Dernier jour AAAA-MM-JJ (défaut = début)"),
                   "motif": ("string", "Congés payés | Arrêt maladie | Accident du travail | "
                                       "Congé maternité | Congé parental | Formation | "
                                       "Congé sans solde | Absence non justifiée | "
                                       "Repos compensatoire | Garde | Autre"),
                   "commentaire": ("string", "Commentaire (optionnel)")},
        "requis": ["employe", "debut", "motif"],
    },
    {
        "nom": "supprimer_absence",
        "description": "Supprime l'absence prolongée d'un salarié couvrant une date.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "date": ("string", "Une date AAAA-MM-JJ couverte par l'absence")},
        "requis": ["employe", "date"],
    },
    {
        "nom": "modifier_horaires_jour",
        "description": "Change les horaires d'un salarié pour UNE date (retard, heures "
                       "sup, échange, jour non travaillé…). `creneaux` vide = jour non "
                       "travaillé. Un motif est obligatoire.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "date": ("string", "Date AAAA-MM-JJ"),
                   "creneaux": ("string", "Créneaux « 09:00-13:00, 14:00-19:00 » (max 2) ; "
                                          "vide = non travaillé"),
                   "motif": ("string", "Heures sup/récup/échanges | Contrat ponctuel | "
                                       "Repos compensatoire | Garde | Congés payés | Arrêt "
                                       "maladie | Formation | Absence non justifiée | Autre")},
        "requis": ["employe", "date", "motif"],
    },
    {
        "nom": "retablir_horaires_jour",
        "description": "Annule le changement ponctuel d'une date : le salarié retrouve "
                       "ses horaires de trame.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "date": ("string", "Date AAAA-MM-JJ")},
        "requis": ["employe", "date"],
    },
    {
        "nom": "traiter_demande_conges",
        "description": "Accepte ou refuse une demande de congés en attente (voir "
                       "demandes_conges_en_attente pour l'id). Accepter crée l'absence "
                       "« Congés payés » et prévient le salarié par mail.",
        "params": {"id": ("string", "Identifiant de la demande"),
                   "decision": ("string", "accepter | refuser"),
                   "motif_refus": ("string", "Motif en cas de refus (optionnel)")},
        "requis": ["id", "decision"],
    },
    {
        "nom": "envoyer_demande_collaborateur",
        "description": "Envoie à un salarié, dans son espace, une proposition de congés "
                       "ou une demande d'heures supplémentaires à laquelle il répondra.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "type": ("string", "conges | heures_sup"),
                   "debut": ("string", "Premier jour AAAA-MM-JJ"),
                   "fin": ("string", "Dernier jour AAAA-MM-JJ (congés) ; optionnel"),
                   "h_debut": ("string", "Heure de début HH:MM (heures sup)"),
                   "h_fin": ("string", "Heure de fin HH:MM (heures sup)"),
                   "commentaire": ("string", "Message (optionnel)")},
        "requis": ["employe", "type", "debut"],
    },
    {
        "nom": "ajouter_note_journal",
        "description": "Ajoute une note datée au journal RH d'un salarié (entretien, "
                       "augmentation, avertissement, formation…).",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "note": ("string", "Texte de la note"),
                   "type_evenement": ("string", "Entretien | Augmentation | Avertissement | "
                                                 "Formation | Congés | Autre (optionnel)")},
        "requis": ["employe", "note"],
    },
    {
        "nom": "mettre_a_jour_profil",
        "description": "Met à jour UN champ du dossier salarié : poste, "
                       "heures_contractuelles_hebdo, type_contrat, date_entree, date_fin, "
                       "fin_essai, visite_medicale, telephone, adresse, urgence_nom, "
                       "urgence_tel.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "champ": ("string", "Nom du champ (voir description)"),
                   "valeur": ("string", "Nouvelle valeur (dates au format JJ/MM/AAAA)")},
        "requis": ["employe", "champ", "valeur"],
    },
    {
        "nom": "envoyer_mail",
        "description": "ENVOIE réellement un e-mail à un salarié (convocation, "
                       "information…). Le texte est envoyé tel quel : rédige-le "
                       "complètement, en français, poli et sans nom de famille.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "sujet": ("string", "Objet"),
                   "corps": ("string", "Corps du message")},
        "requis": ["employe", "sujet", "corps"],
    },
    {
        "nom": "envoyer_relance",
        "description": "ENVOIE réellement l'e-mail de rappel du relevé d'heures à un "
                       "salarié retardataire (texte standard avec son lien).",
        "params": {"employe": ("string", "Étiquette du salarié")},
        "requis": ["employe"],
    },
]

# Outils qui MODIFIENT des données ou ENVOIENT quelque chose. En mode « validation »
# l'implémentation renvoie une PROPOSITION (carte à confirmer) ; en mode « autonome »
# elle exécute. Décision prise côté app (agent_outils.executer), jamais par le LLM.
# --- Outils RELEVÉS D'HEURES & PAIE (phase 1) + ANNULATION ---
OUTILS_SPECS += [
    {
        "nom": "releve_du_mois",
        "description": "Relevés d'heures d'un mois : par salarié, H+ / H−, statut "
                       "(validé, à valider, corrigé, manquant) et commentaire. Avec "
                       "`employe`, donne aussi le détail jour par jour s'il existe.",
        "params": {
            "mois": ("integer", "Mois 1-12 (optionnel, défaut = mois courant)"),
            "annee": ("integer", "Année (optionnel)"),
            "employe": ("string", "Étiquette « Employé X » pour le détail d'une seule personne (optionnel)"),
        },
        "requis": [],
    },
    {
        "nom": "stats_heures",
        "description": "Statistiques d'heures sur une période (plusieurs mois) : total "
                       "H+, H− et solde par salarié, et par mois si un seul salarié. "
                       "Sert aux questions du type « combien d'heures sup en juin ? », "
                       "« qui fait le plus d'heures depuis janvier ? ».",
        "params": {
            "mois_debut": ("integer", "Mois de début 1-12 (optionnel, défaut = 6 mois en arrière)"),
            "annee_debut": ("integer", "Année de début (optionnel)"),
            "mois_fin": ("integer", "Mois de fin 1-12 (optionnel, défaut = mois courant)"),
            "annee_fin": ("integer", "Année de fin (optionnel)"),
            "employe": ("string", "Étiquette « Employé X » (optionnel)"),
        },
        "requis": [],
    },
    {
        "nom": "apercu_recap_comptable",
        "description": "Aperçu du dossier PAIE du mois tel qu'il partirait au "
                       "cabinet comptable : par salarié, heures, majorations 25/50, "
                       "complémentaires, sujétion, congés ; puis ce qui bloque l'envoi "
                       "(relevés non validés, manquants, destinataires). À appeler AVANT "
                       "envoyer_recap_comptable.",
        "params": {
            "mois": ("integer", "Mois 1-12 (optionnel, défaut = mois courant)"),
            "annee": ("integer", "Année (optionnel)"),
        },
        "requis": [],
    },
    {
        "nom": "corriger_releve",
        "description": "PAIE — corrige les TOTAUX H+ / H− du relevé d'un salarié pour un "
                       "mois (erreur de saisie), ou saisit le relevé s'il ne l'a pas rendu. "
                       "La déclaration d'origine est conservée et le relevé repasse « à "
                       "valider ». Toujours soumis à validation de l'utilisateur.",
        "params": {
            "employe": ("string", "Étiquette « Employé X »"),
            "heures_plus": ("number", "Nouveau total d'heures en plus (décimal, ex. 4.5)"),
            "heures_moins": ("number", "Nouveau total d'heures en moins (décimal)"),
            "motif": ("string", "Motif de la correction (obligatoire, court)"),
            "mois": ("integer", "Mois 1-12 (optionnel, défaut = mois courant)"),
            "annee": ("integer", "Année (optionnel)"),
        },
        "requis": ["employe", "heures_plus", "heures_moins", "motif"],
    },
    {
        "nom": "valider_releve",
        "description": "PAIE — marque un relevé reçu comme VALIDÉ par la pharmacie (ou "
                       "annule sa validation) avant l'envoi au comptable. employe = "
                       "« tous » valide d'un coup tous les relevés reçus non validés du mois.",
        "params": {
            "employe": ("string", "Étiquette « Employé X », ou « tous »"),
            "valide": ("boolean", "true = valider (défaut), false = retirer la validation"),
            "mois": ("integer", "Mois 1-12 (optionnel, défaut = mois courant)"),
            "annee": ("integer", "Année (optionnel)"),
        },
        "requis": ["employe"],
    },
    {
        "nom": "envoyer_recap_comptable",
        "description": "PAIE — envoie le dossier paie du mois à l'expert-comptable "
                       "(mail + Excel). Refusé s'il reste des relevés non validés. "
                       "Irréversible ; toujours soumis à validation de l'utilisateur. "
                       "Appelle d'abord apercu_recap_comptable et montre-le.",
        "params": {
            "mois": ("integer", "Mois 1-12 (optionnel, défaut = mois courant)"),
            "annee": ("integer", "Année (optionnel)"),
        },
        "requis": [],
    },
    {
        "nom": "annuler_derniere_action",
        "description": "Annule la DERNIÈRE action que tu as exécutée (planning, "
                       "absence, relevé, fiche…) en rétablissant l'état d'avant, comme un "
                       "Ctrl+Z. Les e-mails déjà partis ne peuvent pas être rappelés. "
                       "À utiliser quand l'utilisateur dit « annule », « reviens en "
                       "arrière », « c'était une erreur ».",
        "params": {},
        "requis": [],
    },
]

# --- Outils DOSSIER SALARIÉ (phase 2) ---
OUTILS_SPECS += [
    {
        "nom": "dossier_salarie",
        "description": "Dossier complet d'un salarié : documents déposés (id, type, "
                       "à valider, expiration), documents requis manquants, suggestions "
                       "extraites des documents en attente (id), checklists d'arrivée / "
                       "de départ (cochées / restantes), statut (actif, inactif, archivé) "
                       "et alertes. À appeler avant toute action sur le dossier.",
        "params": {"employe": ("string", "Étiquette « Employé X »")},
        "requis": ["employe"],
    },
    {
        "nom": "appliquer_suggestion",
        "description": "Applique une suggestion extraite d'un document (écrit le champ "
                       "dans la fiche). suggestion = id donné par dossier_salarie, ou "
                       "« toutes ».",
        "params": {"employe": ("string", "Étiquette « Employé X »"),
                   "suggestion": ("string", "Id de la suggestion, ou « toutes »")},
        "requis": ["employe", "suggestion"],
    },
    {
        "nom": "ignorer_suggestion",
        "description": "Écarte une suggestion extraite d'un document sans rien écrire. "
                       "suggestion = id, ou « toutes ».",
        "params": {"employe": ("string", "Étiquette « Employé X »"),
                   "suggestion": ("string", "Id de la suggestion, ou « toutes »")},
        "requis": ["employe", "suggestion"],
    },
    {
        "nom": "analyser_documents",
        "description": "Relit les documents exploitables du dossier (contrat, avenant, "
                       "promesse, RIB) et génère des suggestions de pré-remplissage à "
                       "valider. N'écrit rien dans la fiche.",
        "params": {"employe": ("string", "Étiquette « Employé X »")},
        "requis": ["employe"],
    },
    {
        "nom": "cocher_checklist",
        "description": "Coche (ou décoche) une tâche de la checklist d'ARRIVÉE ou de "
                       "DÉPART d'un salarié (ex. « badge remis », « RIB reçu », « blouse "
                       "rendue »). tache = libellé approximatif accepté.",
        "params": {"employe": ("string", "Étiquette « Employé X »"),
                   "liste": ("string", "arrivee | depart"),
                   "tache": ("string", "Libellé de la tâche (approximatif accepté)"),
                   "coche": ("boolean", "true = cocher (défaut), false = décocher")},
        "requis": ["employe", "liste", "tache"],
    },
    {
        "nom": "changer_statut",
        "description": "Change le statut d'un salarié : « actif » (relevés + planning), "
                       "« inactif » (en poste mais exclu des relevés et du planning : "
                       "longue absence, pas encore arrivé), « archive » (a quitté "
                       "l'entreprise, dossier conservé).",
        "params": {"employe": ("string", "Étiquette « Employé X »"),
                   "statut": ("string", "actif | inactif | archive")},
        "requis": ["employe", "statut"],
    },
    {
        "nom": "valider_document",
        "description": "Marque un document auto-classé comme vérifié (retire l'étiquette "
                       "« à valider »). document = id donné par dossier_salarie, ou "
                       "« tous ».",
        "params": {"employe": ("string", "Étiquette « Employé X »"),
                   "document": ("string", "Id du document, ou « tous »")},
        "requis": ["employe", "document"],
    },
    {
        "nom": "retyper_document",
        "description": "Corrige le type d'un document déposé (ex. classé « Autre » alors "
                       "que c'est un contrat). Types possibles : ceux listés par l'outil "
                       "en cas d'erreur.",
        "params": {"employe": ("string", "Étiquette « Employé X »"),
                   "document": ("string", "Id du document"),
                   "type": ("string", "Nouveau type (libellé approximatif accepté)")},
        "requis": ["employe", "document", "type"],
    },
    {
        "nom": "generer_attestation",
        "description": "Génère l'attestation de travail en PDF, la range dans les "
                       "documents du salarié et note l'événement au journal. Renvoie "
                       "un lien pour l'ouvrir.",
        "params": {"employe": ("string", "Étiquette « Employé X »")},
        "requis": ["employe"],
    },
    {
        "nom": "envoyer_attestation",
        "description": "Génère l'attestation de travail (PDF) et l'ENVOIE par e-mail au "
                       "salarié, en pièce jointe. Irréversible.",
        "params": {"employe": ("string", "Étiquette « Employé X »"),
                   "message": ("string", "Petit mot d'accompagnement (optionnel)")},
        "requis": ["employe"],
    },
]

# --- Outils MAILS RH & ÉQUIPE (phase 3) ---
OUTILS_SPECS += [
    {
        "nom": "mails_rh_du_jour",
        "description": "Synthèse des e-mails RH reçus (cabinet comptable, salariés, "
                       "administratif) produite par l'assistant : résumé, tâches à faire "
                       "avec priorité, choses à mettre en place, échéances, alertes. Sans "
                       "date : la plus récente disponible. Sert à répondre à « qu'a demandé "
                       "le comptable ? », « qu'y a-t-il dans les mails aujourd'hui ? ».",
        "params": {"date": ("string", "Date AAAA-MM-JJ (optionnel, défaut = dernière synthèse)")},
        "requis": [],
    },
    {
        "nom": "actualiser_mails",
        "description": "Relance la lecture de la boîte mail RH et la génération de la "
                       "synthèse du jour (quelques minutes, via le runner). À proposer "
                       "quand la dernière synthèse date d'hier ou plus.",
        "params": {},
        "requis": [],
    },
    {
        "nom": "documents_manquants_equipe",
        "description": "Pour toute l'équipe active : documents obligatoires manquants "
                       "(contrat, RIB, pièce d'identité), documents à valider, checklists "
                       "d'arrivée incomplètes. Vue d'ensemble pour la ronde.",
        "params": {},
        "requis": [],
    },
]

OUTILS_ECRITURE = {
    "ajouter_absence", "supprimer_absence", "modifier_horaires_jour",
    "retablir_horaires_jour", "traiter_demande_conges", "envoyer_demande_collaborateur",
    "ajouter_note_journal", "mettre_a_jour_profil", "envoyer_mail", "envoyer_relance",
    "corriger_releve", "valider_releve", "envoyer_recap_comptable", "annuler_derniere_action",
    "appliquer_suggestion", "ignorer_suggestion", "analyser_documents", "cocher_checklist",
    "changer_statut", "valider_document", "retyper_document", "generer_attestation",
    "envoyer_attestation", "actualiser_mails",
}
# Outils PAIE : validation par l'utilisateur OBLIGATOIRE, même en mode autonome.
OUTILS_PAIE = {"corriger_releve", "valider_releve", "envoyer_recap_comptable"}


def _schema_props(spec):
    return {nom: {"type": t, "description": desc} for nom, (t, desc) in spec["params"].items()}


def _tools_mistral():
    return [{
        "type": "function",
        "function": {
            "name": s["nom"], "description": s["description"],
            "parameters": {"type": "object", "properties": _schema_props(s),
                           "required": s["requis"]},
        },
    } for s in OUTILS_SPECS]


def _tools_claude():
    return [{
        "name": s["nom"], "description": s["description"],
        "input_schema": {"type": "object", "properties": _schema_props(s),
                         "required": s["requis"]},
    } for s in OUTILS_SPECS]


def _exec_outil(nom, args, annuaire, table, executer, outils, actions):
    """Exécute un outil en local. Outil LECTURE -> texte (re-pseudonymisé pour le
    modèle). Outil ACTION -> dict {resultat, action} : le texte va au modèle, et
    l'`action` (vrai contenu, surfacée à l'UI uniquement, JAMAIS au LLM) est
    accumulée à part."""
    outils.append(nom)
    try:
        brut = executer(nom, args or {}, annuaire)
    except Exception as e:
        brut = f"(erreur de l'outil {nom} : {type(e).__name__})"
    if isinstance(brut, dict):
        act = brut.get("action")
        if act:
            actions.append(act)
        brut = brut.get("resultat", "")
    return pseudonymiser_texte(str(brut), table)


# --- Transports par moteur ---

def _boucle_mistral(systeme, msgs, annuaire, table, executer, modele):
    cle = os.getenv("MISTRAL_API_KEY")
    if not cle:
        raise RuntimeError("MISTRAL_API_KEY manquante.")
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {cle}", "Content-Type": "application/json"}
    convo = [{"role": "system", "content": systeme}] + msgs
    outils, actions = [], []
    for _ in range(MAX_TOURS):
        rep = _post_json(url, headers, {
            "model": modele or "mistral-small-latest", "messages": convo,
            "tools": _tools_mistral(), "tool_choice": "auto",
            "temperature": 0.2, "max_tokens": 1500})
        message = rep["choices"][0]["message"]
        tcs = message.get("tool_calls")
        if not tcs:
            return message.get("content") or "", outils, actions
        convo.append(message)  # message assistant porteur des tool_calls
        for tc in tcs:
            nom = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            res = _exec_outil(nom, args, annuaire, table, executer, outils, actions)
            convo.append({"role": "tool", "tool_call_id": tc.get("id"),
                          "name": nom, "content": res})
    # Garde-fou : un dernier appel SANS outils pour forcer une réponse rédigée.
    rep = _post_json(url, headers, {"model": modele or "mistral-small-latest",
                                    "messages": convo, "temperature": 0.2, "max_tokens": 1500})
    return rep["choices"][0]["message"].get("content") or "(pas de réponse)", outils, actions


def _boucle_claude(systeme, msgs, annuaire, table, executer, modele):
    cle = os.getenv("ANTHROPIC_API_KEY")
    if not cle:
        raise RuntimeError("ANTHROPIC_API_KEY manquante.")
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": cle, "anthropic-version": "2023-06-01",
               "Content-Type": "application/json"}
    convo = [{"role": m["role"], "content": m["content"]} for m in msgs]
    outils, actions = [], []
    for _ in range(MAX_TOURS):
        rep = _post_json(url, headers, {
            "model": modele or "claude-haiku-4-5", "max_tokens": 1500,
            "system": systeme, "messages": convo, "tools": _tools_claude()})
        blocks = rep.get("content", [])
        if rep.get("stop_reason") == "tool_use":
            convo.append({"role": "assistant", "content": blocks})
            resultats = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    res = _exec_outil(b["name"], b.get("input", {}), annuaire, table, executer, outils, actions)
                    resultats.append({"type": "tool_result", "tool_use_id": b["id"], "content": res})
            convo.append({"role": "user", "content": resultats})
            continue
        texte = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return texte, outils, actions
    return "(trop d'étapes — réponse interrompue)", outils, actions


def _boucle_fake(msgs, annuaire, table, executer):
    """Hors-ligne, coût nul : routeur par mots-clés qui déclenche UN outil puis
    rédige une réponse. Sert à valider toute la chaîne (pseudonymisation, exécution
    locale, ré-identification) sans aucun appel réseau."""
    dernier = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    d = dernier.lower()
    outils, actions = [], []
    mo = re.search(r"Employé [A-Z]+", dernier)
    label = mo.group(0) if mo else None
    # Outils ACTION (mots-clés explicites, salarié ciblé requis)
    # Outils ÉCRITURE (mots-clés explicites) — servent aux tests hors-ligne
    mdate = re.search(r"\d{4}-\d{2}-\d{2}", dernier)
    diso = mdate.group(0) if mdate else None
    if "absence" in d and label and diso and "supprim" not in d:
        nom, args = "ajouter_absence", {"employe": label, "debut": diso, "fin": diso,
                                        "motif": "Arrêt maladie"}
    elif "horaire" in d and label and diso:
        nom, args = "modifier_horaires_jour", {"employe": label, "date": diso,
                                               "creneaux": "10:00-14:00",
                                               "motif": "Heures sup/récup/échanges"}
    elif "journal" in d and label and any(k in d for k in ("ajoute", "note")):
        nom, args = "ajouter_note_journal", {"employe": label, "note": "Entretien réalisé.",
                                             "type_evenement": "Entretien"}
    elif "demandes" in d and "cong" in d:
        nom, args = "demandes_conges_en_attente", {}
    elif "solde" in d and label:
        nom, args = "solde_conges", {"employe": label}
    elif "planning" in d and diso:
        nom, args = "planning_jour", {"date": diso}
    elif "envoie" in d and "relance" in d and label:
        nom, args = "envoyer_relance", {"employe": label}
    elif "relance" in d and label:
        nom, args = "preparer_relance", {"employe": label}
    elif ("attestation" in d or "certificat" in d) and label:
        nom, args = "preparer_attestation", {"employe": label}
    elif ("journal" in d or "note" in d) and label:
        nom, args = "proposer_note_journal", {"employe": label, "note": "Entretien réalisé."}
    elif any(k in d for k in ("mail", "écris", "ecris", "rédige", "redige")) and label:
        nom, args = "preparer_mail", {"employe": label, "sujet": "Information", "corps": "Bonjour,\n..."}
    # Outils LECTURE
    elif any(k in d for k in ("relev", "rendu", "manqu")):
        nom, args = "releves_manquants", {}
    elif any(k in d for k in ("visite", "échéan", "echean", "cdd", "essai", "expir")):
        nom, args = "echeances_a_venir", {}
    elif any(k in d for k in ("heure", "écart", "ecart")):
        nom, args = "releves_manquants", {}
    elif any(k in d for k in ("fiche", "profil")):
        nom, args = ("profil_salarie", {"employe": label}) if label else ("lister_employes", {})
    else:
        nom, args = "lister_employes", {}
    res = _exec_outil(nom, args, annuaire, table, executer, outils, actions)
    return f"(mode fake) Résultat de l'outil « {nom} » :\n{res}", outils, actions


def contexte_date(aujourdhui=None):
    """Bloc « date du jour » injecté dans le system : indispensable pour que le
    modèle résolve « demain », « lundi prochain », « ce mois-ci »…"""
    from datetime import date as _date
    d = aujourdhui or _date.today()
    from datetime import timedelta as _td
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    # Calendrier des 14 prochains jours : évite les erreurs « lundi prochain = mardi ».
    cal = ", ".join(f"{jours[(d + _td(days=k)).weekday()]} {(d + _td(days=k)).isoformat()}"
                    for k in range(1, 15))
    return (f"Aujourd'hui : {jours[d.weekday()]} {d.strftime('%d/%m/%Y')} (ISO {d.isoformat()}).\n"
            f"Jours suivants : {cal}.\n"
            "Correspondances OBLIGATOIRES (« X prochain » = le PREMIER X après aujourd'hui) : "
            + ", ".join(f"{jours[(d + _td(days=k)).weekday()]} prochain = {(d + _td(days=k)).isoformat()}"
                        for k in range(1, 8)) + ".\n"
            "Utilise EXACTEMENT ces dates ISO dans les outils (vérifie le jour de la semaine).")


def run_agent(messages, employes, executer, moteur="mistral", modele=None, roster_txt="",
              mode="validation", contexte=""):
    """Lance l'agent outillé. `messages` : [{role:'user'|'assistant', content}].
    `executer(nom, args, annuaire)` : callback fourni par app.py qui exécute l'outil
    en local et renvoie un texte. `roster_txt` : roster pseudonymisé (labels + poste)
    déjà construit par app.py, injecté dans le system pour cibler « le pharmacien ».
    Renvoie {"reply": <texte ré-identifié>, "outils_utilises": [...], "actions": [...]}.
    Les `actions` (boutons à confirmer) ne sont jamais passées au LLM ; leurs libellés
    et brouillons en « Employé X » sont ré-identifiés en local pour l'affichage."""
    table, inverse = construire_table(employes or [])
    annuaire = annuaire_pseudo(employes or [])
    # Pseudonymise les messages de l'utilisateur AVANT tout envoi au modèle.
    msgs = [{"role": m["role"], "content": pseudonymiser_texte(m.get("content", ""), table)}
            for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    systeme = SYSTEM_AGENT + "\n\n" + contexte_date()
    if mode == "autonome":
        systeme += ("\nMODE AUTONOME : tes outils d'écriture EXÉCUTENT immédiatement. "
                    "Agis avec prudence (vérifie avant d'écrire) puis rends compte de ce "
                    "que tu as fait, sans demander de confirmation après coup.")
    else:
        systeme += ("\nMODE VALIDATION : tes outils d'écriture ne font que PROPOSER ; "
                    "l'utilisateur confirme d'un clic sur la carte affichée. Prépare la "
                    "proposition complète, puis dis simplement qu'elle attend sa validation.")
    if contexte:
        systeme += "\n" + contexte
    if roster_txt:
        systeme += f"\n\nSalariés (anonymisés) :\n{roster_txt}"

    if moteur == "claude":
        texte, outils, actions = _boucle_claude(systeme, msgs, annuaire, table, executer, modele)
    elif moteur == "fake":
        texte, outils, actions = _boucle_fake(msgs, annuaire, table, executer)
    else:
        texte, outils, actions = _boucle_mistral(systeme, msgs, annuaire, table, executer, modele)

    # Ré-identifie en local pour l'affichage (étiquettes -> prénoms réels).
    return {"reply": reidentifier(texte, inverse), "outils_utilises": outils,
            "actions": [reidentifier(a, inverse) for a in actions]}
