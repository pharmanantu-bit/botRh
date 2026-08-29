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
                 executer_outil_agent, _roster_pseudo, _OUTILS_AGENT, declencher_workflow)
import planning_equipe as PE
from agent_rh import OUTILS_ECRITURE, OUTILS_SPECS, run_agent
from assistant_rh import annuaire_pseudo

bp = Blueprint("agent", __name__)

CONVERSATION_FILE = os.path.join(BASE_DIR, "agent_conversation.json")
JOURNAL_FILE = os.path.join(BASE_DIR, "agent_journal.json")
OPTIONS_FILE = os.path.join(BASE_DIR, "agent_options.json")
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


def marquer_action_faite(msg_id, idx, resultat):
    """Marque la carte n° idx du message comme confirmée (texte du résultat)."""
    conv = charger_conversation()
    for m in conv:
        if m.get("id") == msg_id:
            acts = m.get("actions") or []
            if 0 <= idx < len(acts):
                acts[idx]["fait"] = resultat
                acts[idx]["fait_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            break
    _ecrire_json(CONVERSATION_FILE, conv)


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
}
assert set(OUTILS_ECRITURE_IMPL) == OUTILS_ECRITURE, "catalogue agent_rh ≠ implémentations"

LIBELLES_OUTILS = {
    "ajouter_absence": "🏖️ Absence", "supprimer_absence": "🗑️ Absence",
    "modifier_horaires_jour": "🕒 Horaires", "retablir_horaires_jour": "↺ Horaires",
    "traiter_demande_conges": "✅ Congés", "envoyer_demande_collaborateur": "📨 Demande",
    "ajouter_note_journal": "📝 Journal", "mettre_a_jour_profil": "👤 Fiche",
    "envoyer_mail": "📧 E-mail", "envoyer_relance": "⏰ Relance",
}


def executer_outil(nom, args, annuaire, mode, origine="chat"):
    """Callback unique pour agent_rh.run_agent. Lecture -> texte. Écriture ->
    exécute (autonome) ou propose (validation : {resultat, action carte})."""
    args = args or {}
    if nom in OUTILS_LECTURE_PLANNING:
        return OUTILS_LECTURE_PLANNING[nom](args, annuaire)
    if nom in OUTILS_ECRITURE_IMPL:
        fn = OUTILS_ECRITURE_IMPL[nom]
        if mode == "autonome":
            texte, ok = fn(args, annuaire, True)
            if ok:
                journaliser(nom, texte, mode, origine)
                return f"FAIT : {texte}"
            return f"ÉCHEC : {texte}"
        texte, ok = fn(args, annuaire, False)
        if not ok:
            return f"ÉCHEC : {texte}"
        return {"resultat": f"PROPOSITION (en attente de validation par l'utilisateur) : {texte}",
                "action": {"type": "confirmer", "outil": nom, "args": args,
                           "label": LIBELLES_OUTILS.get(nom, nom), "resume": texte}}
    return executer_outil_agent(nom, args, annuaire)   # outils historiques (app.py)


def confirmer_action(outil, args, origine="carte"):
    """Exécute pour de bon une carte confirmée (args ré-identifiés : prénoms)."""
    fn = OUTILS_ECRITURE_IMPL.get(outil)
    if not fn:
        return f"Outil inconnu : {outil}", False
    annuaire = annuaire_pseudo(charger_employes())
    texte, ok = fn(args or {}, annuaire, True)
    if ok:
        journaliser(outil, texte, "validation", origine)
    return texte, ok


# --- Boucle de conversation ------------------------------------------------------

def _moteur():
    return os.getenv("ASSISTANT_MOTEUR", "mistral"), (os.getenv("ASSISTANT_MODELE") or None)


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
    "ma décision, ce à quoi je dois penser. Si tout est en ordre, dis-le en une phrase."
)


def ronde():
    """Ronde autonome (cron ou bouton). Mode autonome : exécute ; mode validation :
    propose des cartes. Le compte-rendu est posté dans la conversation."""
    contexte = ("RONDE AUTOMATIQUE : tu parles à l'utilisateur sans qu'il t'ait posé de "
                "question ; sois concis et concret.")
    return repondre(BRIEF_RONDE, origine="ronde", contexte=contexte)


# --- Routes ----------------------------------------------------------------------

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
        ajouter_message("assistant", f"⚠️ Je n'ai pas pu répondre ({type(e).__name__}). "
                                     "Vérifie la clé API / le réseau et réessaie.", erreur=True)
        return _json({"error": f"Service IA indisponible ({type(e).__name__})."}, 502)


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
    texte, ok = confirmer_action(act.get("outil"), act.get("args") or {})
    if ok:
        marquer_action_faite(msg_id, idx, texte)
        ajouter_message("assistant", f"✅ Fait : {texte}", systeme=True)
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
