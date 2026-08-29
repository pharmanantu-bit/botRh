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
                 paie_envoi_file, PROFILS_FILE)
import planning_equipe as PE
from agent_rh import OUTILS_ECRITURE, OUTILS_SPECS, OUTILS_PAIE, run_agent
from assistant_rh import annuaire_pseudo
from tokens import tokens_valides, reponse_de, generer_token

bp = Blueprint("agent", __name__)

CONVERSATION_FILE = os.path.join(BASE_DIR, "agent_conversation.json")
JOURNAL_FILE = os.path.join(BASE_DIR, "agent_journal.json")
OPTIONS_FILE = os.path.join(BASE_DIR, "agent_options.json")
ANNULATIONS_FILE = os.path.join(BASE_DIR, "agent_annulations.json")
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

OUTILS_IRREVERSIBLES = {"envoyer_mail", "envoyer_relance", "envoyer_recap_comptable"}
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
         PROFILS_FILE, reponses_file(mois, annee), reponses_file()]
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


def enregistrer_annulation(outil, resume, etat_avant):
    """Après une écriture réussie : conserve l'état d'avant. Renvoie l'id."""
    if outil in OUTILS_IRREVERSIBLES or outil == "annuler_derniere_action":
        return None
    lst = charger_annulations()
    aid = uuid.uuid4().hex[:10]
    lst.append({"id": aid, "ts": datetime.now().strftime("%d/%m/%Y %H:%M"), "outil": outil,
                "resume": resume, "etat": etat_avant, "annulee": False})
    _ecrire_json(ANNULATIONS_FILE, lst[-MAX_ANNULATIONS:])
    return aid


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
    for f, contenu in (derniere.get("etat") or {}).items():
        if contenu is None:
            if os.path.exists(f):
                os.remove(f)
        else:
            with open(f, "w", encoding="utf-8") as fp:
                fp.write(contenu)
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
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
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


def _envoyer_mail_reel(dest, sujet, corps):
    """Runner GitHub si GITHUB_TOKEN (serveur), sinon SMTP direct (local)."""
    if os.getenv("GITHUB_TOKEN"):
        declencher_workflow("mail_agent", {"to": dest, "subject": sujet, "body": corps})
        return "envoi confié au runner GitHub (quelques minutes)"
    import smtplib
    from email.mime.text import MIMEText
    user, pwd = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    if not user or not pwd:
        raise RuntimeError("ni GITHUB_TOKEN ni identifiants Gmail : envoi impossible")
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
    resume = f"Relance du relevé d'heures à {lab} — objet « {act.get('subject')} »"
    if not executer:
        return resume, True
    if current_app.config.get("TESTING"):
        return resume + " (TEST : non envoyée)", True
    try:
        statut = _envoyer_mail_reel(e["email"], act.get("subject", "Rappel"), corps)
    except Exception as ex:
        return f"Échec de la relance à {lab} : {type(ex).__name__}.", False
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
    declencher_workflow("envoi_comptable", {"mois": mois, "annee": annee, "destinataires": dest})
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
}
assert set(OUTILS_ECRITURE_IMPL) == OUTILS_ECRITURE, "catalogue agent_rh ≠ implémentations"

LIBELLES_OUTILS = {
    "ajouter_absence": "🏖️ Absence", "supprimer_absence": "🗑️ Absence",
    "modifier_horaires_jour": "🕒 Horaires", "retablir_horaires_jour": "↺ Horaires",
    "traiter_demande_conges": "✅ Congés", "envoyer_demande_collaborateur": "📨 Demande",
    "ajouter_note_journal": "📝 Journal", "mettre_a_jour_profil": "👤 Fiche",
    "envoyer_mail": "📧 E-mail", "envoyer_relance": "⏰ Relance",
    "corriger_releve": "🧾 Relevé (paie)", "valider_releve": "✅ Relevé (paie)",
    "envoyer_recap_comptable": "📤 Comptable (paie)", "annuler_derniere_action": "↩ Annulation",
}


def executer_outil(nom, args, annuaire, mode, origine="chat"):
    """Callback unique pour agent_rh.run_agent. Lecture -> texte. Écriture ->
    exécute (autonome) ou propose (validation : {resultat, action carte})."""
    args = args or {}
    if nom in OUTILS_LECTURE_PLANNING:
        return OUTILS_LECTURE_PLANNING[nom](args, annuaire)
    if nom in OUTILS_LECTURE_PAIE:
        return OUTILS_LECTURE_PAIE[nom](args, annuaire)
    if nom in OUTILS_ECRITURE_IMPL:
        fn = OUTILS_ECRITURE_IMPL[nom]
        # PAIE : jamais d'exécution directe, même en mode autonome.
        if mode == "autonome" and nom not in OUTILS_PAIE:
            avant = _instantane(args)
            texte, ok = fn(args, annuaire, True)
            if ok:
                journaliser(nom, texte, mode, origine)
                aid = enregistrer_annulation(nom, texte, avant)
                if not aid:
                    return f"FAIT : {texte}"
                return {"resultat": f"FAIT : {texte}",
                        "action": {"type": "fait", "outil": nom, "label": LIBELLES_OUTILS.get(nom, nom),
                                   "resume": texte, "annulation_id": aid}}
            return f"ÉCHEC : {texte}"
        texte, ok = fn(args, annuaire, False)
        if not ok:
            return f"ÉCHEC : {texte}"
        suffixe = " — PAIE : validation obligatoire" if nom in OUTILS_PAIE else ""
        return {"resultat": f"PROPOSITION (en attente de validation par l'utilisateur) : {texte}",
                "action": {"type": "confirmer", "outil": nom, "args": args,
                           "label": LIBELLES_OUTILS.get(nom, nom) + suffixe, "resume": texte}}
    return executer_outil_agent(nom, args, annuaire)   # outils historiques (app.py)


def confirmer_action(outil, args, origine="carte"):
    """Exécute pour de bon une carte confirmée (args ré-identifiés : prénoms)."""
    fn = OUTILS_ECRITURE_IMPL.get(outil)
    if not fn:
        return f"Outil inconnu : {outil}", False, None
    annuaire = annuaire_pseudo(charger_employes())
    avant = _instantane(args or {})
    texte, ok = fn(args or {}, annuaire, True)
    aid = None
    if ok:
        journaliser(outil, texte, "validation", origine)
        aid = enregistrer_annulation(outil, texte, avant)
    return texte, ok, aid


# --- Boucle de conversation ------------------------------------------------------

MODELE_AGENT_DEFAUT = {"mistral": "mistral-medium-latest", "claude": "claude-sonnet-4-5"}


def _moteur():
    """Moteur + modèle. Sans ASSISTANT_MODELE, l'agent prend un modèle « medium » :
    testé le 29/08/2026, mistral-small confond « lundi prochain » et n'appelle pas
    les outils d'écriture ; mistral-medium fait les deux correctement."""
    moteur = os.getenv("ASSISTANT_MOTEUR", "mistral")
    return moteur, (os.getenv("ASSISTANT_MODELE") or MODELE_AGENT_DEFAUT.get(moteur))


def repondre(texte_utilisateur, origine="chat", contexte=""):
    """Ajoute le message utilisateur, fait tourner l'agent sur les derniers
    échanges, persiste et renvoie la réponse {reply, actions, outils_utilises}."""
    mode = mode_agent()
    ajouter_message("user", texte_utilisateur, origine=origine if origine != "chat" else None)
    conv = charger_conversation()
    messages = [{"role": m["role"], "content": m["content"]}
                for m in conv if m.get("role") in ("user", "assistant")][-TOURS_CONTEXTE:]
    employes = charger_employes()
    annuaire = annuaire_pseudo(employes)
    roster = _roster_pseudo(annuaire, charger_profils())
    moteur, modele = _moteur()

    def _exec(nom, args, ann):
        return executer_outil(nom, args, ann, mode, origine)

    res = run_agent(messages, employes, _exec, moteur=moteur, modele=modele,
                    roster_txt=roster, mode=mode, contexte=contexte)
    actions = [a for a in (res.get("actions") or []) if a]
    m = ajouter_message("assistant", res.get("reply") or "(pas de réponse)",
                        actions=actions, outils=res.get("outils_utilises") or [],
                        origine=origine if origine != "chat" else None)
    return {"reply": m["content"], "actions": actions, "outils_utilises": m.get("outils", []),
            "id": m["id"], "ts": m["ts"], "mode": mode}


BRIEF_RONDE = (
    "Fais ta RONDE quotidienne de gestion RH. Passe en revue, dans l'ordre : "
    "1) releves_manquants — si la clôture (le 25) est dans 3 jours ou moins, envoie une "
    "relance (envoyer_relance) à chaque retardataire ; "
    "2) demandes_conges_en_attente — accepte (traiter_demande_conges) uniquement si le "
    "solde restant couvre les jours demandés, qu'aucune absence ne chevauche et "
    "qu'aucun autre salarié n'est déjà absent sur la période ; sinon laisse en attente "
    "et explique pourquoi ; "
    "3) echeances_a_venir et absences_en_cours — signale ce qui mérite attention "
    "(fin de CDD, période d'essai, visite médicale, retour d'absence) sans rien écrire. "
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
    return repondre(BRIEF_RONDE, origine="ronde", contexte=contexte)


# --- Routes ----------------------------------------------------------------------

def _detail_erreur_ia(e):
    """Message lisible pour l'utilisateur selon l'erreur du moteur IA."""
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return {401: "clé API refusée (vérifie MISTRAL_API_KEY / ANTHROPIC_API_KEY)",
                429: "trop de requêtes en même temps (limite de débit Mistral) — réessaie dans quelques secondes",
                }.get(e.code, f"erreur HTTP {e.code} du moteur IA") + "."
    if isinstance(e, RuntimeError):
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
                           msg=request.args.get("msg", ""))


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
        return _json({"ok": False, "error": type(e).__name__}, 502)


@bp.route("/admin/agent/effacer", methods=["POST"])
def effacer():
    """Vide la conversation (le journal d'audit est conservé)."""
    if not session.get("admin"):
        return redirect(url_for("admin"))
    _ecrire_json(CONVERSATION_FILE, [])
    return redirect(url_for("agent.page"))
