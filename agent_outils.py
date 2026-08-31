"""AGENT RH conversationnel (Blueprint Flask) — le chat « façon WhatsApp » qui AGIT.

Complète agent_rh.py (boucle LLM + catalogue d'outils) avec :
  - les implémentations des outils PLANNING (lecture) et ÉCRITURE ;
  - deux MODES : « validation » (chaque écriture devient une carte à confirmer
    d'un clic) et « autonome » (l'agent exécute puis rend compte) ;
  - une CONVERSATION persistante côté serveur (agent_conversation.json) ;
  - un JOURNAL d'audit de tout ce que l'agent a fait (agent_journal.json) ;
  - la RONDE quotidienne : l'agent passe en revue relevés, demandes de congés,
    échéances et poste son compte-rendu dans le chat (déclenchée par cron
    GitHub via /agent_ronde, ou à la main depuis la page).

RGPD : comme partout dans botRh, le LLM ne voit que des étiquettes « Employé X ».
Les implémentations reçoivent l'annuaire {étiquette: employé} et travaillent en
local. Les cartes de confirmation (jamais envoyées au modèle) sont ré-identifiées
pour l'affichage ; à la confirmation, le salarié est retrouvé par prénom/étiquette.

Réseau : api.mistral.ai et api.anthropic.com sont dans la liste blanche de
PythonAnywhere gratuit (proxy HTTP), donc la boucle agent tourne sur le serveur.
Les e-mails partent via le runner GitHub (repository_dispatch « mail_agent »),
le SMTP sortant étant bloqué sur le serveur ; en local (sans GITHUB_TOKEN), envoi
SMTP direct avec les identifiants Gmail du .env.
"""
import os
import re
import json
import uuid
from datetime import date, datetime, timedelta

from flask import Blueprint, request, render_template, redirect, url_for, session, abort, current_app

from app import (_lire_json, _ecrire_json, BASE_DIR, charger_employes, charger_profils,
                 sauvegarder_profils, collaborateur_actif, CHAMPS_PROFIL, API_CLE, MOIS_FR,
                 executer_outil_agent, _roster_pseudo, _OUTILS_AGENT, declencher_workflow,
                 charger_reponses, ecrire_reponses, reponses_file, construire_resume_paie,
                 paie_envoi_file, PROFILS_FILE, DOCS_DIR, DOCS_INDEX, charger_docs_index,
                 sauvegarder_docs_index, docs_manquants, TACHES_ARRIVEE, TACHES_DEPART,
                 FAMILLES_DOCS, alertes_completes, _analyser_document, _doc_analysable,
                 _valeur_profil_pour, _purger_propositions, CIBLES_SENSIBLES, ASSISTANT_FILE,
                 DOCS_REQUIS, EXT_DOCS_OK, deviner_type_doc, humaniser_taille)
from werkzeug.utils import secure_filename
import extraction_pj
import crypto_rh
import planning_equipe as PE
from agent_rh import OUTILS_ECRITURE, OUTILS_SPECS, OUTILS_PAIE, OUTILS_DECISION, run_agent
from agent_recrutement import OUTILS_SPECS as OUTILS_SPECS_RECRUTEMENT
import recrutement as REC
from assistant_rh import annuaire_pseudo, construire_table, pseudonymiser_texte, reidentifier
from tokens import tokens_valides, reponse_de, generer_token

bp = Blueprint("agent", __name__)

CONVERSATION_FILE = os.path.join(BASE_DIR, "agent_conversation.json")
JOURNAL_FILE = os.path.join(BASE_DIR, "agent_journal.json")
OPTIONS_FILE = os.path.join(BASE_DIR, "agent_options.json")
ANNULATIONS_FILE = os.path.join(BASE_DIR, "agent_annulations.json")
MEMOIRE_FILE = os.path.join(BASE_DIR, "agent_memoire.json")
PJ_DIR = os.path.join(BASE_DIR, "agent_pieces_jointes")   # pièces déposées dans le chat
PJ_INDEX = os.path.join(PJ_DIR, "index.json")
PJ_JOURS_RETENTION = 7      # jours de conservation d'une pièce déjà rangée (permet ↩ Annuler)
PJ_JOURS_ABANDON = 30       # jours avant purge d'une pièce jamais rangée
MAX_SOUVENIRS = 40
MAX_ANNULATIONS = 20        # instantanés conservés (les plus récents)
MAX_MESSAGES = 400          # conservés dans la conversation (les plus récents)
TOURS_CONTEXTE = 24         # messages envoyés au modèle à chaque question
MAX_JOURNAL = 1000

CHAMPS_PROFIL_MODIFIABLES = {"poste", "heures_contractuelles_hebdo", "type_contrat",
                             "date_entree", "date_fin", "fin_essai", "visite_medicale",
                             "telephone", "adresse", "urgence_nom", "urgence_tel"}
TYPES_JOURNAL = {"Entretien", "Augmentation", "Avertissement", "Formation", "Congés", "Autre"}


# --- Options (mode) ------------------------------------------------------------

def charger_options():
    o = _lire_json(OPTIONS_FILE)
    return o if isinstance(o, dict) else {}


def mode_agent():
    """« validation » (défaut, sûr) ou « autonome »."""
    return "autonome" if charger_options().get("mode") == "autonome" else "validation"


def definir_mode(mode):
    o = charger_options()
    o["mode"] = "autonome" if mode == "autonome" else "validation"
    _ecrire_json(OPTIONS_FILE, o)
    return o["mode"]


# --- Conversation persistante ----------------------------------------------------

def charger_conversation():
    c = _lire_json(CONVERSATION_FILE, [])
    return c if isinstance(c, list) else []


def ajouter_message(role, content, **extra):
    """Ajoute un message {id, role, content, ts, ...} et renvoie l'objet."""
    conv = charger_conversation()
    m = {"id": uuid.uuid4().hex[:10], "role": role, "content": content,
         "ts": datetime.now().strftime("%Y-%m-%d %H:%M")}
    m.update({k: v for k, v in extra.items() if v})
    conv.append(m)
    _ecrire_json(CONVERSATION_FILE, conv[-MAX_MESSAGES:])
    return m


def marquer_action_faite(msg_id, idx, resultat, annulation_id=None):
    """Marque la carte n° idx du message comme confirmée (texte du résultat)."""
    conv = charger_conversation()
    for m in conv:
        if m.get("id") == msg_id:
            acts = m.get("actions") or []
            if 0 <= idx < len(acts):
                acts[idx]["fait"] = resultat
                acts[idx]["fait_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
                if annulation_id:
                    acts[idx]["annulation_id"] = annulation_id
            break
    _ecrire_json(CONVERSATION_FILE, conv)


def marquer_annulee(annulation_id):
    """Marque comme annulée la carte liée à cet instantané (bouton ↩ grisé)."""
    conv = charger_conversation()
    for m in conv:
        for a in m.get("actions") or []:
            if a.get("annulation_id") == annulation_id:
                a["annulee"] = True
    _ecrire_json(CONVERSATION_FILE, conv)


# --- Annulation (Ctrl+Z) : instantané des fichiers AVANT chaque écriture --------
# Les données de botRh sont de petits fichiers JSON : avant d'exécuter un outil
# d'écriture, on mémorise le contenu des fichiers qu'il peut toucher ; « annuler »
# les réécrit tels quels. Seule la DERNIÈRE action non annulée est annulable (pas
# de retour en arrière dans le désordre). Les e-mails partis ne se rappellent pas.

OUTILS_IRREVERSIBLES = {"envoyer_mail", "envoyer_relance", "envoyer_recap_comptable",
                        "envoyer_attestation", "actualiser_mails", "envoyer_mail_candidat"}
OUTILS_MAIL_PARTIEL = {"traiter_demande_conges", "envoyer_demande_collaborateur",
                       "ajouter_absence"}   # écrivent ET peuvent prévenir par mail


def _mois_annee(args):
    now = datetime.now()
    try:
        mois = int(args.get("mois") or now.month)
        annee = int(args.get("annee") or now.year)
    except (TypeError, ValueError):
        return now.month, now.year
    if not 1 <= mois <= 12 or not 2000 <= annee <= 2100:
        return now.month, now.year
    return mois, annee


def _fichiers_etat(args):
    mois, annee = _mois_annee(args or {})
    f = [PE.ABSENCES_FILE, PE.CHANGEMENTS_FILE, PE.DEMANDES_CP_FILE, PE.DEMANDES_ADMIN_FILE,
         PROFILS_FILE, DOCS_INDEX, REC.CANDIDATS_FILE, MEMOIRE_FILE, PJ_INDEX,
         REC.CANDIDATS_DOCS_INDEX,
         reponses_file(mois, annee), reponses_file()]
    return list(dict.fromkeys(f))


def _instantane(args):
    etat = {}
    for f in _fichiers_etat(args):
        try:
            with open(f, encoding="utf-8") as fp:
                etat[f] = fp.read()
        except FileNotFoundError:
            etat[f] = None
    return etat


def charger_annulations():
    a = _lire_json(ANNULATIONS_FILE, [])
    return a if isinstance(a, list) else []


def enregistrer_annulation(outil, resume, etat_avant, etat_apres=None):
    """Après une écriture réussie : conserve l'état d'AVANT (à rétablir) et l'état
    d'APRÈS (pour vérifier, au moment d'annuler, que rien n'a bougé entre-temps).
    Ne garde que les fichiers réellement modifiés par l'action. Renvoie l'id."""
    if outil in OUTILS_IRREVERSIBLES or outil == "annuler_derniere_action":
        return None
    etat_apres = etat_apres or {}
    touches = {f: c for f, c in (etat_avant or {}).items() if etat_apres.get(f) != c}
    if not touches:
        return None   # rien n'a changé sur disque : rien à annuler
    lst = charger_annulations()
    aid = uuid.uuid4().hex[:10]
    lst.append({"id": aid, "ts": datetime.now().strftime("%d/%m/%Y %H:%M"), "outil": outil,
                "resume": resume, "etat": touches,
                "apres": {f: etat_apres.get(f) for f in touches}, "annulee": False})
    _ecrire_json(ANNULATIONS_FILE, lst[-MAX_ANNULATIONS:])
    return aid


def _restaurer(etat):
    """Réécrit les fichiers tels qu'ils étaient (None = n'existait pas)."""
    for f, contenu in (etat or {}).items():
        if contenu is None:
            if os.path.exists(f):
                os.remove(f)
        else:
            with open(f, "w", encoding="utf-8") as fp:
                fp.write(contenu)


def derniere_annulable():
    return next((a for a in reversed(charger_annulations()) if not a.get("annulee")), None)


def annuler(annulation_id=None, origine="chat"):
    """Rétablit l'état d'avant la dernière action (ou celle demandée si c'est
    bien la dernière non annulée). Renvoie (texte, ok)."""
    lst = charger_annulations()
    derniere = next((a for a in reversed(lst) if not a.get("annulee")), None)
    if not derniere:
        return "Rien à annuler : aucune action récente de l'agent.", False
    if annulation_id and derniere.get("id") != annulation_id:
        return ("Seule la dernière action peut être annulée (« "
                f"{derniere.get('resume')} »). Annule-la d'abord."), False
    # Sécurité : si un fichier a été modifié depuis l'action (à la main dans botRh,
    # ou par une autre action), on refuse plutôt que d'écraser ces changements.
    apres = derniere.get("apres") or {}
    modifies = []
    for f, attendu in apres.items():
        try:
            with open(f, encoding="utf-8") as fp:
                actuel = fp.read()
        except FileNotFoundError:
            actuel = None
        if actuel != attendu:
            modifies.append(os.path.basename(f))
    if modifies:
        derniere["annulee"] = True
        derniere["annulee_le"] = "refusée : " + ", ".join(modifies)
        _ecrire_json(ANNULATIONS_FILE, lst)
        marquer_annulee(derniere["id"])
        return ("Annulation refusée : " + ", ".join(modifies) + " a été modifié depuis cette action "
                "(par toi ou par une autre action). Corrige à la main pour ne rien écraser."), False
    # Fichiers (PDF d'attestation…) créés par l'action : présents dans l'index
    # d'après, absents de celui d'avant -> supprimés du disque (pas d'orphelin nominatif).
    etat = derniere.get("etat") or {}
    if DOCS_INDEX in etat:
        try:
            av = json.loads(etat.get(DOCS_INDEX) or "{}")
            ap = json.loads((apres.get(DOCS_INDEX) or "{}"))
            ids_av = {d.get("id") for lst in av.values() for d in lst}
            for lst in ap.values():
                for d in lst:
                    if d.get("id") not in ids_av and d.get("fichier"):
                        chemin = os.path.join(DOCS_DIR, d["fichier"])
                        if os.path.exists(chemin):
                            os.remove(chemin)
        except (ValueError, OSError):
            current_app.logger.exception("Nettoyage des fichiers d'une action annulée")
    _restaurer(etat)
    derniere["annulee"] = True
    derniere["annulee_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    _ecrire_json(ANNULATIONS_FILE, lst)
    marquer_annulee(derniere["id"])
    texte = f"Annulé : {derniere.get('resume')} (état du {derniere.get('ts')} rétabli)"
    if derniere.get("outil") in OUTILS_MAIL_PARTIEL:
        texte += " — si un e-mail est parti au salarié, il ne peut pas être rappelé"
    journaliser("annuler_derniere_action", texte, mode_agent(), origine)
    return texte, True


# --- Journal d'audit -------------------------------------------------------------

def charger_journal():
    j = _lire_json(JOURNAL_FILE, [])
    return j if isinstance(j, list) else []


def journaliser(outil, resume, mode, origine):
    j = charger_journal()
    j.append({"ts": datetime.now().strftime("%d/%m/%Y %H:%M"), "outil": outil,
              "resume": resume, "mode": mode, "origine": origine})
    _ecrire_json(JOURNAL_FILE, j[-MAX_JOURNAL:])


# --- Helpers communs -------------------------------------------------------------

def _employe(val, annuaire):
    """Retrouve un salarié par étiquette (« Employé B »), prénom ou e-mail.
    L'étiquette vient du modèle ; le prénom vient d'une carte ré-identifiée."""
    v = (val or "").strip()
    if not v:
        return None
    if v in annuaire:
        return annuaire[v]
    vl = v.lower()
    for e in annuaire.values():
        if e.get("email", "").lower() == vl:
            return e
    cands = [e for e in annuaire.values() if (e.get("prenom") or "").lower() == vl]
    if len(cands) == 1:
        return cands[0]
    cands = [e for e in annuaire.values()
             if f"{e.get('prenom', '')} {e.get('nom', '')}".strip().lower() == vl]
    return cands[0] if len(cands) == 1 else None


def _label_de(emp, annuaire):
    for lab, e in annuaire.items():
        if e.get("email") == emp.get("email"):
            return lab
    return emp.get("prenom", "?")


def _date(s):
    """AAAA-MM-JJ (attendu du modèle) ou JJ/MM/AAAA, JJ-MM-AAAA, JJ.MM.AAAA."""
    v = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _fr(d):
    return d.strftime("%d/%m/%Y") if d else "?"


def _cr_txt(creneaux):
    cr = [c for c in (creneaux or []) if PE.creneau_valide(c)]
    return ", ".join(f"{c['debut']}–{c['fin']}" for c in cr) or "repos"


def _parse_creneaux(txt):
    """« 09:00-13:00, 14:00-19:00 » -> [{debut, fin}] ; ValueError si incohérent."""
    out = []
    for part in re.split(r"[,;]| et ", txt or ""):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^\s*(\d{1,2}[:h]?\d{0,2})\s*[-–à]\s*(\d{1,2}[:h]?\d{0,2})\s*$", part)
        if not m:
            raise ValueError(f"créneau illisible : « {part} »")
        deb, fin = PE._norm_hhmm(m.group(1)), PE._norm_hhmm(m.group(2))
        if not deb or not fin or PE.creneau_incoherent(deb, fin):
            raise ValueError(f"créneau invalide : « {part} » (fin après début attendu)")
        out.append({"debut": deb, "fin": fin})
    out.sort(key=lambda c: PE._minutes(c["debut"]) or 0)
    return out[:2]


def _actifs(annuaire):
    profils = charger_profils()
    return [(lab, e) for lab, e in annuaire.items()
            if collaborateur_actif(profils.get(e["email"], {}))]


# --- Outils LECTURE planning ----------------------------------------------------

def _o_planning_jour(args, annuaire):
    d = _date(args.get("date")) or PE.jour_courant()
    trames = PE.charger_trames()
    act = PE.trame_active_pour(trames, d)
    chgs, absences = PE.charger_changements(), PE.charger_absences()
    lignes, absents, repos = [], [], []
    for lab, e in _actifs(annuaire):
        em = e["email"]
        ab = PE.absence_active(absences, em, d)
        cr = PE.creneaux_effectifs_jour(act, em, d, chgs, absences) if act else []
        chg = PE.changement_de(chgs, d.isoformat(), em)
        if ab and not cr:
            absents.append(f"{lab} ({ab.get('motif', 'absence')} du {_fr(_date(ab.get('debut')))} "
                           f"au {_fr(_date(ab.get('fin')))})")
        elif cr:
            suffixe = f" [modifié : {chg.get('motif')}]" if chg else ""
            lignes.append(f"- {lab} : {_cr_txt(cr)}{suffixe}")
        elif chg:
            absents.append(f"{lab} (jour non travaillé : {chg.get('motif')})")
        else:
            repos.append(lab)
    fer = PE.ferie_de(d)
    tete = f"Planning du {PE.JOURS_NOMS[d.isoweekday()]} {_fr(d)}"
    if fer:
        tete += f" — JOUR FÉRIÉ ({fer})"
    if not act:
        return tete + " : aucune trame active à cette date."
    txt = tete + " :\n" + ("\n".join(lignes) if lignes else "- personne de planifié")
    if absents:
        txt += "\nAbsents (absence déclarée) : " + " ; ".join(absents)
    if repos:
        txt += "\nNon planifiés ce jour (simple repos, PAS une absence) : " + ", ".join(repos)
    return txt


def _o_planning_collaborateur(args, annuaire):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable (utilise une étiquette « Employé X »)."
    d = _date(args.get("date")) or PE.jour_courant()
    lundi = PE._lundi(d)
    trames, chgs, absences = PE.charger_trames(), PE.charger_changements(), PE.charger_absences()
    lignes, total = [], 0
    for k in range(7):
        j = lundi + timedelta(days=k)
        act = PE.trame_active_pour(trames, j)
        cr = PE.creneaux_effectifs_jour(act, e["email"], j, chgs, absences) if act else []
        total += sum(PE.duree_creneau(c) for c in cr)
        ab = PE.absence_active(absences, e["email"], j)
        chg = PE.changement_de(chgs, j.isoformat(), e["email"])
        note = f" [{chg.get('motif')}]" if chg else (f" [{ab.get('motif')}]" if ab and not cr else "")
        lignes.append(f"- {PE.JOURS_ABBR[j.isoweekday()]} {j.strftime('%d/%m')} : {_cr_txt(cr)}{note}")
    lab = _label_de(e, annuaire)
    return (f"Semaine du {_fr(lundi)} — {lab} ({total / 60:.1f} h effectives) :\n"
            + "\n".join(lignes))


def _o_solde_conges(args, annuaire):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable."
    p1, p2 = PE.periode_conges()
    b = PE.bilan_cp(e["email"], PE.charger_absences(), PE.charger_changements(),
                    PE.charger_conges(), p1, p2)
    lab = _label_de(e, annuaire)
    txt = (f"Congés payés de {lab} (période {_fr(p1)} → {_fr(p2)}) : droit {b['droit']} j, "
           f"report {b['report']} j, posés {b['poses']} j, restant {b['restant']} j.")
    if b.get("detail"):
        txt += "\nPlages : " + " ; ".join(t for _, t in sorted(b["detail"], key=lambda x: x[0]))
    return txt


def _o_demandes_conges(args, annuaire):
    dems = [d for d in PE.charger_demandes_cp() if d.get("statut") == "en_attente"]
    if not dems:
        return "Aucune demande de congés en attente."
    absences = PE.charger_absences()
    lignes = []
    for dm in dems:
        e = next((x for x in annuaire.values()
                  if x.get("email", "").lower() == dm.get("email", "").lower()), None)
        lab = _label_de(e, annuaire) if e else "salarié inconnu"
        d1, d2 = _date(dm.get("debut")), _date(dm.get("fin"))
        nb = PE._jours_ouvrables_cp(d1, d2, d1, d2) if d1 and d2 else "?"
        extra = ""
        if e and d1 and d2:
            chev = PE.absence_chevauchante(absences, e["email"], d1, d2)
            if chev:
                extra += " ⚠️ chevauche une absence existante"
            autres = sorted({_label_de(x, annuaire) for x in annuaire.values()
                             if x["email"] != e["email"]
                             and PE.absence_chevauchante(absences, x["email"], d1, d2)})
            if autres:
                extra += " ; déjà absents sur la période : " + ", ".join(autres)
            p1, p2 = PE.periode_conges()
            b = PE.bilan_cp(e["email"], absences, PE.charger_changements(), PE.charger_conges(), p1, p2)
            extra += f" ; solde restant {b['restant']} j"
        lignes.append(f"- id {dm.get('id')} : {lab}, du {_fr(d1)} au {_fr(d2)} ({nb} j ouvrables), "
                      f"déposée le {dm.get('demande_le', '?')}"
                      + (f", commentaire « {dm.get('commentaire')} »" if dm.get("commentaire") else "")
                      + extra)
    return f"{len(dems)} demande(s) en attente :\n" + "\n".join(lignes)


def _o_absences_en_cours(args, annuaire):
    auj = PE.jour_courant()
    lim = auj + timedelta(days=60)
    lignes = []
    for a in sorted(PE.charger_absences(), key=lambda x: x.get("debut", "")):
        d1, d2 = _date(a.get("debut")), _date(a.get("fin"))
        if not d1 or not d2 or d2 < auj or d1 > lim:
            continue
        e = next((x for x in annuaire.values() if x.get("email") == a.get("email")), None)
        lab = _label_de(e, annuaire) if e else "salarié inconnu"
        lignes.append(f"- {lab} : {a.get('motif')} du {_fr(d1)} au {_fr(d2)}"
                      + (f" ({a.get('commentaire')})" if a.get("commentaire") else ""))
    return ("Absences en cours / à venir (60 j) :\n" + "\n".join(lignes)) if lignes \
        else "Aucune absence en cours ni à venir dans les 60 jours."


OUTILS_LECTURE_PLANNING = {
    "planning_jour": _o_planning_jour,
    "planning_collaborateur": _o_planning_collaborateur,
    "solde_conges": _o_solde_conges,
    "demandes_conges_en_attente": _o_demandes_conges,
    "absences_en_cours": _o_absences_en_cours,
}


# --- Outils ÉCRITURE -----------------------------------------------------------
# Chaque fonction : (args, annuaire, executer: bool) -> (texte, ok).
# executer=False : VALIDE (salarié, dates, motif, chevauchement) et DÉCRIT sans rien
# écrire ; executer=True : écrit. Le même code sert donc au mode validation (carte)
# et au mode autonome / à la confirmation.

def _w_ajouter_absence(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    d1 = _date(args.get("debut"))
    d2 = _date(args.get("fin")) or d1
    if not d1 or not d2:
        return "Dates invalides (attendu AAAA-MM-JJ).", False
    if d2 < d1:
        d1, d2 = d2, d1
    motif = (args.get("motif") or "").strip()
    if motif not in PE.MOTIFS or motif == "Non catégorisé":
        return f"Motif invalide « {motif} ». Motifs possibles : " + ", ".join(PE.MOTIFS[1:]) + ".", False
    absences = PE.charger_absences()
    chev = PE.absence_chevauchante(absences, e["email"], d1, d2)
    if chev:
        return (f"Refusé : une absence « {chev.get('motif')} » existe déjà du "
                f"{_fr(_date(chev.get('debut')))} au {_fr(_date(chev.get('fin')))}."), False
    lab = _label_de(e, annuaire)
    resume = f"Absence « {motif} » de {lab} du {_fr(d1)} au {_fr(d2)}"
    if args.get("commentaire"):
        resume += f" ({args['commentaire'].strip()})"
    if not executer:
        return resume, True
    purges = PE._purger_ponctuels_couverts(e["email"], d1, d2)
    absences = PE.charger_absences()
    absences.append({"id": datetime.now().strftime("%Y%m%d%H%M%S%f"), "email": e["email"],
                     "debut": d1.isoformat(), "fin": d2.isoformat(), "motif": motif,
                     "commentaire": (args.get("commentaire") or "").strip()})
    PE.sauvegarder_absences(absences)
    if purges:
        resume += f" — {purges} changement(s) ponctuel(s) retiré(s) sur la plage"
    return resume, True


def _w_supprimer_absence(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    d = _date(args.get("date"))
    if not e or not d:
        return "Salarié ou date invalide.", False
    absences = PE.charger_absences()
    a = PE.absence_active(absences, e["email"], d)
    if not a:
        return f"Aucune absence de {_label_de(e, annuaire)} ne couvre le {_fr(d)}.", False
    resume = (f"Suppression de l'absence « {a.get('motif')} » de {_label_de(e, annuaire)} "
              f"du {_fr(_date(a.get('debut')))} au {_fr(_date(a.get('fin')))}")
    if executer:
        PE.sauvegarder_absences([x for x in absences if x.get("id") != a.get("id")])
    return resume, True


def _w_modifier_horaires_jour(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    d = _date(args.get("date"))
    if not e or not d:
        return "Salarié ou date invalide.", False
    motif = (args.get("motif") or "").strip()
    if motif not in PE.MOTIFS or motif == "Non catégorisé":
        return f"Motif invalide « {motif} ». Motifs possibles : " + ", ".join(PE.MOTIFS[1:]) + ".", False
    try:
        creneaux = _parse_creneaux(args.get("creneaux") or "")
    except ValueError as ex:
        return f"Refusé : {ex}.", False
    lab = _label_de(e, annuaire)
    act = PE.trame_active_pour(PE.charger_trames(), d)
    tr = PE.creneaux_trame_jour(act, e["email"], d) if act else []
    resume = (f"Horaires de {lab} le {PE.JOURS_ABBR[d.isoweekday()]} {_fr(d)} : "
              f"{_cr_txt(creneaux)} (trame : {_cr_txt(tr)}) — motif {motif}")
    if not executer:
        return resume, True
    data = PE.charger_changements()
    if creneaux and not PE.ferie_de(d) and PE.meme_que_trame(creneaux, tr):
        if d.isoformat() in data and e["email"] in data[d.isoformat()]:
            del data[d.isoformat()][e["email"]]
            if not data[d.isoformat()]:
                del data[d.isoformat()]
        resume += " (identique à la trame : changement retiré)"
    else:
        data.setdefault(d.isoformat(), {})[e["email"]] = {
            "motif": motif, "creneaux": creneaux,
            "maj": datetime.now().strftime("%d/%m/%Y %H:%M")}
    PE.sauvegarder_changements(data)
    return resume, True


def _w_retablir_horaires_jour(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    d = _date(args.get("date"))
    if not e or not d:
        return "Salarié ou date invalide.", False
    data = PE.charger_changements()
    chg = PE.changement_de(data, d.isoformat(), e["email"])
    if not chg:
        return f"Pas de changement ponctuel pour {_label_de(e, annuaire)} le {_fr(d)}.", False
    resume = (f"Retour à la trame pour {_label_de(e, annuaire)} le {_fr(d)} "
              f"(changement « {chg.get('motif')} » : {_cr_txt(chg.get('creneaux'))} retiré)")
    if executer:
        del data[d.isoformat()][e["email"]]
        if not data[d.isoformat()]:
            del data[d.isoformat()]
        PE.sauvegarder_changements(data)
    return resume, True


def _w_traiter_demande_conges(args, annuaire, executer):
    did = (args.get("id") or "").strip()
    decision = (args.get("decision") or "").strip().lower()
    if decision not in ("accepter", "refuser"):
        return "Décision invalide (accepter | refuser).", False
    demandes = PE.charger_demandes_cp()
    dm = next((x for x in demandes if x.get("id") == did), None)
    if not dm:
        return f"Demande {did} introuvable.", False
    if dm.get("statut") != "en_attente":
        return f"Demande {did} déjà traitée ({dm.get('statut')}).", False
    e = next((x for x in annuaire.values()
              if x.get("email", "").lower() == dm.get("email", "").lower()), None)
    lab = _label_de(e, annuaire) if e else dm.get("email")
    d1, d2 = _date(dm.get("debut")), _date(dm.get("fin"))
    absences = PE.charger_absences()
    if decision == "accepter" and d1 and d2 and PE.absence_chevauchante(absences, dm["email"], d1, d2):
        return f"Refusé : la demande de {lab} chevauche une absence déjà enregistrée.", False
    if decision == "accepter" and d1 and d2:
        try:
            p1, p2 = PE.periode_conges()
            bilan = PE.bilan_cp(dm["email"], absences, PE.charger_changements(), PE.charger_conges(), p1, p2)
            demandes_j = PE._jours_ouvrables_cp(d1, d2, d1, d2)
            if demandes_j > float(bilan.get("restant") or 0):
                return (f"Refusé : solde de congés insuffisant pour {lab} "
                        f"({bilan.get('restant')} j restant(s), {demandes_j} j demandé(s))."), False
        except Exception:
            current_app.logger.exception("Contrôle du solde CP (agent)")
    resume = (f"Demande de congés de {lab} du {_fr(d1)} au {_fr(d2)} : "
              + ("ACCEPTÉE" if decision == "accepter" else "REFUSÉE"))
    if decision == "refuser" and args.get("motif_refus"):
        resume += f" (motif : {args['motif_refus'].strip()[:200]})"
    if not executer:
        return resume, True
    if decision == "accepter":
        if d1 and d2:
            PE._purger_ponctuels_couverts(dm["email"], d1, d2)
        absences = PE.charger_absences()
        aid = datetime.now().strftime("%Y%m%d%H%M%S%f")
        absences.append({"id": aid, "email": dm["email"], "debut": dm.get("debut", ""),
                         "fin": dm.get("fin", ""), "motif": "Congés payés",
                         "commentaire": (dm.get("commentaire") or "Demande employé").strip()})
        PE.sauvegarder_absences(absences)
        dm["statut"], dm["absence_id"] = "acceptee", aid
    else:
        dm["statut"] = "refusee"
        dm["motif_refus"] = (args.get("motif_refus") or "").strip()[:200]
    dm["traite_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    dm["traite_par"] = "agent"
    PE.sauvegarder_demandes_cp(demandes)
    PE._notifier_demande_cp(dm["statut"], dm, e["prenom"] if e else dm.get("email", ""))
    return resume + " — salarié prévenu par mail", True


def _w_envoyer_demande_collaborateur(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    typ = (args.get("type") or "").strip()
    if not e or typ not in PE.TYPES_DEMANDE_ADMIN:
        return "Salarié introuvable ou type invalide (conges | heures_sup).", False
    lab = _label_de(e, annuaire)
    auj = PE.jour_courant()
    dm = {"id": uuid.uuid4().hex[:10], "email": e["email"], "type": typ,
          "commentaire": (args.get("commentaire") or "").strip()[:300],
          "statut": "en_attente", "lu_admin": True,
          "cree_le": datetime.now().strftime("%d/%m/%Y %H:%M"), "par": "agent"}
    d1 = _date(args.get("debut"))
    if not d1 or d1 < auj:
        return "Date de début invalide ou passée.", False
    if typ == "conges":
        d2 = _date(args.get("fin")) or d1
        if d2 < d1:
            d1, d2 = d2, d1
        dm["debut"], dm["fin"] = d1.isoformat(), d2.isoformat()
        resume = f"Proposition de congés à {lab} du {_fr(d1)} au {_fr(d2)}"
    else:
        hd, hf = PE._norm_hhmm(args.get("h_debut")), PE._norm_hhmm(args.get("h_fin"))
        if not hd or not hf or PE.creneau_incoherent(hd, hf):
            return "Heures invalides (h_debut / h_fin HH:MM, fin après début).", False
        d2 = _date(args.get("fin")) or d1
        jours = [d1 + timedelta(days=k) for k in range((d2 - d1).days + 1)] if d2 >= d1 else [d1]
        dm["jours"] = [j.isoformat() for j in jours]
        dm["debut"], dm["fin"] = dm["jours"][0], dm["jours"][-1]
        dm["h_debut"], dm["h_fin"] = hd, hf
        resume = (f"Demande d'heures supplémentaires à {lab} le(s) "
                  + ", ".join(j.strftime("%d/%m") for j in jours) + f" de {hd} à {hf}")
    if dm["commentaire"]:
        resume += f" — « {dm['commentaire']} »"
    if executer:
        dems = PE.charger_demandes_admin()
        dems.append(dm)
        PE.sauvegarder_demandes_admin(dems)
        resume += " (visible dans son espace)"
    return resume, True


def _w_ajouter_note_journal(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    note = (args.get("note") or "").strip()
    if not e or not note:
        return "Salarié introuvable ou note vide.", False
    typ = (args.get("type_evenement") or "Autre").strip()
    if typ not in TYPES_JOURNAL:
        typ = "Autre"
    resume = f"Note au journal de {_label_de(e, annuaire)} [{typ}] : « {note} »"
    if executer:
        profils = charger_profils()
        profil = profils.get(e["email"], {})
        journal = profil.get("journal", [])
        journal.append({"id": uuid.uuid4().hex[:8], "date": datetime.now().strftime("%d/%m/%Y"),
                        "type": typ, "note": note, "par": "agent"})
        profil["journal"] = journal
        profils[e["email"]] = profil
        sauvegarder_profils(profils)
    return resume, True


def _w_mettre_a_jour_profil(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    champ = (args.get("champ") or "").strip()
    valeur = (args.get("valeur") or "").strip()
    if not e:
        return "Salarié introuvable.", False
    if champ not in CHAMPS_PROFIL_MODIFIABLES:
        return "Champ non modifiable. Champs : " + ", ".join(sorted(CHAMPS_PROFIL_MODIFIABLES)) + ".", False
    libelle = dict(CHAMPS_PROFIL).get(champ, champ)
    profils = charger_profils()
    ancien = profils.get(e["email"], {}).get(champ, "")
    resume = f"Fiche de {_label_de(e, annuaire)} — {libelle} : « {ancien or '—'} » → « {valeur or '—'} »"
    if executer:
        profil = profils.get(e["email"], {})
        profil[champ] = valeur
        profils[e["email"]] = profil
        sauvegarder_profils(profils)
    return resume, True


def _envoyer_mail_reel(dest, sujet, corps, piece=None):
    """Runner GitHub si GITHUB_TOKEN (serveur), sinon SMTP direct (local).
    piece : (nom_fichier, octets) optionnel — PDF joint."""
    if os.getenv("GITHUB_TOKEN"):
        charge = {"to": dest, "subject": sujet, "body": corps}
        if piece:
            import base64
            charge["attachment_name"] = piece[0]
            charge["attachment_b64"] = base64.b64encode(piece[1]).decode("ascii")
        declencher_workflow("mail_agent", charge)
        return "envoi confié au runner GitHub (quelques minutes)"
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.application import MIMEApplication
    user, pwd = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    if not user or not pwd:
        raise RuntimeError("ni GITHUB_TOKEN ni identifiants Gmail : envoi impossible")
    if piece:
        msg = MIMEMultipart()
        msg.attach(MIMEText(corps, "plain", "utf-8"))
        pj = MIMEApplication(piece[1], Name=piece[0])
        pj["Content-Disposition"] = f'attachment; filename="{piece[0]}"'
        msg.attach(pj)
    else:
        msg = MIMEText(corps, "plain", "utf-8")
    msg["From"], msg["To"], msg["Subject"] = user, dest, sujet
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(user, pwd)
        s.sendmail(user, [dest], msg.as_string())
    return "envoyé (SMTP direct)"


def _w_envoyer_mail(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    sujet = (args.get("sujet") or "").strip()
    corps = (args.get("corps") or "").strip()
    if not e or not sujet or not corps:
        return "Salarié introuvable, sujet ou corps vide.", False
    lab = _label_de(e, annuaire)
    resume = f"E-mail à {lab} — objet « {sujet} » :\n{corps}"
    if not executer:
        return resume, True
    if current_app.config.get("TESTING"):
        return resume + "\n(TEST : non envoyé)", True
    try:
        statut = _envoyer_mail_reel(e["email"], sujet, corps)
    except Exception as ex:
        return f"Échec de l'envoi à {lab} : {type(ex).__name__}.", False
    return resume + f"\n→ {statut}", True


def _w_envoyer_relance(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    lab = _label_de(e, annuaire)
    brut = _OUTILS_AGENT["preparer_relance"]({"employe": lab}, annuaire, charger_profils())
    act = (brut or {}).get("action") or {}
    if not act.get("body"):
        return "Impossible de composer la relance.", False
    corps = act["body"].replace(f"Bonjour {lab},", f"Bonjour {e['prenom']},")
    profils = charger_profils()
    prof = profils.get(e["email"], {})
    derniere = prof.get("derniere_relance_agent", "")
    try:
        dd = datetime.strptime(derniere, "%Y-%m-%d").date() if derniere else None
    except ValueError:
        dd = None
    if dd and (date.today() - dd).days < 3:
        return f"{lab} a déjà été relancé(e) le {_fr(dd)} : pas de nouvelle relance avant 3 jours.", False
    resume = f"Relance du relevé d'heures à {lab} — objet « {act.get('subject')} »"
    if not executer:
        return resume, True
    if current_app.config.get("TESTING"):
        return resume + " (TEST : non envoyée)", True
    try:
        statut = _envoyer_mail_reel(e["email"], act.get("subject", "Rappel"), corps)
    except Exception as ex:
        return f"Échec de la relance à {lab} : {type(ex).__name__}.", False
    prof["derniere_relance_agent"] = date.today().isoformat()
    profils[e["email"]] = prof
    sauvegarder_profils(profils)
    return resume + f" → {statut}", True


# --- Outils RELEVÉS D'HEURES & PAIE ------------------------------------------------

def _statut_releve(r):
    if r is None:
        return "manquant"
    if r.get("correction"):
        st = "corrigé"
    elif r.get("saisi_par_admin"):
        st = "saisi par la pharmacie"
    else:
        st = "reçu"
    return st + (", validé" if r.get("valide") else ", à valider")


def _h(x):
    try:
        v = float(x or 0)
    except (TypeError, ValueError):
        return "0"
    return f"{v:g}"


def _lignes_releves(annuaire, cible=None):
    """Salariés à lister : actifs + (si demandé) le salarié ciblé même inactif."""
    lst = list(_actifs(annuaire))
    if cible and all(e["email"] != cible["email"] for _, e in lst):
        lst.append((_label_de(cible, annuaire), cible))
    return lst


def _o_releve_du_mois(args, annuaire):
    mois, annee = _mois_annee(args)
    cible = _employe(args.get("employe"), annuaire) if args.get("employe") else None
    if args.get("employe") and not cible:
        return "Salarié introuvable (utilise une étiquette « Employé X »)."
    reps = charger_reponses(mois, annee)
    lignes = [f"Relevés d'heures de {MOIS_FR[mois]} {annee} :"]
    nb_manq = 0
    for lab, e in _lignes_releves(annuaire, cible):
        if cible and e["email"] != cible["email"]:
            continue
        r = reponse_de(reps, e["prenom"], e["email"])
        if r is None:
            nb_manq += 1
            lignes.append(f"- {lab} : manquant")
            continue
        l = (f"- {lab} : H+ {_h(r.get('heures_plus'))} / H− {_h(r.get('heures_moins'))}"
             f" — {_statut_releve(r)}")
        if r.get("commentaire"):
            l += f" — « {str(r['commentaire'])[:120]} »"
        if r.get("correction"):
            l += f" (corrigé le {r['correction'].get('le', '?')} : {r['correction'].get('motif', '')})"
        lignes.append(l)
        if cible:
            if r.get("declare"):
                d = r["declare"]
                lignes.append(f"  Déclaré à l'origine par le salarié : H+ {_h(d.get('heures_plus'))} "
                              f"/ H− {_h(d.get('heures_moins'))}")
            if r.get("jours"):
                lignes.append("  Détail jour par jour : " + " ; ".join(
                    f"{j.get('label')} +{_h(j.get('plus'))}/−{_h(j.get('moins'))}" for j in r["jours"]))
            else:
                lignes.append("  (pas de détail jour par jour)")
    if not cible:
        lignes.append(f"{nb_manq} relevé(s) manquant(s).")
    return "\n".join(lignes)


def _mois_range(args):
    now = datetime.now()
    try:
        mf = int(args.get("mois_fin") or now.month)
        af = int(args.get("annee_fin") or now.year)
        if args.get("mois_debut"):
            md = int(args["mois_debut"])
            ad = int(args.get("annee_debut") or af)
        else:
            ad, md0 = divmod(af * 12 + (mf - 1) - 5, 12)
            md = md0 + 1
    except (TypeError, ValueError):
        mf, af = now.month, now.year
        ad, md0 = divmod(af * 12 + mf - 6, 12)
        md = md0 + 1
    out, a, m = [], ad, md
    while (a, m) <= (af, mf) and len(out) < 24:
        out.append((m, a))
        m += 1
        if m > 12:
            m, a = 1, a + 1
    return out


def _o_stats_heures(args, annuaire):
    cible = _employe(args.get("employe"), annuaire) if args.get("employe") else None
    if args.get("employe") and not cible:
        return "Salarié introuvable (utilise une étiquette « Employé X »)."
    periode = _mois_range(args)
    if not periode:
        return "Période invalide."
    tot = {}       # email -> [plus, moins, nb_mois]
    par_mois = []  # pour un seul salarié
    for m, a in periode:
        reps = charger_reponses(m, a)
        if not reps:
            continue
        for lab, e in annuaire.items():
            if cible and e["email"] != cible["email"]:
                continue
            r = reponse_de(reps, e["prenom"], e["email"])
            if r is None:
                continue
            p, mo = float(r.get("heures_plus") or 0), float(r.get("heures_moins") or 0)
            t = tot.setdefault(e["email"], [0.0, 0.0, 0])
            t[0] += p
            t[1] += mo
            t[2] += 1
            if cible:
                par_mois.append(f"- {MOIS_FR[m]} {a} : H+ {_h(p)} / H− {_h(mo)} (solde {_h(p - mo)})")
    (m1, a1), (m2, a2) = periode[0], periode[-1]
    entete = f"Heures de {MOIS_FR[m1]} {a1} à {MOIS_FR[m2]} {a2} :"
    if not tot:
        return entete + " aucun relevé sur la période."
    lignes = [entete]
    if cible:
        lignes += par_mois
        p, mo, n = tot[cible["email"]]
        lignes.append(f"Total {_label_de(cible, annuaire)} sur {n} mois : H+ {_h(p)} / H− {_h(mo)} "
                      f"(solde {_h(p - mo)})")
        return "\n".join(lignes)
    classement = sorted(tot.items(), key=lambda x: -(x[1][0] - x[1][1]))
    for email, (p, mo, n) in classement:
        lab = next((lab for lab, e in annuaire.items() if e["email"] == email), "?")
        lignes.append(f"- {lab} : H+ {_h(p)} / H− {_h(mo)} (solde {_h(p - mo)}, {n} relevé(s))")
    tp, tm = sum(v[0] for v in tot.values()), sum(v[1] for v in tot.values())
    lignes.append(f"Équipe : H+ {_h(tp)} / H− {_h(tm)} (solde {_h(tp - tm)})")
    return "\n".join(lignes)


def _blocages_envoi_comptable(mois, annee):
    """Ce qui empêche l'envoi au comptable. Renvoie (liste de textes, recus, dest)."""
    reps = charger_reponses(mois, annee)
    profils = charger_profils()
    employes = [e for e in charger_employes()
                if collaborateur_actif(profils.get(e["email"], {}))
                or reponse_de(reps, e["prenom"], e["email"])]
    recus = [r for r in (reponse_de(reps, e["prenom"], e["email"]) for e in employes) if r]
    bloc = []
    if not recus:
        bloc.append("aucun relevé reçu ce mois")
    nv = sum(1 for r in recus if not r.get("valide"))
    if nv:
        bloc.append(f"{nv} relevé(s) reçu(s) non validé(s) (valider_releve)")
    dest = (os.getenv("COMPTA_EMAILS") or "").strip()
    if not dest:
        bloc.append("aucun destinataire comptable configuré (COMPTA_EMAILS)")
    if not os.getenv("GITHUB_TOKEN"):
        bloc.append("envoi impossible depuis cet environnement (GITHUB_TOKEN absent)")
    return bloc, recus, dest


def _o_apercu_recap_comptable(args, annuaire):
    mois, annee = _mois_annee(args)
    resume = construire_resume_paie(mois, annee)
    lignes = [f"Dossier paie {MOIS_FR[mois]} {annee} (tel qu'il partirait au comptable) :"]
    for it in resume:
        lab = next((lab for lab, e in annuaire.items() if e["email"] == it.get("email")),
                   it.get("prenom", "?"))
        if it.get("statut") == "manquant":
            l = f"- {lab} : relevé MANQUANT"
        else:
            l = (f"- {lab} : H+ {_h(it.get('plus'))} / H− {_h(it.get('moins'))} (solde {_h(it.get('solde'))}) "
                 f"— {'validé' if it.get('valide') else 'À VALIDER'}")
            if it.get("statut") == "ok":
                l += (f" ; sup 25 % {_h(it.get('sup25'))} h, sup 50 % {_h(it.get('sup50'))} h, "
                      f"complémentaires {_h(it.get('complementaires'))} h, sujétion {_h(it.get('sujetion'))} h")
            elif it.get("statut") == "sans_detail":
                l += " ; pas de détail jour par jour → ventilation 25/50 impossible"
            elif it.get("statut") == "sans_contrat":
                l += " ; heures contractuelles non renseignées sur la fiche → ventilation impossible"
        if it.get("conges"):
            l += f" ; congés payés : {it['conges']}"
        lignes.append(l)
    bloc, _, dest = _blocages_envoi_comptable(mois, annee)
    fige = paie_envoi_file(mois, annee)
    if os.path.exists(fige):
        lignes.append("Déjà envoyé au comptable le "
                      + datetime.fromtimestamp(os.path.getmtime(fige)).strftime("%d/%m/%Y %H:%M") + ".")
    if bloc:
        lignes.append("Envoi BLOQUÉ : " + " ; ".join(bloc) + ".")
    else:
        lignes.append(f"Prêt à envoyer à : {dest}.")
    return "\n".join(lignes)


def _w_corriger_releve(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    try:
        hp = round(float(args.get("heures_plus") or 0), 2)
        hm = round(float(args.get("heures_moins") or 0), 2)
    except (TypeError, ValueError):
        return "Heures invalides (nombres décimaux attendus).", False
    if hp < 0 or hm < 0 or hp > 300 or hm > 300:
        return "Heures hors limites (0 à 300).", False
    motif = (args.get("motif") or "").strip()
    if not motif:
        return "Motif de correction obligatoire.", False
    mois, annee = _mois_annee(args)
    lab = _label_de(e, annuaire)
    reps = charger_reponses(mois, annee)
    r, tok = None, None
    for t in tokens_valides(e["prenom"], e["email"]):
        if reps.get(t) is not None:
            r, tok = reps[t], t
            break
    if r is None:
        resume = (f"Saisie du relevé de {lab} pour {MOIS_FR[mois]} {annee} (non rendu) : "
                  f"H+ {_h(hp)} / H− {_h(hm)} — {motif}")
    else:
        if float(r.get("heures_plus") or 0) == hp and float(r.get("heures_moins") or 0) == hm:
            return f"Le relevé de {lab} indique déjà H+ {_h(hp)} / H− {_h(hm)} : rien à corriger.", False
        resume = (f"Correction du relevé de {lab} ({MOIS_FR[mois]} {annee}) : "
                  f"H+ {_h(r.get('heures_plus'))} → {_h(hp)}, H− {_h(r.get('heures_moins'))} → {_h(hm)} — {motif}")
        if r.get("jours"):
            resume += " (détail jour par jour conservé tel quel, à revoir sur la page Historique si besoin)"
    if not executer:
        return resume, True
    now = datetime.now()
    if r is None:
        reps[generer_token(e["prenom"], e["email"])] = {
            "prenom": e["prenom"], "heures_plus": hp, "heures_moins": hm, "commentaire": motif,
            "date_signature": now.strftime("%d/%m/%Y"), "signature": "Saisie pharmacie (agent RH)",
            "date": now.strftime("%d/%m/%Y %H:%M"), "mois": mois, "annee": annee, "jours": [],
            "saisi_par_admin": True, "valide": True,
            "date_validation": now.strftime("%d/%m/%Y %H:%M")}
    else:
        r.setdefault("declare", {"heures_plus": r.get("heures_plus"),
                                 "heures_moins": r.get("heures_moins"),
                                 "jours": json.loads(json.dumps(r.get("jours") or []))})
        r["heures_plus"], r["heures_moins"] = hp, hm
        r["correction"] = {"le": now.strftime("%d/%m/%Y %H:%M"), "motif": motif + " (agent RH)"}
        r["valide"], r["date_validation"] = False, ""
        reps[tok] = r
    ecrire_reponses(reps, mois, annee)
    return resume, True


def _w_valider_releve(args, annuaire, executer):
    mois, annee = _mois_annee(args)
    v = args.get("valide")
    valide = True if v is None else (str(v).lower() in ("true", "1", "oui", "yes"))
    reps = charger_reponses(mois, annee)
    cibles = []   # (label, token, r)
    if (args.get("employe") or "").strip().lower() in ("tous", "toutes", "tout", "all", "*"):
        for lab, e in _actifs(annuaire):
            for t in tokens_valides(e["prenom"], e["email"]):
                r = reps.get(t)
                if r is not None and bool(r.get("valide")) != valide:
                    cibles.append((lab, t, r))
                    break
        if not cibles:
            return (f"Tous les relevés reçus de {MOIS_FR[mois]} {annee} sont déjà "
                    f"{'validés' if valide else 'non validés'}."), False
    else:
        e = _employe(args.get("employe"), annuaire)
        if not e:
            return "Salarié introuvable (ou « tous »).", False
        lab = _label_de(e, annuaire)
        for t in tokens_valides(e["prenom"], e["email"]):
            if reps.get(t) is not None:
                cibles.append((lab, t, reps[t]))
                break
        if not cibles:
            return f"{lab} n'a pas rendu son relevé de {MOIS_FR[mois]} {annee} : rien à valider.", False
        if bool(cibles[0][2].get("valide")) == valide:
            return f"Le relevé de {lab} est déjà {'validé' if valide else 'non validé'}.", False
    action = "Validation" if valide else "Retrait de la validation"
    resume = (f"{action} du relevé de {MOIS_FR[mois]} {annee} pour : "
              + ", ".join(lab for lab, _, _ in cibles))
    if executer:
        for _, t, r in cibles:
            r["valide"] = valide
            r["date_validation"] = datetime.now().strftime("%d/%m/%Y %H:%M") if valide else ""
            reps[t] = r
        ecrire_reponses(reps, mois, annee)
    return resume, True


def _w_envoyer_recap_comptable(args, annuaire, executer):
    mois, annee = _mois_annee(args)
    bloc, recus, dest = _blocages_envoi_comptable(mois, annee)
    if bloc:
        return "Envoi refusé : " + " ; ".join(bloc) + ".", False
    resume = (f"Envoi du dossier paie de {MOIS_FR[mois]} {annee} ({len(recus)} relevé(s)) "
              f"à {dest} — irréversible")
    if os.path.exists(paie_envoi_file(mois, annee)):
        resume += " (déjà envoyé une fois : ce sera un renvoi)"
    if not executer:
        return resume, True
    _ecrire_json(paie_envoi_file(mois, annee), construire_resume_paie(mois, annee))
    try:
        declencher_workflow("envoi_comptable", {"mois": mois, "annee": annee, "destinataires": dest})
    except Exception as ex:
        try:
            os.remove(paie_envoi_file(mois, annee))
        except OSError:
            pass
        return f"Envoi NON parti ({type(ex).__name__}) : rien n'a été transmis au comptable.", False
    return resume, True


def _w_annuler_derniere_action(args, annuaire, executer):
    d = derniere_annulable()
    if not d:
        return "Rien à annuler : aucune action récente de l'agent.", False
    resume = f"Annulation de la dernière action : {d.get('resume')} ({d.get('ts')})"
    if not executer:
        return resume, True
    return annuler(d["id"])


OUTILS_LECTURE_PAIE = {
    "releve_du_mois": _o_releve_du_mois,
    "stats_heures": _o_stats_heures,
    "apercu_recap_comptable": _o_apercu_recap_comptable,
}

# --- Outils DOSSIER SALARIÉ ---------------------------------------------------------

def _simplifie(t):
    """Minuscules sans accents ni ponctuation, pour les correspondances approximatives."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _correspondance(voulu, candidats):
    """Retrouve un libellé parmi `candidats` à partir d'un texte approximatif :
    égalité, inclusion, puis recouvrement de mots (≥ 2 mots communs ou 1 mot rare)."""
    v = _simplifie(voulu)
    if not v:
        return None
    simp = {c: _simplifie(c) for c in candidats}
    for c, sc in simp.items():
        if sc == v:
            return c
    inclus = [c for c, sc in simp.items() if v in sc or sc in v]
    if len(inclus) == 1:
        return inclus[0]
    mots_v = set(v.split())
    scores = []
    for c, sc in simp.items():
        commun = mots_v & set(sc.split())
        commun -= {"de", "du", "des", "la", "le", "les", "et", "a", "d"}
        if commun:
            scores.append((len(commun), c))
    scores.sort(reverse=True)
    if scores and (len(scores) == 1 or scores[0][0] > scores[1][0]):
        return scores[0][1]
    return None


TOUS_TYPES_DOCS = [t for lst in FAMILLES_DOCS.values() for t in lst]


def _docs_de(email):
    return charger_docs_index().get(email, [])


def _doc_par_id(email, doc_id):
    doc_id = (doc_id or "").strip()
    return next((d for d in _docs_de(email) if d.get("id") == doc_id), None)


def _statut_texte(prof):
    if prof.get("statut") == "archive":
        return "archivé (a quitté l'entreprise)"
    return "actif" if collaborateur_actif(prof) else "inactif (exclu des relevés et du planning)"


def _valeur_suggestion_affichable(p):
    """Valeur d'une suggestion SANS donnée sensible (IBAN masqué)."""
    if p.get("chiffre") or p.get("cible") in CIBLES_SENSIBLES:
        clair = crypto_rh.dechiffrer(p.get("valeur", "")) if p.get("chiffre") else p.get("valeur", "")
        return f"IBAN se terminant par {str(clair)[-4:]}" if clair else "IBAN"
    return p.get("apercu") or p.get("valeur", "")


def _o_dossier_salarie(args, annuaire):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable (utilise une étiquette « Employé X »)."
    lab = _label_de(e, annuaire)
    prof = charger_profils().get(e["email"], {})
    lignes = [f"Dossier de {lab} — statut : {_statut_texte(prof)}."]
    docs = _docs_de(e["email"])
    if docs:
        lignes.append("Documents :")
        for d in docs:
            l = f"- [{d.get('id')}] {d.get('type', '?')} — {d.get('libelle') or d.get('nom_original', '')} (ajouté le {d.get('date_ajout', '?')})"
            if d.get("a_valider"):
                l += " — À VALIDER (classement automatique)"
            if d.get("expiration"):
                l += f" — expire le {d['expiration']}"
            lignes.append(l)
    else:
        lignes.append("Aucun document déposé.")
    manq = docs_manquants(e["email"])
    lignes.append("Documents requis manquants : " + (", ".join(manq) if manq else "aucun ✅"))
    props = prof.get("propositions") or []
    if props:
        lignes.append("Suggestions extraites des documents (à confirmer) :")
        for p in props:
            actuel = _valeur_profil_pour(prof, p.get("cible", ""))
            l = f"- [{p.get('id')}] {p.get('libelle') or p.get('cible')} : {_valeur_suggestion_affichable(p)}"
            if actuel and p.get("cible") != "iban":
                l += f" (actuellement : {actuel})"
            lignes.append(l)
    else:
        lignes.append("Aucune suggestion en attente.")
    for typ, taches, cle in (("arrivée", TACHES_ARRIVEE, "check_arrivee"),
                             ("départ", TACHES_DEPART, "check_depart")):
        faites = [t for t in prof.get(cle, []) if t in taches]
        restantes = [t for t in taches if t not in faites]
        lignes.append(f"Checklist {typ} : {len(faites)}/{len(taches)} — restantes : "
                      + (", ".join(restantes) if restantes else "aucune ✅"))
    al = alertes_completes(e["email"], prof)
    if al:
        lignes.append("Alertes : " + " ; ".join(a.get("texte", "") for a in al))
    return "\n".join(lignes)


def _suggestions_ciblees(prof, ref):
    props = prof.get("propositions") or []
    ref = (ref or "").strip()
    if ref.lower() in ("toutes", "tous", "tout", "all", "*"):
        return props
    return [p for p in props if p.get("id") == ref]


def _appliquer_une_suggestion(prof, p):
    cible, valeur = p.get("cible", ""), p.get("valeur", "")
    if cible.startswith("profil:"):
        champ = cible.split(":", 1)[1]
        if champ in {c for c, _ in CHAMPS_PROFIL}:
            prof[champ] = valeur
    elif cible == "iban":
        prof["iban"] = valeur
        ca = prof.setdefault("check_arrivee", [])
        if "RIB reçu" in TACHES_ARRIVEE and "RIB reçu" not in ca:
            ca.append("RIB reçu")
    prof["propositions"] = [x for x in prof.get("propositions", []) if x.get("id") != p.get("id")]


def _w_appliquer_suggestion(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    profils = charger_profils()
    prof = profils.get(e["email"], {})
    cibles = _suggestions_ciblees(prof, args.get("suggestion"))
    if not cibles:
        return "Suggestion introuvable (utilise l'id donné par dossier_salarie, ou « toutes »).", False
    lab = _label_de(e, annuaire)
    resume = f"Application dans la fiche de {lab} : " + " ; ".join(
        f"{p.get('libelle') or p.get('cible')} = {_valeur_suggestion_affichable(p)}" for p in cibles)
    if executer:
        for p in cibles:
            _appliquer_une_suggestion(prof, p)
        _purger_propositions(prof)
        profils[e["email"]] = prof
        sauvegarder_profils(profils)
    return resume, True


def _w_ignorer_suggestion(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    profils = charger_profils()
    prof = profils.get(e["email"], {})
    cibles = _suggestions_ciblees(prof, args.get("suggestion"))
    if not cibles:
        return "Suggestion introuvable (utilise l'id donné par dossier_salarie, ou « toutes »).", False
    ids = {p.get("id") for p in cibles}
    resume = f"Suggestions écartées pour {_label_de(e, annuaire)} : " + ", ".join(
        p.get("libelle") or p.get("cible") for p in cibles)
    if executer:
        prof["propositions"] = [x for x in prof.get("propositions", []) if x.get("id") not in ids]
        profils[e["email"]] = prof
        sauvegarder_profils(profils)
    return resume, True


def _w_analyser_documents(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    docs = [d for d in _docs_de(e["email"]) if _doc_analysable(d.get("type", ""))]
    if not docs:
        return "Aucun document exploitable (contrat, avenant, promesse, RIB) dans ce dossier.", False
    lab = _label_de(e, annuaire)
    resume = f"Analyse de {len(docs)} document(s) de {lab} : " + ", ".join(d.get("type", "?") for d in docs)
    if not executer:
        return resume, True
    nb = 0
    for d in docs:
        try:
            nb += _analyser_document(e["email"], d)
        except Exception:
            current_app.logger.exception("Analyse document (agent)")
    return resume + f" — {nb} suggestion(s) ajoutée(s), à valider", True


def _w_cocher_checklist(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    liste = _simplifie(args.get("liste"))
    if liste.startswith("arriv") or liste in ("onboarding", "entree"):
        typ, taches, cle = "arrivée", TACHES_ARRIVEE, "check_arrivee"
    elif liste.startswith("depart") or liste in ("offboarding", "sortie"):
        typ, taches, cle = "départ", TACHES_DEPART, "check_depart"
    else:
        return "Liste invalide : arrivee | depart.", False
    tache = _correspondance(args.get("tache"), taches)
    if not tache:
        return f"Tâche introuvable dans la checklist {typ}. Tâches : " + ", ".join(taches) + ".", False
    c = args.get("coche")
    coche = True if c is None else str(c).lower() in ("true", "1", "oui", "yes")
    profils = charger_profils()
    prof = profils.get(e["email"], {})
    faites = [t for t in prof.get(cle, []) if t in taches]
    if coche and tache in faites:
        return f"« {tache} » est déjà coché pour {_label_de(e, annuaire)}.", False
    if not coche and tache not in faites:
        return f"« {tache} » n'est pas coché pour {_label_de(e, annuaire)}.", False
    resume = f"Checklist {typ} de {_label_de(e, annuaire)} : « {tache} » {'coché' if coche else 'décoché'}"
    if executer:
        faites = [t for t in taches if (t in faites and t != tache) or (coche and t == tache)]
        prof[cle] = faites
        profils[e["email"]] = prof
        sauvegarder_profils(profils)
        restantes = [t for t in taches if t not in faites]
        resume += f" — restantes : {', '.join(restantes) if restantes else 'aucune ✅'}"
    return resume, True


def _w_changer_statut(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    st = _simplifie(args.get("statut"))
    if st.startswith("archiv"):
        st = "archive"
    elif st.startswith("inactif") or st in ("pause", "suspendu"):
        st = "inactif"
    elif st.startswith("actif") or st in ("reactiver", "reactive", "en poste"):
        st = "actif"
    else:
        return "Statut invalide : actif | inactif | archive.", False
    profils = charger_profils()
    prof = profils.get(e["email"], {})
    actuel = ("archive" if prof.get("statut") == "archive"
              else "actif" if collaborateur_actif(prof) else "inactif")
    lab = _label_de(e, annuaire)
    if actuel == st:
        return f"{lab} est déjà {st}.", False
    libelles = {"actif": "ACTIF (relevés + planning)", "inactif": "INACTIF (exclu des relevés et du planning, dossier conservé)",
                "archive": "ARCHIVÉ (a quitté l'entreprise, dossier conservé)"}
    resume = f"Statut de {lab} : {actuel} → {libelles[st]}"
    if executer:
        prof["statut"] = "archive" if st == "archive" else "actif"
        if st != "archive":
            prof["releves_actif"] = (st == "actif")
        prof.setdefault("journal", []).append({
            "id": uuid.uuid4().hex[:8], "date": datetime.now().strftime("%d/%m/%Y"), "type": "Autre",
            "note": f"Statut passé à {libelles[st]} (agent RH)."})
        profils[e["email"]] = prof
        sauvegarder_profils(profils)
    return resume, True


def _w_valider_document(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    ref = (args.get("document") or "").strip()
    docs = _docs_de(e["email"])
    if ref.lower() in ("tous", "toutes", "tout", "all", "*"):
        cibles = [d for d in docs if d.get("a_valider")]
    else:
        cibles = [d for d in docs if d.get("id") == ref and d.get("a_valider")]
    if not cibles:
        return "Aucun document « à valider » correspondant.", False
    resume = f"Documents de {_label_de(e, annuaire)} marqués vérifiés : " + ", ".join(
        f"{d.get('type')} ({d.get('libelle') or d.get('nom_original', '')})" for d in cibles)
    if executer:
        idx = charger_docs_index()
        ids = {d.get("id") for d in cibles}
        for d in idx.get(e["email"], []):
            if d.get("id") in ids:
                d["a_valider"] = False
        sauvegarder_docs_index(idx)
    return resume, True


def _w_retyper_document(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    d = _doc_par_id(e["email"], args.get("document"))
    if not d:
        return "Document introuvable (utilise l'id donné par dossier_salarie).", False
    typ = _correspondance(args.get("type"), TOUS_TYPES_DOCS)
    if not typ:
        return "Type inconnu. Types possibles : " + ", ".join(TOUS_TYPES_DOCS) + ".", False
    if typ == d.get("type"):
        return f"Ce document est déjà classé « {typ} ».", False
    resume = (f"Document {d.get('libelle') or d.get('nom_original', '')} de {_label_de(e, annuaire)} : "
              f"« {d.get('type')} » → « {typ} »")
    if executer:
        idx = charger_docs_index()
        for x in idx.get(e["email"], []):
            if x.get("id") == d.get("id"):
                x["type"] = typ
                x["a_valider"] = False
        sauvegarder_docs_index(idx)
    return resume, True


def _pdf_attestation(e, prof):
    import attestation_pdf
    if not attestation_pdf.reportlab_disponible():
        raise RuntimeError("reportlab indisponible : PDF impossible")
    return attestation_pdf.generer_pdf_attestation(e, prof)


def _ranger_attestation(e, prof, octets):
    """Enregistre le PDF dans les documents du salarié + note au journal. Renvoie l'id."""
    import hashlib
    from app import humaniser_taille
    os.makedirs(DOCS_DIR, exist_ok=True)
    doc_id = uuid.uuid4().hex[:12]
    nom = f"Attestation_travail_{e.get('prenom', '')}_{datetime.now():%Y-%m-%d}.pdf".replace(" ", "_")
    fichier = f"{doc_id}_{nom}"
    with open(os.path.join(DOCS_DIR, fichier), "wb") as fp:
        fp.write(octets)
    idx = charger_docs_index()
    idx.setdefault(e["email"], []).append({
        "id": doc_id, "fichier": fichier, "nom_original": nom, "type": "Attestation employeur",
        "libelle": f"Attestation de travail du {datetime.now():%d/%m/%Y}", "expiration": "",
        "taille": humaniser_taille(len(octets)), "date_ajout": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "source": "agent", "a_valider": False, "sha": hashlib.sha256(octets).hexdigest()})
    sauvegarder_docs_index(idx)
    profils = charger_profils()
    p = profils.get(e["email"], prof)
    p.setdefault("journal", []).append({
        "id": uuid.uuid4().hex[:8], "date": datetime.now().strftime("%d/%m/%Y"), "type": "Autre",
        "note": "Attestation de travail générée (agent RH)."})
    profils[e["email"]] = p
    sauvegarder_profils(profils)
    return doc_id


def _w_generer_attestation(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e:
        return "Salarié introuvable.", False
    lab = _label_de(e, annuaire)
    prof = charger_profils().get(e["email"], {})
    resume = f"Attestation de travail de {lab} en PDF (poste : {prof.get('poste') or 'non renseigné'}, entrée : {prof.get('date_entree') or 'non renseignée'})"
    if not executer:
        return resume, True
    try:
        octets = _pdf_attestation(e, prof)
    except Exception as ex:
        return f"PDF impossible : {ex}", False
    doc_id = _ranger_attestation(e, prof, octets)
    return resume + f" — rangée dans ses documents : /admin/document/{doc_id}", True


def _w_envoyer_attestation(args, annuaire, executer):
    e = _employe(args.get("employe"), annuaire)
    if not e or not e.get("email"):
        return "Salarié introuvable.", False
    lab = _label_de(e, annuaire)
    prof = charger_profils().get(e["email"], {})
    mot = (args.get("message") or "").strip()
    corps = (f"Bonjour {e.get('prenom', '')},\n\n"
             + (mot + "\n\n" if mot else "")
             + "Vous trouverez ci-joint votre attestation de travail.\n\n"
             "Bien cordialement,\nPharmacie Apothical Nanterre Université")
    resume = f"Envoi de l'attestation de travail (PDF) à {lab} par e-mail :\n{corps}"
    if not executer:
        return resume, True
    if current_app.config.get("TESTING"):
        return resume + "\n(TEST : non envoyé)", True
    try:
        octets = _pdf_attestation(e, prof)
        doc_id = _ranger_attestation(e, prof, octets)
        nom = f"Attestation_travail_{e.get('prenom', '')}.pdf".replace(" ", "_")
        etat = _envoyer_mail_reel(e["email"], "Votre attestation de travail", corps, (nom, octets))
    except Exception as ex:
        return f"Échec : {ex}", False
    return resume + f"\n→ {etat} ; copie rangée dans ses documents (/admin/document/{doc_id})", True


OUTILS_LECTURE_DOSSIER = {"dossier_salarie": _o_dossier_salarie}

# --- Outils MAILS RH & ÉQUIPE ---------------------------------------------------------

def _o_mails_rh_du_jour(args, annuaire):
    resumes = _lire_json(ASSISTANT_FILE)
    if not isinstance(resumes, dict) or not resumes:
        return ("Aucune synthèse de mails disponible. Lance actualiser_mails (ou le bouton "
                "« Actualiser » de la page Assistant).")
    dates = sorted(resumes)
    d = (args.get("date") or "").strip()
    if d and d not in resumes:
        return f"Pas de synthèse pour le {d}. Dates disponibles : {', '.join(dates[-10:])}."
    d = d or dates[-1]
    r = resumes[d]
    meta = r.get("_meta") or {}
    lignes = [f"Synthèse des mails RH du {d} (générée le {r.get('genere_le', '?')}, "
              f"{meta.get('nb_mails', '?')} mail(s))"]
    try:
        age = (date.today() - datetime.strptime(d, "%Y-%m-%d").date()).days
        if age >= 1:
            lignes[0] += f" — ATTENTION : elle date d'il y a {age} jour(s), propose actualiser_mails"
    except ValueError:
        pass
    if r.get("resume_texte"):
        lignes.append(r["resume_texte"])
    prio = {"haute": "!!!", "moyenne": "!!", "basse": "!"}
    taches = r.get("taches_a_faire") or []
    if taches:
        lignes.append("Tâches à faire :")
        for t in taches:
            l = f"- [{prio.get((t.get('priorite') or '').lower(), '')}] {t.get('titre', '')}"
            if t.get("detail"):
                l += f" — {t['detail']}"
            if t.get("source_mail"):
                l += f" (source : {t['source_mail']})"
            lignes.append(l)
    amp = r.get("a_mettre_en_place") or []
    if amp:
        lignes.append("À mettre en place :")
        lignes += [f"- {x.get('titre', '')}" + (f" — {x['detail']}" if x.get("detail") else "") for x in amp]
    ech = r.get("echeances") or []
    if ech:
        lignes.append("Échéances :")
        lignes += [f"- {x.get('libelle', '')} : {x.get('date_limite', '?')}"
                   + (f" ({x['source']})" if x.get("source") else "") for x in ech]
    al = r.get("alertes") or []
    if al:
        lignes.append("Alertes : " + " ; ".join(str(a) for a in al))
    if not (taches or amp or ech or al):
        lignes.append("Rien à faire d'après les mails.")
    return "\n".join(lignes)


def _o_documents_manquants_equipe(args, annuaire):
    lignes = []
    for lab, e in _actifs(annuaire):
        prof = charger_profils().get(e["email"], {})
        pb = []
        manq = docs_manquants(e["email"])
        if manq:
            pb.append("manquant(s) : " + ", ".join(manq))
        av = [d for d in _docs_de(e["email"]) if d.get("a_valider")]
        if av:
            pb.append(f"{len(av)} document(s) à valider")
        restantes = [t for t in TACHES_ARRIVEE if t not in prof.get("check_arrivee", [])]
        if restantes and len(restantes) < len(TACHES_ARRIVEE):
            pb.append(f"checklist d'arrivée incomplète ({len(restantes)} restante(s))")
        if pb:
            lignes.append(f"- {lab} : " + " ; ".join(pb))
    if not lignes:
        return "Dossiers de l'équipe complets : rien à signaler. ✅"
    return (f"Dossiers incomplets ({len(lignes)} salarié(s)) — documents requis : "
            + ", ".join(DOCS_REQUIS) + " :\n" + "\n".join(lignes))


def _w_actualiser_mails(args, annuaire, executer):
    resume = "Relecture de la boîte mail RH et régénération de la synthèse (runner GitHub, quelques minutes)"
    if not executer:
        return resume, True
    if current_app.config.get("TESTING"):
        return resume + " (TEST : non lancé)", True
    if not os.getenv("GITHUB_TOKEN"):
        return "Impossible depuis cet environnement (GITHUB_TOKEN absent) : utilise le bouton « Actualiser » de la page Assistant sur le serveur.", False
    try:
        declencher_workflow("assistant_refresh", {"origine": "agent"})
    except Exception as ex:
        return f"Échec du lancement : {type(ex).__name__}", False
    return resume + " — lancée, la synthèse apparaîtra dans mails_rh_du_jour", True


OUTILS_LECTURE_MAILS = {
    "mails_rh_du_jour": _o_mails_rh_du_jour,
    "documents_manquants_equipe": _o_documents_manquants_equipe,
}

# --- Outils RECRUTEMENT ---------------------------------------------------------------
# Lecture + brouillons : délégués à recrutement.executer_outil_recrutement (vraies
# données candidats, pas de pseudonymisation — cloison : jamais de dossier salarié).

OUTILS_LECTURE_RECRUTEMENT = {sp["nom"] for sp in OUTILS_SPECS_RECRUTEMENT}


def _candidat(ref):
    tous = REC.charger_candidats()
    if ref in tous:                       # identifiant stable (cartes à confirmer)
        return ref, tous[ref], None
    cid, c = REC._resoudre_candidat(ref)
    if cid is None:
        return None, None, REC._msg_resolution(c)
    return cid, c, None


def _w_changer_statut_candidat(args, annuaire, executer):
    cid, c, err = _candidat(args.get("candidat"))
    if err:
        return err, False
    statut = _correspondance(args.get("statut"), REC.STATUTS_RECRUTEMENT)
    if not statut:
        return "Statut inconnu. Statuts : " + ", ".join(REC.STATUTS_RECRUTEMENT) + ".", False
    nom = f"{c.get('prenom', '')} {c.get('nom', '')}".strip()
    ancien = c.get("statut") or "?"
    if ancien == statut:
        return f"{nom} est déjà « {statut} ».", False
    resume = f"Candidat {nom} : statut « {ancien} » → « {statut} »"
    if executer:
        cands = REC.charger_candidats()
        cands[cid]["statut"] = statut
        if statut in ("Entretien", "Retenu", "Refusé", "Embauché"):
            cands[cid].setdefault("journal", []).append({
                "id": uuid.uuid4().hex[:8], "date": datetime.now().strftime("%d/%m/%Y"),
                "type": "Décision RH",
                "note": f"⚖️ Décision humaine (confirmée dans l'agent RH) : statut « {statut} » "
                        "(analyse IA consultative, sans valeur décisionnelle)."})
        REC.sauvegarder_candidats(cands)
    return resume, True


def _w_envoyer_mail_candidat(args, annuaire, executer):
    cid, c, err = _candidat(args.get("candidat"))
    if err:
        return err, False
    sujet, corps = (args.get("sujet") or "").strip(), (args.get("corps") or "").strip()
    if not sujet or not corps:
        return "Sujet ou corps vide.", False
    if not c.get("email") or "@" not in c["email"]:
        return "Ce candidat n'a pas d'adresse e-mail enregistrée.", False
    nom = f"{c.get('prenom', '')} {c.get('nom', '')}".strip()
    resume = f"E-mail à {nom} ({c['email']}) — objet « {sujet} » :\n{corps}"
    if not executer:
        return resume, True
    if current_app.config.get("TESTING"):
        return resume + "\n(TEST : non envoyé)", True
    try:
        etat = _envoyer_mail_reel(c["email"], sujet, corps)
    except Exception as ex:
        return f"Échec de l'envoi : {ex}", False
    cands = REC.charger_candidats()
    if cid in cands:
        cands[cid].setdefault("journal", []).append({
            "id": uuid.uuid4().hex[:8], "date": datetime.now().strftime("%d/%m/%Y"),
            "type": "E-mail", "note": f"E-mail envoyé par l'agent RH — « {sujet} »"})
        REC.sauvegarder_candidats(cands)
    return resume + f"\n→ {etat}", True

# --- Mémoire persistante --------------------------------------------------------------
# Faits durables donnés par l'utilisateur, stockés EN CLAIR (prénoms réels) sur le
# serveur ; pseudonymisés au moment d'être injectés dans le contexte du modèle.

def charger_memoire():
    m = _lire_json(MEMOIRE_FILE, [])
    return m if isinstance(m, list) else []


def sauvegarder_memoire(lst):
    _ecrire_json(MEMOIRE_FILE, lst[-MAX_SOUVENIRS:])


def memoire_contexte(table):
    """Bloc « MÉMOIRE » pseudonymisé pour le system prompt ('' si vide)."""
    souvenirs = charger_memoire()
    if not souvenirs:
        return ""
    lignes = [f"- {pseudonymiser_texte(x.get('texte', ''), table)}" for x in souvenirs]
    return "MÉMOIRE (faits durables donnés par l'utilisateur) :\n" + "\n".join(lignes)


def _w_memoriser(args, annuaire, executer):
    texte = " ".join((args.get("texte") or "").split()).strip()[:300]
    if len(texte) < 4:
        return "Rien à retenir (texte vide).", False
    # Le modèle parle en « Employé X » : on ré-identifie localement avant de stocker.
    _, inverse = construire_table(charger_employes())
    clair = reidentifier(texte, inverse)
    if any(_simplifie(clair) == _simplifie(x.get("texte")) for x in charger_memoire()):
        return f"Déjà en mémoire : « {clair} ».", False
    resume = f"Retenir : « {clair} »"
    if executer:
        lst = charger_memoire()
        lst.append({"id": uuid.uuid4().hex[:8], "texte": clair,
                    "ts": datetime.now().strftime("%d/%m/%Y %H:%M")})
        sauvegarder_memoire(lst)
    return resume, True


def _w_oublier(args, annuaire, executer):
    ref = (args.get("souvenir") or "").strip()
    lst = charger_memoire()
    if not lst:
        return "La mémoire est vide.", False
    cible = next((x for x in lst if x.get("id") == ref), None)
    if not cible:
        _, inverse = construire_table(charger_employes())
        r = _simplifie(reidentifier(ref, inverse))
        cands = [x for x in lst if r and (r in _simplifie(x.get("texte")) or _simplifie(x.get("texte")).startswith(r))]
        if len(cands) == 1:
            cible = cands[0]
        elif len(cands) > 1:
            return "Plusieurs souvenirs correspondent : " + " ; ".join(f"[{x['id']}] {x['texte']}" for x in cands), False
    if not cible:
        return "Souvenir introuvable (utilise l'id donné par souvenirs).", False
    resume = f"Oublier : « {cible.get('texte')} »"
    if executer:
        sauvegarder_memoire([x for x in lst if x.get("id") != cible.get("id")])
    return resume, True


def _o_souvenirs(args, annuaire):
    lst = charger_memoire()
    if not lst:
        return "Je n'ai encore rien retenu."
    return "Ce que je retiens :\n" + "\n".join(f"- [{x.get('id')}] {x.get('texte')} ({x.get('ts', '')[:10]})" for x in lst)


OUTILS_LECTURE_MEMOIRE = {"souvenirs": _o_souvenirs}


# --- Pièces jointes déposées dans le chat --------------------------------------------
# Une pièce (fichier ou photo) arrive par /admin/agent/piece, est stockée EN ATTENTE
# dans PJ_DIR, puis l'agent demande comment la nommer et où la ranger et appelle
# ranger_piece_jointe : le fichier est COPIÉ dans le dossier cible (documents_rh ou
# candidats_docs) et l'entrée d'index créée. La pièce en attente reste quelques jours
# (↩ Annuler restaure l'index ; le fichier d'attente permet de re-ranger).

TYPES_CANDIDAT = ["CV", "Lettre de motivation", "Diplôme", "Pièce d'identité", "Autre / divers"]
TYPES_SANTE = {"Arrêt de travail", "Visite médicale"}   # jamais d'extrait envoyé à l'IA
PJ_EXTRAIT_MAX = 280


def charger_pieces():
    lst = _lire_json(PJ_INDEX) if os.path.exists(PJ_INDEX) else []
    return lst if isinstance(lst, list) else []


def sauvegarder_pieces(lst):
    os.makedirs(PJ_DIR, exist_ok=True)
    _ecrire_json(PJ_INDEX, lst)


def _piece(ref):
    ref = (ref or "").strip()
    return next((p for p in charger_pieces() if p.get("id") == ref), None)


def pieces_en_attente():
    return [p for p in charger_pieces() if not p.get("range")]


def purger_pieces():
    """Supprime les fichiers d'attente périmés (rangés depuis > 7 j, abandonnés > 30 j)."""
    lst = charger_pieces()
    garde, now = [], datetime.now()
    for p in lst:
        try:
            age = (now - datetime.strptime(p.get("ts", ""), "%Y-%m-%d %H:%M")).days
        except ValueError:
            age = 0
        limite = PJ_JOURS_RETENTION if p.get("range") else PJ_JOURS_ABANDON
        if age > limite:
            try:
                os.remove(os.path.join(PJ_DIR, p.get("fichier", "")))
            except OSError:
                pass
            continue
        garde.append(p)
    if len(garde) != len(lst):
        sauvegarder_pieces(garde)


def enregistrer_piece(nom_original, octets):
    """Stocke une pièce en attente et renvoie son enregistrement (type deviné,
    extrait de texte pour l'agent — sauf documents de santé)."""
    import hashlib
    ext = os.path.splitext(nom_original or "")[1].lower()
    if ext not in EXT_DOCS_OK:
        raise ValueError("type")
    if not octets:
        raise ValueError("vide")
    purger_pieces()
    os.makedirs(PJ_DIR, exist_ok=True)
    pid = "pj_" + uuid.uuid4().hex[:8]
    stored = f"{pid}_{secure_filename(nom_original) or 'piece' + ext}"
    with open(os.path.join(PJ_DIR, stored), "wb") as fp:
        fp.write(octets)
    try:
        texte = extraction_pj.extraire_texte(nom_original, octets) or ""
    except Exception:
        texte = ""
    typ = deviner_type_doc(nom_original, texte)
    extrait = ""
    if texte.strip() and typ not in TYPES_SANTE:
        extrait = " ".join(texte.split())[:PJ_EXTRAIT_MAX]
    p = {"id": pid, "fichier": stored, "nom_original": nom_original, "ext": ext,
         "taille": humaniser_taille(len(octets)), "type_devine": typ, "extrait": extrait,
         "texte_lu": bool(texte.strip()), "sha": hashlib.sha256(octets).hexdigest(),
         "ts": datetime.now().strftime("%Y-%m-%d %H:%M"), "range": None}
    lst = charger_pieces()
    lst.append(p)
    sauvegarder_pieces(lst)
    return p


def message_depot_piece(p, legende=""):
    """Message « utilisateur » transmis à l'agent lors d'un dépôt (pseudonymisé
    ensuite par run_agent comme tout message)."""
    genre = {"pdf": "PDF", "docx": "Word", "doc": "Word"}.get(p["ext"].lstrip("."), "photo/image")
    lignes = [f"📎 Pièce jointe déposée : « {p['nom_original']} » ({genre}, {p['taille']}) — id {p['id']}.",
              f"Type probable : {p['type_devine']}."]
    if p.get("extrait"):
        lignes.append(f"Extrait du contenu : « {p['extrait']} »")
    elif not p.get("texte_lu"):
        lignes.append("Contenu non lisible automatiquement (photo ou scan) : demande à l'utilisateur de quoi il s'agit.")
    if legende:
        lignes.append(f"Message de l'utilisateur : {legende}")
    else:
        lignes.append("Demande-moi comment nommer cette pièce et où la ranger.")
    return "\n".join(lignes)


def contexte_pieces():
    """Bloc système listant les pièces non rangées ('' si aucune)."""
    att = pieces_en_attente()
    if not att:
        return ""
    return ("PIÈCES JOINTES EN ATTENTE DE RANGEMENT :\n" + "\n".join(
        f"- {p['id']} « {p['nom_original']} » ({p['taille']}, type probable : {p['type_devine']}, "
        f"déposée le {p['ts'][8:10]}/{p['ts'][5:7]} à {p['ts'][11:]})" for p in att))


def _o_pieces_en_attente(args, annuaire):
    att = pieces_en_attente()
    if not att:
        return "Aucune pièce jointe en attente de rangement."
    return "Pièces en attente :\n" + "\n".join(
        f"- {p['id']} : « {p['nom_original']} » ({p['taille']}), type probable {p['type_devine']}, "
        f"déposée le {p['ts']}" for p in att)


def _date_iso_ou_vide(v):
    v = (v or "").strip()
    if not v:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _w_ranger_piece_jointe(args, annuaire, executer):
    import shutil
    p = _piece(args.get("piece"))
    if not p:
        return "Pièce introuvable (utilise l'id pj_… donné au dépôt ou par pieces_en_attente).", False
    if p.get("range"):
        return f"Cette pièce est déjà rangée ({p['range'].get('resume', '')}).", False
    src = os.path.join(PJ_DIR, p.get("fichier", ""))
    if not os.path.exists(src):
        return "Le fichier de cette pièce n'est plus disponible : redépose-le.", False
    dest = _simplifie(args.get("destination") or "")
    libelle = " ".join((args.get("libelle") or "").split()).strip()[:120]
    if not libelle:
        return "Il manque le libellé (nom lisible) du document.", False
    expiration = _date_iso_ou_vide(args.get("expiration"))
    if expiration is None:
        return "Date d'expiration illisible : donne-la au format AAAA-MM-JJ.", False
    nom_fichier = f"{libelle}{p['ext']}"
    if dest.startswith("cand"):
        cid, c, err = _candidat(args.get("candidat"))
        if err:
            return err, False
        typ = _correspondance(args.get("type"), TYPES_CANDIDAT) or "Autre / divers"
        cible = f"{c.get('prenom', '')} {c.get('nom', '')}".strip()
        resume = f"Ranger « {libelle} » ({typ}) dans le dossier du candidat {cible}"
        if not executer:
            return resume, True
        os.makedirs(REC.CANDIDATS_DOCS_DIR, exist_ok=True)
        doc_id = uuid.uuid4().hex[:12]
        stored = f"{doc_id}_{secure_filename(nom_fichier)}"
        shutil.copyfile(src, os.path.join(REC.CANDIDATS_DOCS_DIR, stored))
        idx = REC.charger_candidats_docs_index()
        idx.setdefault(cid, []).append({
            "id": doc_id, "fichier": stored, "nom_original": p["nom_original"], "type": typ,
            "libelle": libelle, "taille": p["taille"],
            "date_ajout": datetime.now().strftime("%d/%m/%Y %H:%M"), "source": "agent"})
        REC.sauvegarder_candidats_docs_index(idx)
        if typ == "CV":
            try:
                with open(src, "rb") as fp:
                    texte = extraction_pj.extraire_texte(p["nom_original"], fp.read())
                if texte.strip():
                    cands = REC.charger_candidats()
                    cands[cid]["cv_texte"] = crypto_rh.chiffrer(texte)
                    REC.sauvegarder_candidats(cands)
            except Exception:
                current_app.logger.exception("CV déposé via l'agent : texte non mémorisé")
        url = url_for("recrutement.candidat", id=cid)
    else:
        e = _employe(args.get("employe"), annuaire)
        if not e:
            return "Salarié introuvable : précise dans le dossier de quel salarié ranger la pièce.", False
        typ = _correspondance(args.get("type"), TOUS_TYPES_DOCS)
        if not typ:
            return "Type inconnu. Types possibles : " + ", ".join(TOUS_TYPES_DOCS) + ".", False
        resume = f"Ranger « {libelle} » ({typ}) dans le dossier de {_label_de(e, annuaire)}"
        if expiration:
            resume += f", expire le {_fr(_date(expiration))}"
        if not executer:
            return resume, True
        os.makedirs(DOCS_DIR, exist_ok=True)
        doc_id = uuid.uuid4().hex[:12]
        stored = f"{doc_id}_{secure_filename(nom_fichier)}"
        shutil.copyfile(src, os.path.join(DOCS_DIR, stored))
        idx = charger_docs_index()
        entree = {"id": doc_id, "fichier": stored, "nom_original": p["nom_original"], "type": typ,
                  "libelle": libelle, "expiration": expiration, "taille": p["taille"],
                  "date_ajout": datetime.now().strftime("%d/%m/%Y %H:%M"),
                  "source": "agent", "a_valider": False, "sha": p.get("sha", "")}
        idx.setdefault(e["email"], []).append(entree)
        sauvegarder_docs_index(idx)
        if _doc_analysable(typ):
            try:
                nb = _analyser_document(e["email"], entree)
                if nb:
                    resume += f" — {nb} suggestion(s) de pré-remplissage à valider sur la fiche"
            except Exception:
                current_app.logger.exception("Analyse d'une pièce rangée par l'agent")
        url = url_for("admin_document", doc_id=doc_id) + "?voir=1"
    lst = charger_pieces()
    for x in lst:
        if x.get("id") == p["id"]:
            x["range"] = {"quand": datetime.now().strftime("%Y-%m-%d %H:%M"), "resume": resume, "url": url}
    sauvegarder_pieces(lst)
    return resume, True


OUTILS_LECTURE_PJ = {"pieces_en_attente": _o_pieces_en_attente}


# --- Suggestions proactives (calcul local, aucun appel IA) ---------------------------

def suggestions_proactives():
    """Puces « 💡 » au-dessus de la saisie : chacune = un message prêt à envoyer."""
    out = []
    auj = date.today()
    try:
        employes = charger_employes()
        profils = charger_profils()
        actifs = [e for e in employes if collaborateur_actif(profils.get(e["email"], {}))]
        reps = charger_reponses()
        manq = [e for e in actifs if not reponse_de(reps, e["prenom"], e["email"])]
        if manq and auj.day >= 20:
            out.append({"ico": "⏰", "texte": f"{len(manq)} relevé(s) d'heures manquant(s)",
                        "prompt": "Qui n'a pas rendu son relevé ce mois-ci ? Prépare les relances."})
        recus_nv = [e for e in actifs if (reponse_de(reps, e["prenom"], e["email"]) or {}).get("valide") is False
                    or (reponse_de(reps, e["prenom"], e["email"]) and not reponse_de(reps, e["prenom"], e["email"]).get("valide"))]
        if recus_nv and auj.day >= 24:
            out.append({"ico": "🧾", "texte": f"{len(recus_nv)} relevé(s) à valider avant la paie",
                        "prompt": "Montre-moi le dossier paie du mois et ce qui reste à valider."})
        en_att = [d for d in PE.charger_demandes_cp() if d.get("statut") == "en_attente"]
        if en_att:
            out.append({"ico": "🏖️", "texte": f"{len(en_att)} demande(s) de congés en attente",
                        "prompt": "Quelles demandes de congés sont en attente ? Dis-moi lesquelles je peux accepter."})
        nb_al = sum(1 for e in actifs for a in alertes_completes(e["email"], profils.get(e["email"], {}))
                    if a.get("niveau") in ("rouge", "orange"))
        if nb_al:
            out.append({"ico": "⚠️", "texte": f"{nb_al} échéance(s) à surveiller",
                        "prompt": "Quelles sont les échéances RH à venir ?"})
        idx = charger_docs_index()
        nb_av = sum(1 for e in actifs for d in idx.get(e["email"], []) if d.get("a_valider"))
        if nb_av:
            out.append({"ico": "📄", "texte": f"{nb_av} document(s) à valider",
                        "prompt": "Quels documents sont à valider dans les dossiers ?"})
        resumes = _lire_json(ASSISTANT_FILE)
        derniere = max(resumes) if isinstance(resumes, dict) and resumes else None
        if not derniere or derniere < auj.isoformat():
            out.append({"ico": "📬", "texte": "Synthèse des mails pas à jour" if derniere else "Aucune synthèse de mails",
                        "prompt": "Qu'y a-t-il dans les mails RH ? Actualise si besoin."})
        nb_pj = len(pieces_en_attente())
        if nb_pj:
            out.append({"ico": "📎", "texte": f"{nb_pj} pièce(s) jointe(s) à ranger",
                        "prompt": "Quelles pièces jointes restent à ranger ? Propose un nom et un emplacement pour chacune."})
        conv = charger_conversation()
        nb_cartes = sum(1 for m in conv for a in m.get("actions") or []
                        if a.get("type") == "confirmer" and not a.get("fait"))
        if nb_cartes:
            out.append({"ico": "✅", "texte": f"{nb_cartes} proposition(s) à confirmer dans la conversation",
                        "prompt": ""})
        derniere_ronde = next((m.get("ts", "") for m in reversed(conv) if m.get("origine") == "ronde"), "")
        if derniere_ronde[:10] < auj.isoformat():
            out.append({"ico": "🔁", "texte": "Ronde du jour pas encore faite", "prompt": "__ronde__"})
    except Exception:
        current_app.logger.exception("Suggestions proactives")
    return out[:6]

OUTILS_ECRITURE_IMPL = {
    "ajouter_absence": _w_ajouter_absence,
    "supprimer_absence": _w_supprimer_absence,
    "modifier_horaires_jour": _w_modifier_horaires_jour,
    "retablir_horaires_jour": _w_retablir_horaires_jour,
    "traiter_demande_conges": _w_traiter_demande_conges,
    "envoyer_demande_collaborateur": _w_envoyer_demande_collaborateur,
    "ajouter_note_journal": _w_ajouter_note_journal,
    "mettre_a_jour_profil": _w_mettre_a_jour_profil,
    "envoyer_mail": _w_envoyer_mail,
    "envoyer_relance": _w_envoyer_relance,
    "corriger_releve": _w_corriger_releve,
    "valider_releve": _w_valider_releve,
    "envoyer_recap_comptable": _w_envoyer_recap_comptable,
    "annuler_derniere_action": _w_annuler_derniere_action,
    "appliquer_suggestion": _w_appliquer_suggestion,
    "ignorer_suggestion": _w_ignorer_suggestion,
    "analyser_documents": _w_analyser_documents,
    "cocher_checklist": _w_cocher_checklist,
    "changer_statut": _w_changer_statut,
    "valider_document": _w_valider_document,
    "retyper_document": _w_retyper_document,
    "generer_attestation": _w_generer_attestation,
    "envoyer_attestation": _w_envoyer_attestation,
    "actualiser_mails": _w_actualiser_mails,
    "changer_statut_candidat": _w_changer_statut_candidat,
    "envoyer_mail_candidat": _w_envoyer_mail_candidat,
    "memoriser": _w_memoriser,
    "oublier": _w_oublier,
    "ranger_piece_jointe": _w_ranger_piece_jointe,
}
assert set(OUTILS_ECRITURE_IMPL) == OUTILS_ECRITURE, "catalogue agent_rh ≠ implémentations"

LIBELLES_OUTILS = {
    "ranger_piece_jointe": "📎 Pièce jointe",
    "ajouter_absence": "🏖️ Absence", "supprimer_absence": "🗑️ Absence",
    "modifier_horaires_jour": "🕒 Horaires", "retablir_horaires_jour": "↺ Horaires",
    "traiter_demande_conges": "✅ Congés", "envoyer_demande_collaborateur": "📨 Demande",
    "ajouter_note_journal": "📝 Journal", "mettre_a_jour_profil": "👤 Fiche",
    "envoyer_mail": "📧 E-mail", "envoyer_relance": "⏰ Relance",
    "corriger_releve": "🧾 Relevé (paie)", "valider_releve": "✅ Relevé (paie)",
    "envoyer_recap_comptable": "📤 Comptable (paie)", "annuler_derniere_action": "↩ Annulation",
    "appliquer_suggestion": "🔎 Suggestion", "ignorer_suggestion": "🔎 Suggestion",
    "analyser_documents": "📑 Analyse", "cocher_checklist": "☑️ Checklist",
    "changer_statut": "🟢 Statut", "valider_document": "📄 Document",
    "retyper_document": "📄 Document", "generer_attestation": "📄 Attestation",
    "envoyer_attestation": "📧 Attestation", "actualiser_mails": "📬 Mails",
    "changer_statut_candidat": "🧑‍💼 Candidat", "envoyer_mail_candidat": "📧 Candidat",
    "memoriser": "🧠 Mémoire", "oublier": "🧠 Mémoire",
}


def _stabiliser_args(args, annuaire):
    """Arguments venus du modèle (« Employé X », noms de candidats) -> arguments
    sûrs : salarié désigné par son E-MAIL, candidat par son IDENTIFIANT (deux
    homonymes ne se confondent plus), et textes libres (corps de mail, note,
    motif…) ré-identifiés (plus d'« Employé X » dans un mail ou un journal)."""
    a = dict(args or {})
    if a.get("employe") and str(a["employe"]).strip().lower() not in ("tous", "toutes", "tout", "all", "*"):
        e = _employe(a["employe"], annuaire)
        if e:
            a["employe"] = e["email"]
    if a.get("candidat"):
        cid, _, err = _candidat(a["candidat"])
        if not err:
            a["candidat"] = cid
    _, inverse = construire_table(charger_employes())
    return reidentifier(a, inverse)


def _executer_protege(nom, fn, args, annuaire, avant):
    """Exécute une écriture ; en cas d'exception, remet les fichiers comme avant
    (pas d'état à moitié écrit) et renvoie un échec explicite."""
    try:
        return fn(args, annuaire, True)
    except Exception as ex:
        current_app.logger.exception("Outil %s : exception, état restauré", nom)
        try:
            _restaurer(avant)
        except OSError:
            current_app.logger.exception("Restauration après exception")
        return f"erreur interne ({type(ex).__name__}) — aucune modification conservée", False


def executer_outil(nom, args, annuaire, mode, origine="chat"):
    """Callback unique pour agent_rh.run_agent. Lecture -> texte. Écriture ->
    exécute (autonome) ou propose (validation : {resultat, action carte})."""
    args = args or {}
    if nom in OUTILS_LECTURE_PLANNING:
        return OUTILS_LECTURE_PLANNING[nom](args, annuaire)
    if nom in OUTILS_LECTURE_PAIE:
        return OUTILS_LECTURE_PAIE[nom](args, annuaire)
    if nom in OUTILS_LECTURE_DOSSIER:
        return OUTILS_LECTURE_DOSSIER[nom](args, annuaire)
    if nom in OUTILS_LECTURE_MAILS:
        return OUTILS_LECTURE_MAILS[nom](args, annuaire)
    if nom in OUTILS_LECTURE_MEMOIRE:
        return OUTILS_LECTURE_MEMOIRE[nom](args, annuaire)
    if nom in OUTILS_LECTURE_PJ:
        return OUTILS_LECTURE_PJ[nom](args, annuaire)
    if nom in OUTILS_LECTURE_RECRUTEMENT:
        return REC.executer_outil_recrutement(nom, args)   # str ou {resultat, action mailto}
    if nom in OUTILS_ECRITURE_IMPL:
        fn = OUTILS_ECRITURE_IMPL[nom]
        # PAIE et DÉCISIONS RH : jamais d'exécution directe, même en mode autonome.
        if mode == "autonome" and nom not in OUTILS_PAIE and nom not in OUTILS_DECISION:
            args = _stabiliser_args(args, annuaire)
            avant = _instantane(args)
            texte, ok = _executer_protege(nom, fn, args, annuaire, avant)
            if ok:
                if nom != "annuler_derniere_action":   # annuler() se journalise lui-même
                    journaliser(nom, texte, mode, origine)
                aid = enregistrer_annulation(nom, texte, avant, _instantane(args))
                if not aid:
                    return f"FAIT : {texte}"
                return {"resultat": f"FAIT : {texte}",
                        "action": {"type": "fait", "outil": nom, "label": LIBELLES_OUTILS.get(nom, nom),
                                   "resume": texte, "annulation_id": aid}}
            return f"ÉCHEC : {texte}"
        texte, ok = fn(args, annuaire, False)
        if not ok:
            return f"ÉCHEC : {texte}"
        suffixe = (" — PAIE : validation obligatoire" if nom in OUTILS_PAIE
                   else " — DÉCISION RH : validation obligatoire" if nom in OUTILS_DECISION else "")
        return {"resultat": f"PROPOSITION (en attente de validation par l'utilisateur) : {texte}",
                "action": {"type": "confirmer", "outil": nom, "args": _stabiliser_args(args, annuaire),
                           "label": LIBELLES_OUTILS.get(nom, nom) + suffixe, "resume": texte}}
    return executer_outil_agent(nom, args, annuaire)   # outils historiques (app.py)


def confirmer_action(outil, args, origine="carte"):
    """Exécute pour de bon une carte confirmée (args ré-identifiés : prénoms)."""
    fn = OUTILS_ECRITURE_IMPL.get(outil)
    if not fn:
        return f"Outil inconnu : {outil}", False, None
    annuaire = annuaire_pseudo(charger_employes())
    avant = _instantane(args or {})
    texte, ok = _executer_protege(outil, fn, args or {}, annuaire, avant)
    aid = None
    if ok:
        if outil != "annuler_derniere_action":
            journaliser(outil, texte, "validation", origine)
        aid = enregistrer_annulation(outil, texte, avant, _instantane(args or {}))
    return texte, ok, aid


# --- Boucle de conversation ------------------------------------------------------

MODELE_AGENT_DEFAUT = {"mistral": "mistral-medium-latest", "claude": "claude-sonnet-4-5"}


def _moteur():
    """Moteur + modèle. Sans ASSISTANT_MODELE, l'agent prend un modèle « medium » :
    testé le 29/08/2026, mistral-small confond « lundi prochain » et n'appelle pas
    les outils d'écriture ; mistral-medium fait les deux correctement."""
    moteur = os.getenv("ASSISTANT_MOTEUR", "mistral")
    return moteur, (os.getenv("ASSISTANT_MODELE") or MODELE_AGENT_DEFAUT.get(moteur))


def repondre(texte_utilisateur, origine="chat", contexte="", nb_contexte=TOURS_CONTEXTE,
             piece=None):
    """Ajoute le message utilisateur, fait tourner l'agent sur les derniers
    échanges, persiste et renvoie la réponse {reply, actions, outils_utilises}.
    nb_contexte : messages d'historique envoyés au modèle (la ronde en envoie
    peu : chaque appel coûte des tokens et le palier Mistral est limité/minute)."""
    mode = mode_agent()
    ajouter_message("user", texte_utilisateur, origine=origine if origine != "chat" else None,
                    piece=piece)
    conv = charger_conversation()
    messages = [{"role": m["role"], "content": m["content"]}
                for m in conv if m.get("role") in ("user", "assistant")
                and not m.get("systeme") and not m.get("erreur")
                and not (m.get("role") == "user" and m.get("origine") == "ronde"
                         and m.get("content") != texte_utilisateur)][-nb_contexte:]
    employes = charger_employes()
    annuaire = annuaire_pseudo(employes)
    roster = _roster_pseudo(annuaire, charger_profils())
    moteur, modele = _moteur()
    table, _ = construire_table(employes)
    mem = memoire_contexte(table)
    if mem:
        contexte = (contexte + "\n\n" if contexte else "") + mem
    pj = contexte_pieces()
    if pj:
        contexte = (contexte + "\n\n" if contexte else "") + pj

    def _exec(nom, args, ann):
        return executer_outil(nom, args, ann, mode, origine)

    res = run_agent(messages, employes, _exec, moteur=moteur, modele=modele,
                    roster_txt=roster, mode=mode, contexte=contexte)
    actions = [a for a in (res.get("actions") or []) if a]
    m = ajouter_message("assistant", res.get("reply") or "(pas de réponse)",
                        actions=actions, outils=res.get("outils_utilises") or [],
                        origine=origine if origine != "chat" else None)
    return {"reply": m["content"], "actions": actions, "outils_utilises": m.get("outils", []),
            "id": m["id"], "ts": m["ts"], "mode": mode, "piece": piece}


BRIEF_RONDE = (
    "Fais ta RONDE quotidienne de gestion RH. Passe en revue, dans l'ordre : "
    "1) mails_rh_du_jour — si la synthèse date d'hier ou plus, lance actualiser_mails ; "
    "relève les tâches de priorité haute et les échéances, et pour chacune PROPOSE "
    "l'action concrète avec l'outil adapté (préparer un document, relancer, noter au "
    "journal, ajouter une absence…) ; "
    "2) releves_manquants — si la clôture (le 25) est dans 3 jours ou moins, envoie une "
    "relance (envoyer_relance) à chaque retardataire ; "
    "3) demandes_conges_en_attente — accepte (traiter_demande_conges) uniquement si le "
    "solde restant couvre les jours demandés, qu'aucune absence ne chevauche et "
    "qu'aucun autre salarié n'est déjà absent sur la période ; sinon laisse en attente "
    "et explique pourquoi ; "
    "4) echeances_a_venir et absences_en_cours — signale ce qui mérite attention "
    "(fin de CDD, période d'essai, visite médicale, retour d'absence) sans rien écrire ; "
    "5) documents_manquants_equipe — signale les dossiers incomplets, et propose "
    "d'envoyer un e-mail (envoyer_mail) au salarié pour réclamer ce qui manque quand "
    "un document obligatoire est absent. "
    "Termine par un compte-rendu court et structuré : ce que tu as fait, ce qui attend "
    "ma décision, ce à quoi je dois penser. Si tout est en ordre, dis-le en une phrase. "
    "Ne touche pas à la paie (aucune correction/validation de relevé, aucun envoi au "
    "comptable) pendant la ronde."
)


def ronde():
    """Ronde autonome (cron ou bouton). Mode autonome : exécute ; mode validation :
    propose des cartes. Le compte-rendu est posté dans la conversation."""
    contexte = ("RONDE AUTOMATIQUE : tu parles à l'utilisateur sans qu'il t'ait posé de "
                "question ; sois concis et concret.")
    return repondre(BRIEF_RONDE, origine="ronde", contexte=contexte, nb_contexte=1)


# --- Routes ----------------------------------------------------------------------

def _detail_erreur_ia(e):
    """Message lisible pour l'utilisateur selon l'erreur du moteur IA."""
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return {401: "clé API refusée (vérifie MISTRAL_API_KEY / ANTHROPIC_API_KEY)",
                429: "limite de débit Mistral atteinte (palier gratuit : 25 000 tokens/minute) — attends une minute et réessaie, ou active la facturation sur console.mistral.ai",
                }.get(e.code, f"erreur HTTP {e.code} du moteur IA") + "."
    if isinstance(e, (RuntimeError, TimeoutError)):
        return f"{e}"
    return f"{type(e).__name__} — vérifie la clé API / le réseau et réessaie."


def _json(data, status=200):
    return current_app.response_class(json.dumps(data, ensure_ascii=False),
                                      status=status, mimetype="application/json")


@bp.route("/admin/agent")
def page():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    conv = charger_conversation()
    journal = list(reversed(charger_journal()[-50:]))
    moteur, modele = _moteur()
    return render_template("admin_agent.html", conversation=conv, journal=journal,
                           mode=mode_agent(), moteur=moteur,
                           outils_libelles=LIBELLES_OUTILS,
                           suggestions=suggestions_proactives(), memoire=charger_memoire(),
                           msg=request.args.get("msg", ""))


@bp.route("/admin/agent/oublier", methods=["POST"])
def oublier_route():
    """Bouton du panneau Mémoire : efface un souvenir."""
    if not session.get("admin"):
        return redirect(url_for("admin"))
    sid = request.form.get("id", "")
    lst = charger_memoire()
    cible = next((x for x in lst if x.get("id") == sid), None)
    if cible:
        sauvegarder_memoire([x for x in lst if x.get("id") != sid])
        journaliser("oublier", f"Oublier : « {cible.get('texte')} »", mode_agent(), "panneau")
    return redirect(url_for("agent.page"))


@bp.route("/admin/agent/chat", methods=["POST"])
def chat():
    if not session.get("admin"):
        return _json({"error": "non autorisé"}, 403)
    data = request.get_json(force=True, silent=True) or {}
    texte = (data.get("message") or "").strip()
    if not texte:
        return _json({"error": "message vide"}, 400)
    try:
        return _json(repondre(texte[:4000]))
    except Exception as e:
        current_app.logger.exception("Agent RH : échec")
        detail = _detail_erreur_ia(e)
        ajouter_message("assistant", f"⚠️ Je n'ai pas pu répondre : {detail}", erreur=True)
        return _json({"error": f"Service IA indisponible : {detail}"}, 502)


@bp.route("/admin/agent/piece", methods=["POST"])
def deposer_piece():
    """Dépôt d'un fichier ou d'une photo dans le chat : la pièce est mise en
    attente, puis l'agent demande comment la nommer et où la ranger."""
    if not session.get("admin"):
        return _json({"error": "non autorisé"}, 403)
    f = request.files.get("fichier")
    if not f or not f.filename:
        return _json({"error": "aucun fichier"}, 400)
    try:
        p = enregistrer_piece(f.filename, f.read())
    except ValueError as e:
        return _json({"error": "Format non accepté (PDF, JPG, PNG, Word)." if str(e) == "type"
                      else "Fichier vide."}, 400)
    legende = (request.form.get("message") or "").strip()[:2000]
    piece_ui = {"id": p["id"], "nom": p["nom_original"], "taille": p["taille"],
                "url": url_for("agent.voir_piece", pid=p["id"]), "legende": legende}
    try:
        return _json(repondre(message_depot_piece(p, legende), piece=piece_ui))
    except Exception as e:
        current_app.logger.exception("Agent RH : échec après dépôt de pièce")
        detail = _detail_erreur_ia(e)
        ajouter_message("assistant", f"⚠️ Pièce reçue ({p['id']}) mais je n'ai pas pu répondre : {detail}",
                        erreur=True)
        return _json({"error": f"Pièce reçue ({p['id']}) mais service IA indisponible : {detail}",
                      "piece": piece_ui}, 502)


@bp.route("/admin/agent/piece/<pid>")
def voir_piece(pid):
    """Ouvre une pièce déposée : le document rangé si elle l'a été, sinon le
    fichier en attente (admin uniquement)."""
    if not session.get("admin"):
        return redirect(url_for("admin"))
    p = _piece(pid)
    if not p:
        abort(404)
    if p.get("range") and p["range"].get("url"):
        return redirect(p["range"]["url"])
    chemin = os.path.join(PJ_DIR, p.get("fichier", ""))
    if not os.path.exists(chemin):
        abort(404)
    from flask import send_file
    return send_file(chemin, download_name=p.get("nom_original"), as_attachment=False)


@bp.route("/admin/agent/confirmer", methods=["POST"])
def confirmer():
    """Clic sur une carte (mode validation) : exécute l'écriture proposée."""
    if not session.get("admin"):
        return _json({"error": "non autorisé"}, 403)
    data = request.get_json(force=True, silent=True) or {}
    msg_id, idx = data.get("id"), int(data.get("idx", -1))
    conv = charger_conversation()
    m = next((x for x in conv if x.get("id") == msg_id), None)
    act = (m or {}).get("actions", [])[idx] if m and 0 <= idx < len(m.get("actions", [])) else None
    if not act or act.get("type") != "confirmer":
        return _json({"error": "carte introuvable"}, 404)
    if act.get("fait"):
        return _json({"error": "déjà confirmée", "resultat": act["fait"]}, 409)
    texte, ok, aid = confirmer_action(act.get("outil"), act.get("args") or {})
    if ok:
        marquer_action_faite(msg_id, idx, texte, aid)
        ajouter_message("assistant", f"✅ Fait : {texte}", systeme=True)
    return _json({"ok": ok, "resultat": texte, "annulation_id": aid}, 200 if ok else 422)


@bp.route("/admin/agent/annuler", methods=["POST"])
def annuler_route():
    """Bouton ↩ d'une carte (ou « annuler la dernière action ») : rétablit
    l'état d'avant. Seule la dernière action non annulée est acceptée."""
    if not session.get("admin"):
        return _json({"error": "non autorisé"}, 403)
    data = request.get_json(force=True, silent=True) or {}
    texte, ok = annuler(data.get("annulation_id") or None, origine="carte")
    if ok:
        ajouter_message("assistant", f"↩ {texte}", systeme=True)
    return _json({"ok": ok, "resultat": texte}, 200 if ok else 422)


@bp.route("/admin/agent/mode", methods=["POST"])
def changer_mode():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    m = definir_mode(request.form.get("mode", "validation"))
    ajouter_message("assistant",
                    "⚡ Mode AUTONOME activé : j'exécute directement ce que tu me demandes "
                    "et je fais ma ronde quotidienne seul." if m == "autonome" else
                    "🤝 Mode VALIDATION activé : je te propose chaque changement, tu confirmes d'un clic.",
                    systeme=True)
    return redirect(url_for("agent.page"))


@bp.route("/admin/agent/ronde", methods=["POST"])
def ronde_manuelle():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    try:
        ronde()
    except Exception as e:
        current_app.logger.exception("Ronde agent : échec")
        ajouter_message("assistant", f"⚠️ Ronde interrompue ({type(e).__name__}).", erreur=True)
    return redirect(url_for("agent.page"))


@bp.route("/agent_ronde", methods=["POST", "GET"])
def ronde_machine():
    """Déclencheur machine (cron GitHub Actions) — clé API requise."""
    if request.args.get("cle", "") != API_CLE:
        abort(403)
    try:
        res = ronde()
        return _json({"ok": True, "mode": res["mode"], "reply": res["reply"]})
    except Exception as e:
        current_app.logger.exception("Ronde agent (machine) : échec")
        ajouter_message("assistant", f"⚠️ Ronde interrompue : {_detail_erreur_ia(e)}", erreur=True)
        return _json({"ok": False, "error": type(e).__name__}, 502)


@bp.route("/admin/agent/effacer", methods=["POST"])
def effacer():
    """Vide la conversation (le journal d'audit est conservé)."""
    if not session.get("admin"):
        return redirect(url_for("admin"))
    _ecrire_json(CONVERSATION_FILE, [])
    return redirect(url_for("agent.page"))
