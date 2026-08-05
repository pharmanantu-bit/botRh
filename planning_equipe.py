"""Module PLANNING D'ÉQUIPE (Blueprint Flask) — façon Mon Planning Pharma.

UNE seule entrée de menu « Planning » ; tout le reste en sous-onglets internes
(?onglet=). Cœur 100 % local (aucun réseau) → marche sur PythonAnywhere gratuit.

Itération 1 : la TRAME (semaines tournantes A/B, saisie d'heures tapées) + la FRISE
colorée (rendu Gantt horizontal). Sous-onglets Effectifs / Changements / Totaux à venir.

Cf. docs/planning_specs.md pour le modèle complet.
"""
import os
import uuid
import calendar
from datetime import date, datetime, timedelta

from flask import (Blueprint, request, render_template, redirect, url_for,
                   session, abort, current_app)

from signature_mail import SIGNATURE

from app import (_lire_json, _ecrire_json, BASE_DIR, charger_employes,
                 charger_profils, sauvegarder_profils, couleur_collaborateur,
                 collaborateur_actif, poste_de, POSTES, PALETTE_PLANNING,
                 _heures_hebdo)

bp = Blueprint("planning_equipe", __name__)

TRAME_FILE = os.path.join(BASE_DIR, "planning_trame.json")

JOURS_NOMS = {1: "Lundi", 2: "Mardi", 3: "Mercredi", 4: "Jeudi",
              5: "Vendredi", 6: "Samedi", 7: "Dimanche"}
JOURS_ABBR = {1: "Lun", 2: "Mar", 3: "Mer", 4: "Jeu", 5: "Ven", 6: "Sam", 7: "Dim"}


def jour_courant():
    """Jour à ENCADRER en rouge (« aujourd'hui ») dans le planning.

    Heure de PARIS et non du serveur : PythonAnywhere tourne en UTC, donc sans
    correction le cadre ne passait au jour suivant qu'à 1 h ou 2 h du matin
    (selon la saison) au lieu de minuit pile."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris")).date()
    except Exception:              # base de fuseaux absente (ex. Windows sans tzdata)
        return datetime.now().date()
MOIS_ABBR = {1: "Jan", 2: "Fév", 3: "Mar", 4: "Avr", 5: "Mai", 6: "Juin",
             7: "Juil", 8: "Août", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Déc"}
SEMAINES = ["A", "B"]  # rotation par défaut (2 semaines tournantes)

HORAIRES_DEFAUT = {
    "1": [["09:00", "19:30"]], "2": [["09:00", "19:30"]], "3": [["09:00", "19:30"]],
    "4": [["09:00", "19:30"]], "5": [["09:00", "19:30"]], "6": [["09:00", "12:30"]], "7": [],
}
TRAME_DEFAUT = {
    "date_demarrage": "", "nb_semaines": 2, "semaine_demarrage": "A",
    "horaires_ouverture": HORAIRES_DEFAUT, "employes": {},
}


def _admin():
    return session.get("admin")


# --- Options d'affichage du planning (globales) -----------------------------
OPTIONS_FILE = os.path.join(BASE_DIR, "planning_options.json")
OPTIONS_DEFAUT = {
    "collaborateurs_masques": [],                       # e-mails masqués
    "jours": ["1", "2", "3", "4", "5", "6", "7"],       # jours affichés (ISO str)
    "periode": "hebdo",                                  # hebdo / mensuel / periode
    "mode": "grille",                                    # grille / texte / tableau
    "lignes_vides": "afficher",                          # afficher / masquer
    "horaires_grille": "afficher",                       # afficher / masquer (libellés sur barres)
    "recap_changements": "masquer",                      # afficher / masquer
    "heures_travaillees": "aucun",                       # aucun / non_admin / tous
}


def charger_options():
    o = dict(OPTIONS_DEFAUT)
    saved = _lire_json(OPTIONS_FILE)
    if isinstance(saved, dict):
        o.update({k: v for k, v in saved.items() if v is not None})
    return o


def sauvegarder_options(o):
    _ecrire_json(OPTIONS_FILE, o)


# --- Changements ponctuels (surcharge de la trame pour une date réelle) ------
CHANGEMENTS_FILE = os.path.join(BASE_DIR, "planning_changements.json")
MOTIFS = ["Non catégorisé", "Heures sup/récup/échanges", "Contrat ponctuel",
          "Repos compensatoire", "Garde", "Congés payés", "Arrêt maladie",
          "Accident du travail", "Congé maternité", "Congé parental",
          "Formation", "Congé sans solde", "Autre"]


def charger_changements():
    """{date_iso: {email: {motif, creneaux:[...]}}}."""
    d = _lire_json(CHANGEMENTS_FILE)
    return d if isinstance(d, dict) else {}


def sauvegarder_changements(d):
    _ecrire_json(CHANGEMENTS_FILE, d)


def changement_de(changements, date_iso, email):
    return (changements.get(date_iso, {}) or {}).get(email)


# --- Absences prolongées (plage de dates : congés, arrêt, fin de contrat…) ----
ABSENCES_FILE = os.path.join(BASE_DIR, "planning_absences.json")


def charger_absences():
    """Liste d'absences : [{id, email, debut, fin, motif, commentaire}]."""
    d = _lire_json(ABSENCES_FILE, [])
    return d if isinstance(d, list) else []


def sauvegarder_absences(lst):
    _ecrire_json(ABSENCES_FILE, lst)


def absence_active(absences, email, date_obj):
    """Absence couvrant cette date pour ce collaborateur (ou None)."""
    iso = date_obj.isoformat()
    for a in absences:
        if a.get("email") == email and a.get("debut", "") <= iso <= a.get("fin", "9999"):
            return a
    return None


def ponctuel_redondant(absences, email, date_obj, motif, creneaux):
    """True si un changement ponctuel « non travaillé » est déjà couvert par une
    absence prolongée au même motif : il n'apporte rien (l'absence vide déjà le
    jour) et encombrerait le journal. Un ponctuel avec des horaires, ou avec un
    motif différent de l'absence, reste une vraie information."""
    if creneaux:
        return False
    a = absence_active(absences, email, date_obj)
    return a is not None and a.get("motif") == motif


def creneaux_effectifs_jour(trame, email, d, changements, absences):
    """Créneaux réellement travaillés à une date : trame surchargée par le
    changement ponctuel, annulée par une absence prolongée ou un jour férié."""
    chg = changement_de(changements, d.isoformat(), email)
    if chg is not None:
        cr = chg.get("creneaux", []) or []
    elif absence_active(absences, email, d) is not None or ferie_de(d):
        cr = []
    else:
        cr = creneaux_trame_jour(trame, email, d)
    return [c for c in cr if creneau_valide(c)]


# --- Conformité durée du travail (contrôle INDICATIF, Code du travail / CCN) --
REPOS_QUOTIDIEN_MIN = 11 * 60      # repos entre deux journées : 11 h minimum
JOUR_MAX_MIN = 10 * 60             # travail effectif : 10 h max par jour
SEMAINE_MAX_MIN = 48 * 60          # 48 h max par semaine civile
JOURS_CONSECUTIFS_MAX = 6          # au plus 6 jours travaillés d'affilée


def _fmt_h(minutes):
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes % 60 else f"{minutes // 60}h"


def alertes_conformite(data, emp_sm, lundi, changements, absences):
    """Contrôle indicatif de la semaine affichée : repos quotidien < 11 h,
    journée > 10 h, semaine > 48 h, plus de 6 jours travaillés consécutifs.
    Fenêtre élargie aux 6 jours PRÉCÉDANT le lundi (séries à cheval sur deux
    semaines, repos dimanche→lundi). Renvoie [{prenom, texte}]."""
    alertes = []
    for e in emp_sm:
        em, prenom = e["email"], e["prenom"]
        jours = {}
        for k in range(-6, 7):
            d = lundi + timedelta(days=k)
            tr = trame_active_pour(data, d)
            cr = creneaux_effectifs_jour(tr, em, d, changements, absences) if tr else []
            mins = [(_minutes(c.get("debut")), _minutes(c.get("fin"))) for c in cr]
            jours[k] = sorted((a, b) for a, b in mins
                              if a is not None and b is not None and b > a)
        # Journée > 10 h + total de la semaine affichée
        total_sem = 0
        for k in range(7):
            tot = sum(b - a for a, b in jours[k])
            total_sem += tot
            if tot > JOUR_MAX_MIN:
                d = lundi + timedelta(days=k)
                alertes.append({"prenom": prenom, "texte":
                                f"{JOURS_ABBR[d.isoweekday()]} {d.strftime('%d/%m')} : "
                                f"{_fmt_h(tot)} de travail (max 10h/jour)"})
        if total_sem > SEMAINE_MAX_MIN:
            alertes.append({"prenom": prenom, "texte":
                            f"{_fmt_h(total_sem)} sur la semaine (max 48h)"})
        # Repos quotidien < 11 h (fin de la veille → début du jour)
        for k in range(7):
            if not jours[k - 1] or not jours[k]:
                continue
            repos = (24 * 60 - jours[k - 1][-1][1]) + jours[k][0][0]
            if repos < REPOS_QUOTIDIEN_MIN:
                d = lundi + timedelta(days=k)
                alertes.append({"prenom": prenom, "texte":
                                f"repos de {_fmt_h(repos)} seulement avant "
                                f"{JOURS_ABBR[d.isoweekday()]} {d.strftime('%d/%m')} (min 11h)"})
        # Plus de 6 jours travaillés d'affilée (série touchant la semaine affichée ;
        # la fenêtre de 6 jours arrière garantit qu'une série atteint 7 au plus
        # tôt le lundi affiché → une seule alerte par série)
        serie = 0
        for k in range(-6, 7):
            serie = serie + 1 if jours[k] else 0
            if serie == JOURS_CONSECUTIFS_MAX + 1 and k >= 0:
                d = lundi + timedelta(days=k)
                alertes.append({"prenom": prenom, "texte":
                                f"7e jour travaillé d'affilée le {JOURS_ABBR[d.isoweekday()]} "
                                f"{d.strftime('%d/%m')} (max 6 jours consécutifs)"})
    return alertes


# --- Effectifs minimums (contrôle de couverture par créneau) -----------------
EFFECTIFS_FILE = os.path.join(BASE_DIR, "planning_effectifs.json")
EFFECTIFS_DEFAUT = {"min_total": 2, "min_pharmaciens": 1}


def charger_effectifs():
    o = dict(EFFECTIFS_DEFAUT)
    d = _lire_json(EFFECTIFS_FILE)
    if isinstance(d, dict):
        o.update({k: v for k, v in d.items() if isinstance(v, int) and v >= 0})
    return o


# --- Congés payés (droits par collaborateur, posés comptés du planning) ------
CONGES_FILE = os.path.join(BASE_DIR, "planning_conges.json")
CONGES_DROIT_DEFAUT = 30      # jours OUVRABLES (lun-sam), règle légale française


def charger_conges():
    d = _lire_json(CONGES_FILE)
    return d if isinstance(d, dict) else {}


def periode_conges(ref=None):
    """Période de référence CP française : 1er juin N → 31 mai N+1."""
    ref = ref or date.today()
    an = ref.year if ref.month >= 6 else ref.year - 1
    return date(an, 6, 1), date(an + 1, 5, 31)


def _jours_ouvrables_cp(d1, d2, p1, p2):
    """Jours ouvrables (lun-sam, hors fériés) de [d1..d2] ∩ [p1..p2]."""
    n, d = 0, max(d1, p1)
    fin = min(d2, p2)
    while d <= fin:
        if d.isoweekday() <= 6 and not ferie_de(d):
            n += 1
        d += timedelta(days=1)
    return n


def bilan_cp(em, absences, changements, conges, p1, p2):
    """Bilan congés payés d'un collaborateur sur la période [p1..p2] :
    droit, report, posés (absences + ponctuels « Congés payés » du planning,
    en jours ouvrables), restant et détail des plages."""
    cf = conges.get(em) if isinstance(conges.get(em), dict) else {}
    droit = cf.get("droit", CONGES_DROIT_DEFAUT)
    report = cf.get("report", 0)
    poses, detail, plages = 0, [], []
    for a in absences:
        if a.get("email") != em or a.get("motif") != "Congés payés":
            continue
        try:
            d1 = datetime.strptime(a.get("debut", ""), "%Y-%m-%d").date()
            d2 = datetime.strptime(a.get("fin", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d2 < p1 or d1 > p2:
            continue
        n = _jours_ouvrables_cp(d1, d2, p1, p2)
        if n:
            poses += n
            plages.append((d1, d2))
            detail.append((d1, f"Du {d1.strftime('%d/%m/%y')} au {d2.strftime('%d/%m/%y')} : {n} j"))
    # Jours ponctuels « Congés payés » (jour vidé), hors plages déjà comptées.
    for diso, parem in changements.items():
        ch = (parem or {}).get(em)
        if not ch or ch.get("motif") != "Congés payés" or (ch.get("creneaux") or []):
            continue
        try:
            d0 = datetime.strptime(diso, "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (p1 <= d0 <= p2) or d0.isoweekday() > 6 or ferie_de(d0):
            continue
        if any(a1 <= d0 <= a2 for a1, a2 in plages):
            continue
        poses += 1
        detail.append((d0, f"Le {d0.strftime('%d/%m/%y')} : 1 j"))
    return {"droit": droit, "report": report, "poses": poses,
            "restant": round(droit + report - poses, 1),
            "detail": [t for _, t in sorted(detail)]}


# --- Demandes de congés des employés (depuis Mon espace) ---------------------
DEMANDES_CP_FILE = os.path.join(BASE_DIR, "planning_demandes_conges.json")
STATUTS_CP = {"en_attente": "En attente", "acceptee": "Acceptée",
              "refusee": "Refusée", "annulee": "Annulée"}


def charger_demandes_cp():
    """[{id, email, debut, fin, commentaire, statut, demande_le, traite_le…}].
    Statuts : en_attente / acceptee / refusee / annulee."""
    d = _lire_json(DEMANDES_CP_FILE, [])
    return d if isinstance(d, list) else []


def sauvegarder_demandes_cp(lst):
    _ecrire_json(DEMANDES_CP_FILE, lst)


# --- Demandes de la pharmacie (admin → collaborateur, notification in-app) ----
# La pharmacie propose des congés ou demande des heures supplémentaires ; le
# collaborateur voit la demande dans son Mon espace (aucun e-mail) et répond.
DEMANDES_ADMIN_FILE = os.path.join(BASE_DIR, "planning_demandes_admin.json")
TYPES_DEMANDE_ADMIN = {"conges": "Congés", "heures_sup": "Heures supplémentaires"}
STATUTS_DEMANDE_ADMIN = {"en_attente": "En attente de réponse", "acceptee": "Acceptée",
                         "refusee": "Refusée", "annulee": "Annulée"}


def charger_demandes_admin():
    """[{id, email, type (conges|heures_sup), debut, fin, h_debut, h_fin,
    commentaire, statut, cree_le, traite_le, reponse, lu_admin}]."""
    d = _lire_json(DEMANDES_ADMIN_FILE, [])
    return d if isinstance(d, list) else []


def sauvegarder_demandes_admin(lst):
    _ecrire_json(DEMANDES_ADMIN_FILE, lst)


def _quand_demande_admin(dm):
    """Libellé de la période demandée : « du 12/08 au 16/08/2026 » (congés) ou
    « le 12/08/2026 de 18:00 à 20:00 » (heures supplémentaires)."""
    try:
        if dm.get("type") == "heures_sup":
            d = datetime.strptime(dm.get("debut", ""), "%Y-%m-%d").date()
            return f"le {d.strftime('%d/%m/%Y')} de {dm.get('h_debut', '')} à {dm.get('h_fin', '')}"
        d1 = datetime.strptime(dm.get("debut", ""), "%Y-%m-%d").date()
        d2 = datetime.strptime(dm.get("fin", ""), "%Y-%m-%d").date()
        return f"du {d1.strftime('%d/%m')} au {d2.strftime('%d/%m/%Y')}"
    except ValueError:
        return dm.get("debut", "")


def _cle_creneaux(creneaux):
    """Signature normalisée d'une liste de créneaux (ordre indifférent), pour comparer."""
    return sorted((c.get("debut", ""), c.get("fin", "")) for c in (creneaux or [])
                  if c.get("debut") and c.get("fin"))


def meme_que_trame(creneaux_chg, creneaux_trame):
    """True si le changement a les mêmes horaires que la trame → pas une vraie modif
    (cas du « ↺ Rétablir les horaires » : le jour est revenu à la normale)."""
    return _cle_creneaux(creneaux_chg) == _cle_creneaux(creneaux_trame)


def creneaux_trame_jour(trame, email, date_obj):
    """Créneaux de la trame pour un collaborateur à une date réelle (gère la rotation)."""
    if not trame:
        return []
    rot = semaine_rotation(trame, date_obj)
    return _jours_sem(trame, email, rot).get(str(date_obj.isoweekday()), []) or []


def _nouvelle_trame(commentaire=""):
    """Crée une trame vierge (non activée par défaut)."""
    t = {"id": uuid.uuid4().hex[:8], "activee": False, "commentaire": commentaire,
         "cree_le": datetime.now().strftime("%d/%m/%Y")}
    for k, v in TRAME_DEFAUT.items():
        t[k] = dict(v) if isinstance(v, dict) else v
    return t


def _migrer(data):
    """Ancien format (trame unique à plat) -> {trames:[...]}. Idempotent."""
    if isinstance(data, dict) and "trames" in data:
        return data
    if isinstance(data, dict) and (data.get("employes") or data.get("date_demarrage")):
        t = dict(data)
        t.setdefault("id", uuid.uuid4().hex[:8])
        t.setdefault("activee", True)
        t.setdefault("commentaire", "Trame initiale")
        for k, v in TRAME_DEFAUT.items():
            t.setdefault(k, dict(v) if isinstance(v, dict) else v)
        return {"trames": [t]}
    return {"trames": []}


def charger_trames():
    raw = _lire_json(TRAME_FILE) or {}
    if isinstance(raw, dict) and "trames" in raw:
        return raw
    data = _migrer(raw)
    if data.get("trames"):           # migration d'un ancien format -> on la persiste
        sauvegarder_trames(data)     # (1 seule fois) pour stabiliser les id
    return data


def sauvegarder_trames(data):
    _ecrire_json(TRAME_FILE, data)


def trame_par_id(data, tid):
    for t in data.get("trames", []):
        if t.get("id") == tid:
            return t
    return None


def trame_selectionnee(data, tid=None):
    """Trame à afficher : celle demandée (tid), sinon la 1re activée, sinon la 1re."""
    trames = data.get("trames", [])
    if tid:
        t = trame_par_id(data, tid)
        if t:
            return t
    act = trame_active_pour(data, date.today())   # trame en vigueur aujourd'hui
    if act:
        return act
    return trames[0] if trames else None


def _label_trame(t):
    base = t.get("date_demarrage") or t.get("cree_le") or "sans date"
    com = (t.get("commentaire") or "").strip()
    actif = " — Activée" if t.get("activee") else " — désactivée"
    return f"{base}{(' · ' + com) if com else ''}{actif}"


def _debut_trame(t):
    """Lundi de la date de démarrage de la trame (date.min si absente)."""
    try:
        return _lundi(datetime.strptime(t.get("date_demarrage", ""), "%Y-%m-%d").date())
    except (ValueError, TypeError):
        return date.min


def trame_active_pour(data, jour):
    """RÈGLE MÉTIER : les trames ACTIVÉES se succèdent dans le temps et servent à
    calculer le planning semaine après semaine — pour la semaine du jour donné,
    on prend la trame activée dont la date de démarrage (ramenée au lundi) est la
    plus récente ≤ ce lundi. Les trames non activées (brouillons) sont ignorées.
    Les anciennes trames activées doivent être CONSERVÉES : sans elles,
    l'historique des plannings serait perdu."""
    lundi = _lundi(jour)
    actives = sorted((t for t in data.get("trames", []) if t.get("activee")),
                     key=_debut_trame)
    en_vigueur = [t for t in actives if _debut_trame(t) <= lundi]
    # Avant le démarrage de la 1re trame activée : PAS de trame → planning vide.
    return en_vigueur[-1] if en_vigueur else None


def couleurs_map(employes_base, profils):
    """{email: couleur} stable (index sur l'ordre d'origine, pas l'ordre d'affichage)."""
    return {e["email"]: couleur_collaborateur(profils.get(e["email"], {}), i)
            for i, e in enumerate(employes_base)}


def membres_semaine(trame, employes_tous, profils, lundi):
    """Membres de la trame à AFFICHER pour la semaine donnée. RÈGLE MÉTIER :
    les collaborateurs archivés (ou inactifs) ne sont pas proposés dans les
    trames ni sur le planning courant/futur, MAIS ils restent nécessaires pour
    conserver l'historique — une semaine PASSÉE affiche tous les membres de la
    trame de l'époque, quel que soit leur statut actuel."""
    if lundi is not None and lundi < _lundi(date.today()):
        base = employes_tous
    else:
        base = [e for e in employes_tous
                if collaborateur_actif(profils.get(e["email"], {}))]
    return membres_ordonnes(trame, base)


def membres_ordonnes(trame, employes_base):
    """Emails des collaborateurs SUR la trame, dans l'ordre d'affichage.
    Migration : si la trame n'a pas de clé 'membres', défaut = ceux qui ont déjà des
    heures saisies (sinon liste vide → trame neuve, on ajoute via « Ajouter un
    collaborateur »)."""
    base = [e["email"] for e in employes_base]
    if not trame:
        return []
    m = trame.get("membres")
    if m is None:
        m = [em for em in base
             if any(_jours_sem(trame, em, s).get(str(j))
                    for s in SEMAINES for j in range(1, 8))]
    return [em for em in m if em in base]


# --- Calcul des heures ------------------------------------------------------

def _minutes(hhmm):
    try:
        h, m = str(hhmm).split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h * 60 + m
    if h == 24 and m == 0:
        return 1440
    return None


def _hhmm(minutes):
    minutes = int(round(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _norm_hhmm(s):
    """Normalise une heure tapée librement -> « HH:MM » (ou « » si vide/invalide).
    Accepte « 9:00 », « 09:00 », « 9h », « 9h30 », « 900 », « 0900 », « 9 »."""
    s = (s or "").strip().replace("h", ":").replace(".", ":")
    if not s:
        return ""
    if ":" in s:
        a, _, b = s.partition(":")
        hh = "".join(ch for ch in a if ch.isdigit())
        mm = "".join(ch for ch in b if ch.isdigit())
    else:
        d = "".join(ch for ch in s if ch.isdigit())
        hh, mm = (d[:-2], d[-2:]) if len(d) >= 3 else (d, "00")
    if not hh:
        return ""
    try:
        h, m = int(hh), int(mm or 0)
    except ValueError:
        return ""
    return f"{h:02d}:{m:02d}" if 0 <= h <= 23 and 0 <= m <= 59 else ""


def creneau_valide(c):
    d, f = _minutes(c.get("debut")), _minutes(c.get("fin"))
    return d is not None and f is not None and f > d


def duree_creneau(c):
    d, f = _minutes(c.get("debut")), _minutes(c.get("fin"))
    if d is None or f is None or f <= d:
        return 0.0
    return round((f - d) / 60.0, 2)


def total_jour(creneaux):
    return round(sum(duree_creneau(c) for c in (creneaux or [])), 2)


def total_semaine(jours_dict):
    return round(sum(total_jour(jours_dict.get(str(j), [])) for j in range(1, 8)), 2)


def _fmt_hmin(heures):
    """8.5 → « 8h30min » (format des totaux du tableau)."""
    m = int(round(heures * 60))
    return f"{m // 60}h{m % 60:02d}min"


def _jours_sem(trame, email, sem):
    return (trame.get("employes", {}).get(email, {}) or {}).get(sem, {}) or {}


def _creneaux_txt(trame, email, sem, j):
    """Horaires d'un jour en texte : « 09:00–13:00, 14:00–19:00 » (ou « repos »)."""
    cr = [c for c in _jours_sem(trame, email, sem).get(str(j), []) if creneau_valide(c)]
    return ", ".join(f"{c['debut']}–{c['fin']}" for c in cr) or "repos"


def _paques(an):
    """Date du dimanche de Pâques (algorithme de Butcher)."""
    a, b, c = an % 19, an // 100, an % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(an, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)


def jours_feries(an):
    """Jours fériés français de l'année : {date: libellé}."""
    p = _paques(an)
    return {
        date(an, 1, 1): "Jour de l'an",
        p + timedelta(days=1): "Lundi de Pâques",
        date(an, 5, 1): "Fête du Travail",
        date(an, 5, 8): "Victoire 1945",
        p + timedelta(days=39): "Ascension",
        p + timedelta(days=50): "Lundi de Pentecôte",
        date(an, 7, 14): "Fête nationale",
        date(an, 8, 15): "Assomption",
        date(an, 11, 1): "Toussaint",
        date(an, 11, 11): "Armistice 1918",
        date(an, 12, 25): "Noël",
    }


def ferie_de(d):
    """Libellé du jour férié couvrant cette date (ou None)."""
    return jours_feries(d.year).get(d)


def _lundi(d):
    """Lundi de la semaine contenant la date d."""
    return d - timedelta(days=d.weekday())


def _ajoute_mois(d, k):
    """Premier jour du mois décalé de k mois par rapport à d."""
    m = d.month - 1 + k
    return date(d.year + m // 12, m % 12 + 1, 1)


def semaine_rotation(trame, lundi):
    """Lettre de rotation (A/B/…) de la semaine `lundi`, selon la trame active
    (date de démarrage + nombre de semaines tournantes + semaine de démarrage)."""
    if not trame:
        return SEMAINES[0]
    try:
        dem = datetime.strptime(trame.get("date_demarrage", ""), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        sd = trame.get("semaine_demarrage", SEMAINES[0])
        return sd if sd in SEMAINES else SEMAINES[0]
    nb = max(1, int(trame.get("nb_semaines") or len(SEMAINES)))
    start = SEMAINES.index(trame["semaine_demarrage"]) if trame.get("semaine_demarrage") in SEMAINES else 0
    semaines_ecoulees = (_lundi(lundi) - _lundi(dem)).days // 7
    idx = (start + semaines_ecoulees) % nb
    return SEMAINES[idx] if 0 <= idx < len(SEMAINES) else SEMAINES[0]


# --- Amplitude / frise ------------------------------------------------------

def _amplitude(horaires):
    """(min, max) minutes de l'amplitude affichée, depuis les heures d'ouverture.
    Repli 08:00–20:00 si rien."""
    debs, fins = [], []
    for plages in (horaires or {}).values():
        for p in plages or []:
            d, f = _minutes(p[0]), _minutes(p[1])
            if d is not None:
                debs.append(d)
            if f is not None:
                fins.append(f)
    amp_min = min(debs) if debs else 8 * 60
    amp_max = max(fins) if fins else 20 * 60
    amp_min = (amp_min // 60) * 60            # arrondi à l'heure pleine inférieure
    amp_max = -(-amp_max // 60) * 60          # arrondi à l'heure pleine supérieure
    if amp_max <= amp_min:
        amp_min, amp_max = 8 * 60, 20 * 60
    return amp_min, amp_max


def _pos(deb, fin, amp_min, span):
    """left% / width% d'une barre dans l'amplitude."""
    left = max(0.0, (deb - amp_min) / span * 100)
    width = max(0.5, (fin - deb) / span * 100)
    return round(left, 2), round(min(width, 100 - left), 2)


def _pad2(creneaux):
    """Liste de 2 créneaux (paddée vide) pour les champs de la modale."""
    cr = [{"debut": c.get("debut", ""), "fin": c.get("fin", "")} for c in (creneaux or [])]
    while len(cr) < 2:
        cr.append({"debut": "", "fin": ""})
    return cr[:2]


def _assombrir(couleur, k=0.42):
    """Version foncée d'une couleur #RRGGBB (k = part de luminosité conservée).
    Marque les heures AJOUTÉES par rapport à la trame sur la grille."""
    try:
        h = (couleur or "").lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return "#%02x%02x%02x" % (int(r * k), int(g * k), int(b * k))
    except (ValueError, IndexError):
        return "#333333"


def _eclaircir(couleur, t=0.8):
    """Version très claire d'une couleur #RRGGBB (t = part de blanc mélangée).
    Marque les heures EN MOINS (prévues à la trame mais non travaillées)."""
    try:
        h = (couleur or "").lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return "#%02x%02x%02x" % (int(r + (255 - r) * t), int(g + (255 - g) * t),
                                  int(b + (255 - b) * t))
    except (ValueError, IndexError):
        return "#e8e8e8"


def _frise(trame, sem, employes, couleurs, jours_affiches=None, montrer_horaires=True,
           lundi_date=None, changements=None, absences=None, masquer_vides=False,
           masquer_fermes=False):
    horaires = trame.get("horaires_ouverture", HORAIRES_DEFAUT)
    amp_min, amp_max = _amplitude(horaires)
    span = amp_max - amp_min
    ticks = [{"label": f"{h}h", "left": round((h * 60 - amp_min) / span * 100, 2)}
             for h in range(amp_min // 60, amp_max // 60 + 1)]
    jours = []
    for j in range(1, 8):
        if jours_affiches is not None and j not in jours_affiches:
            continue
        date_reelle = (lundi_date + timedelta(days=j - 1)) if lundi_date else None
        date_iso = date_reelle.isoformat() if date_reelle else ""
        ferie = ferie_de(date_reelle) if date_reelle else None
        ouv = []
        for p in horaires.get(str(j), []) or []:
            d, f = _minutes(p[0]), _minutes(p[1])
            if d is not None and f is not None and f > d:
                left, width = _pos(d, f, amp_min, span)
                ouv.append({"left": left, "width": width})
        lignes = []
        for e in employes:
            couleur = couleurs.get(e["email"], "#888")
            cr_trame = _jours_sem(trame, e["email"], sem).get(str(j), []) or []
            # Changement ponctuel pour cette date réelle ? -> surcharge la trame.
            chg = changement_de(changements or {}, date_iso, e["email"]) if date_iso else None
            # Un changement rétabli à la trame (mêmes horaires) n'est plus une modif.
            # SAUF un jour férié : là il sert à noter la personne présente, on le garde.
            if (chg is not None and not ferie and chg.get("creneaux")
                    and meme_que_trame(chg.get("creneaux"), cr_trame)):
                chg = None
            # Absence prolongée couvrant ce jour (le changement ponctuel reste prioritaire).
            abs_a = absence_active(absences or [], e["email"],
                                   lundi_date + timedelta(days=j - 1)) if (date_iso and chg is None) else None
            if chg is not None:
                cr_eff = chg.get("creneaux", []) or []
                motif = chg.get("motif", "")
                # Présent un férié avec ses horaires de trame = il était censé
                # travailler : ce n'est PAS une modification.
                modifie = not (ferie and meme_que_trame(cr_eff, cr_trame))
                if not modifie:
                    motif = ""
            elif abs_a is not None:
                cr_eff, motif, modifie = [], abs_a.get("motif", "Absence"), True
            elif ferie:
                # Jour férié : personne ne travaille par défaut, seul un
                # changement ponctuel (garde…) rend présent.
                cr_eff, motif, modifie = [], ferie, False
            else:
                cr_eff, motif, modifie = cr_trame, "", False
            barres = []
            eff_iv = [(_minutes(c["debut"]), _minutes(c["fin"]), c)
                      for c in cr_eff if creneau_valide(c)]
            trame_iv = [(_minutes(c["debut"]), _minutes(c["fin"]))
                        for c in cr_trame if creneau_valide(c)]
            if modifie and eff_iv:
                # Jour modifié avec présence : la bande marque la DIFFÉRENCE avec
                # la trame — heures en plus en FONCÉ, heures en moins (prévues mais
                # non faites) en TRÈS CLAIR pointillé, le reste en couleur normale.
                # Les horaires complets du créneau ne s'affichent qu'une fois, au
                # début de la bande travaillée.
                pts = sorted({x for iv in eff_iv for x in iv[:2]}
                             | {x for iv in trame_iv for x in iv})
                segs = []
                for a, b in zip(pts, pts[1:]):
                    mid = (a + b) / 2
                    in_eff = any(d <= mid < f for d, f, _ in eff_iv)
                    in_tr = any(t1 <= mid < t2 for t1, t2 in trame_iv)
                    if in_eff and in_tr:
                        typ = "normal"
                    elif in_eff:
                        typ = "ajout"
                    elif in_tr:
                        typ = "retrait"
                    else:
                        continue
                    if segs and segs[-1]["typ"] == typ and segs[-1]["fin"] == a:
                        segs[-1]["fin"] = b
                    else:
                        segs.append({"deb": a, "fin": b, "typ": typ})
                labels = {d: f"{c['debut']}–{c['fin']}" for d, f, c in eff_iv}
                for s in segs:
                    left, width = _pos(s["deb"], s["fin"], amp_min, span)
                    lab = ""
                    if montrer_horaires and s["typ"] != "retrait" and s["deb"] in labels:
                        lab = labels[s["deb"]]
                    barres.append({
                        "left": left, "width": width, "label": lab,
                        "deborde": bool(lab),
                        "couleur": (couleur if s["typ"] == "normal"
                                    else _assombrir(couleur, 0.55) if s["typ"] == "ajout"
                                    else _eclaircir(couleur)),
                        "bordure": couleur if s["typ"] == "retrait" else ""})
            else:
                for d, f, c in eff_iv:
                    left, width = _pos(d, f, amp_min, span)
                    barres.append({"left": left, "width": width, "couleur": couleur,
                                   "label": (f"{c['debut']}–{c['fin']}" if montrer_horaires else "")})
            # Option « Lignes vides : masquer » = ne montrer que les présents du jour
            # (repos, absence ou jour vidé par un changement → ligne retirée).
            if masquer_vides and not barres:
                continue
            # Losange typé selon la nature du changement (cf. légende sous la grille).
            marque, marque_titre = "", ""
            if modifie:
                a_eff = any(creneau_valide(c) for c in cr_eff)
                a_tr = any(creneau_valide(c) for c in cr_trame)
                if a_eff and a_tr:
                    marque, marque_titre = "orange", "Présent avec des heures différentes de la trame"
                elif a_eff:
                    marque, marque_titre = "bleu", "Présent alors que non prévu dans la trame"
                elif a_tr:
                    marque, marque_titre = "rouge", "Absent alors que prévu dans la trame"
                else:
                    marque, marque_titre = "gris", "Absence sur un jour non prévu dans la trame"
                if motif:
                    marque_titre += f" — {motif}"
            lignes.append({"prenom": e["prenom"], "email": e["email"], "couleur": couleur,
                           "barres": barres, "total": total_jour(cr_eff),
                           "total_txt": _fmt_hmin(total_jour(cr_eff)),
                           "modifie": modifie, "motif": motif,
                           "marque": marque, "marque_titre": marque_titre,
                           "creneaux": _pad2(cr_eff), "creneaux_trame": _pad2(cr_trame)})
        ferme = not horaires.get(str(j))
        # Jour de fermeture (ex. dimanche) : masqué du planning, SAUF si une
        # modification y met quelqu'un de présent (garde, inventaire…).
        if masquer_fermes and ferme and not ferie and not any(l["barres"] for l in lignes):
            continue
        # Jour férié : la ligne du jour reste visible, mais sans collaborateur
        # (seuls les présents notés par un changement apparaissent).
        if ferie:
            lignes = [l for l in lignes if l["barres"]]
        nom = JOURS_NOMS[j]
        if lundi_date:
            nom += " " + (lundi_date + timedelta(days=j - 1)).strftime("%d/%m")
        jours.append({"iso": j, "nom": nom, "date_iso": date_iso, "ouverture": ouv,
                      "lignes": lignes, "ferme": ferme, "ferie": ferie,
                      "aujourdhui": bool(date_reelle) and date_reelle == jour_courant()})
    return {"ticks": ticks, "jours": jours}


def _frise_solo(trame, sem, email, couleur):
    """Frise (Gantt) d'UN collaborateur sur une semaine : jours en lignes."""
    horaires = trame.get("horaires_ouverture", HORAIRES_DEFAUT)
    amp_min, amp_max = _amplitude(horaires)
    span = amp_max - amp_min
    ticks = [{"label": f"{h}h", "left": round((h * 60 - amp_min) / span * 100, 2)}
             for h in range(amp_min // 60, amp_max // 60 + 1)]
    jours = []
    for j in range(1, 8):
        ouv = []
        for p in horaires.get(str(j), []) or []:
            d, f = _minutes(p[0]), _minutes(p[1])
            if d is not None and f is not None and f > d:
                left, width = _pos(d, f, amp_min, span)
                ouv.append({"left": left, "width": width})
        barres = []
        for c in _jours_sem(trame, email, sem).get(str(j), []) or []:
            if creneau_valide(c):
                d, f = _minutes(c["debut"]), _minutes(c["fin"])
                left, width = _pos(d, f, amp_min, span)
                barres.append({"left": left, "width": width, "couleur": couleur,
                               "label": f"{c['debut']}–{c['fin']}"})
        jours.append({"nom": JOURS_NOMS[j], "ouverture": ouv, "barres": barres,
                      "ferme": not horaires.get(str(j)),
                      "total": total_jour(_jours_sem(trame, email, sem).get(str(j), []))})
    return {"ticks": ticks, "jours": jours}


# --- Routes -----------------------------------------------------------------

ONGLETS = [("equipe", "Équipe"), ("trame", "Trame"), ("planning", "Planning"),
           ("changements", "Changements"), ("conges", "Congés"),
           ("demandes", "Mes demandes"), ("totaux", "Totaux / Fin de mois")]
# Effectifs & Options : sous-onglets internes de « Planning ».
SOUS_PLANNING = [("planning", "Planning"), ("effectifs", "Effectifs"), ("options", "Options")]

FONCTIONS = POSTES   # référentiel unique : le champ `poste` du dossier salarié
PERMISSIONS = ["Pas d'accès", "Consultation", "Gestionnaire", "Administrateur"]
VUES_PLANNING = ["Le planning de tous", "Son planning personnel"]


@bp.route("/admin/planning-equipe")
def vue():
    if not _admin():
        return redirect(url_for("admin"))
    onglet = request.args.get("onglet", "planning")
    sem = request.args.get("sem", "A")
    if sem not in SEMAINES:
        sem = "A"
    data = charger_trames()
    trame = trame_selectionnee(data, request.args.get("trame"))
    tid = trame.get("id") if trame else None
    profils = charger_profils()
    employes_tous = charger_employes()
    couleurs = couleurs_map(employes_tous, profils)        # couleurs stables (liste complète)
    # Seuls les collaborateurs ACTIFS sont PROPOSÉS (trames, semaines courantes).
    # Les archivés/inactifs restent affichés sur les semaines PASSÉES (historique),
    # cf. membres_semaine(). Leurs heures de trame sont conservées.
    employes_base = [e for e in employes_tous
                     if collaborateur_actif(profils.get(e["email"], {}))]
    emap = {e["email"]: e for e in employes_tous}          # noms résolus, même archivés

    liste_trames = [{"id": t.get("id"), "label": _label_trame(t), "activee": t.get("activee")}
                    for t in data.get("trames", [])]

    # « Planning » reste l'onglet principal actif quand on est sur ses sous-onglets.
    onglet_principal = "planning" if onglet in ("effectifs", "options") else onglet
    ctx = dict(onglets=ONGLETS, sous_planning=SOUS_PLANNING, onglet=onglet,
               onglet_principal=onglet_principal, sem=sem, semaines=SEMAINES,
               trame=trame, tid=tid, liste_trames=liste_trames,
               msg=request.args.get("msg", ""))

    # Écran dédié « Horaires » d'UN collaborateur (toutes les semaines de rotation).
    he = request.args.get("horaires", "")
    if onglet == "trame" and he and trame:
        emp = next((e for e in employes_base if e["email"] == he), None)
        if emp:
            weeks = []
            for s in SEMAINES:
                jrs = []
                for j in range(1, 8):
                    cr = list(_jours_sem(trame, he, s).get(str(j), []) or [])
                    while len(cr) < 2:
                        cr.append({"debut": "", "fin": ""})
                    jrs.append({"iso": j, "nom": JOURS_NOMS[j], "creneaux": cr[:2]})
                weeks.append({"sem": s, "jours": jrs,
                              "total": total_semaine(_jours_sem(trame, he, s))})
            memb = membres_ordonnes(trame, employes_base)
            tous = [{"email": em, "prenom": emap[em]["prenom"], "nom": emap[em]["nom"]}
                    for em in memb if em in emap]
            return render_template("planning_horaires_collab.html",
                                   emp=emp, couleur=couleurs[he], weeks=weeks, trame=trame,
                                   tid=tid, tous=tous, msg=request.args.get("msg", ""))

    if onglet == "trame":
        memb = membres_ordonnes(trame, employes_base)
        lignes = []
        for em in memb:
            e = emap[em]
            jours = {}
            for j in range(1, 8):
                cr = list(_jours_sem(trame, em, sem).get(str(j), []) or [])
                while len(cr) < 2:
                    cr.append({"debut": "", "fin": ""})
                jours[j] = cr[:2]
            moy = round(sum(total_semaine(_jours_sem(trame, em, s))
                            for s in SEMAINES) / len(SEMAINES), 1)
            lignes.append({"email": em, "prenom": e["prenom"], "nom": e["nom"],
                           "couleur": couleurs[em], "jours": jours,
                           "total": total_semaine(_jours_sem(trame, em, sem)), "moyenne": moy})
        # Candidats à ajouter : ont une fiche mais ne sont pas (encore) sur la trame.
        membset = set(memb)
        hors = [{"email": e["email"], "prenom": e["prenom"], "nom": e["nom"],
                 "couleur": couleurs[e["email"]]}
                for e in employes_base if e["email"] not in membset]
        # Horaires d'ouverture : 2 plages paddées par jour pour l'édition.
        ouverture = {}
        ho = trame.get("horaires_ouverture", {}) if trame else {}
        for j in range(1, 8):
            rg = list(ho.get(str(j), []) or [])
            while len(rg) < 2:
                rg.append(["", ""])
            ouverture[j] = rg[:2]
        # Aperçu en frise (collaborateurs sur la trame uniquement).
        emp_inclus = [emap[em] for em in memb]
        ctx["frise"] = _frise(trame, sem, emp_inclus, couleurs) if (trame and memb) else None
        ctx.update(lignes=lignes, jours_noms=JOURS_NOMS, ouverture=ouverture, hors_trame=hors)
        return render_template("planning_equipe.html", **ctx)

    if onglet == "planning":
        opts = charger_options()
        changements = charger_changements()
        absences = charger_absences()
        masques = set(opts.get("collaborateurs_masques", []))
        jours_aff = [j for j in range(1, 8)
                     if str(j) in opts.get("jours", []) or not opts.get("jours")]
        montrer_h = opts.get("horaires_grille") != "masquer"
        mode = opts.get("mode", "grille")
        periode = opts.get("periode", "hebdo")
        # Date de référence pour la navigation réelle.
        try:
            ref = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            ref = jour_courant()   # à 00h30, rester sur la semaine de la veille
        # Le planning suit la trame ACTIVÉE en vigueur pour la semaine consultée
        # (les trames activées se succèdent : l'historique garde ses anciennes trames).
        act = trame_active_pour(data, ref)
        emp_base = [emap[em] for em in membres_semaine(act, employes_tous, profils, _lundi(ref))
                    if em not in masques]
        # Lundis à afficher selon la période.
        if periode == "hebdo":
            lundis = [_lundi(ref)]
        else:                                            # mensuel / période = semaines du mois
            dernier = ref.replace(day=calendar.monthrange(ref.year, ref.month)[1])
            L, lundis = _lundi(ref.replace(day=1)), []
            while L <= dernier:
                lundis.append(L)
                L += timedelta(days=7)
        # Barre de navigation (semaines cliquables en Hebdo, mois en Mensuel).
        nav = {"type": "semaine" if periode == "hebdo" else "mois", "boutons": [],
               "aujourdhui": url_for(".vue", onglet="planning", date=jour_courant().isoformat()) + "#auj"}
        if periode == "hebdo":
            cur = _lundi(ref)
            for k in range(-2, 7):
                L = cur + timedelta(days=7 * k)
                tr_l = trame_active_pour(data, L)
                nav["boutons"].append({"url": url_for(".vue", onglet="planning", date=L.isoformat()),
                                     "label": L.strftime("%d/%m"),
                                     "sub": ("Sem. " + semaine_rotation(tr_l, L)) if tr_l else "",
                                     "actif": L == cur})
        else:
            prem = ref.replace(day=1)
            for k in range(-3, 12):
                m = _ajoute_mois(prem, k)
                nav["boutons"].append({"url": url_for(".vue", onglet="planning", date=m.isoformat()),
                                     "label": f"{MOIS_ABBR[m.month]} {m.year % 100:02d}", "sub": "",
                                     "actif": (m.year, m.month) == (ref.year, ref.month)})
        # Vues (1 par semaine) : chaque semaine utilise la trame activée EN VIGUEUR
        # à sa date (succession des trames), rotation A/B calculée depuis la date.
        vues = []
        for lundi in lundis:
            act_l = trame_active_pour(data, lundi)
            if act_l:
                rot = semaine_rotation(act_l, lundi)
                emp_sm = [emap[em] for em in membres_semaine(act_l, employes_tous, profils, lundi)
                          if em not in masques]
                # Remplaçants : un collaborateur HORS trame qui a des horaires
                # ponctuels cette semaine s'affiche aussi (créé au préalable et
                # non archivé pour les semaines courantes/futures).
                vus = {e["email"] for e in emp_sm}
                passee = lundi < _lundi(date.today())
                for k in range(7):
                    for em2, ch in (changements.get((lundi + timedelta(days=k)).isoformat(), {}) or {}).items():
                        if (em2 in vus or em2 in masques or em2 not in emap
                                or not (ch.get("creneaux") or [])):
                            continue
                        if passee or collaborateur_actif(profils.get(em2, {})):
                            emp_sm.append(emap[em2])
                            vus.add(em2)
                if opts.get("lignes_vides") == "masquer":
                    emp_sm = [e for e in emp_sm if total_semaine(_jours_sem(act_l, e["email"], rot)) > 0]
                fin = lundi + timedelta(days=6)
                titre = f"{lundi.strftime('%d/%m')} – {fin.strftime('%d/%m/%Y')} · Semaine {rot}"
                # Compteur hebdo par collaborateur : heures EFFECTIVES de la
                # semaine (trame + ponctuels + absences + fériés) vs heures
                # contractuelles de la fiche (moyenne en cas de rotation A/B).
                compteurs = []
                for e in emp_sm:
                    eff = 0
                    for k in range(7):
                        for c in creneaux_effectifs_jour(act_l, e["email"],
                                                         lundi + timedelta(days=k),
                                                         changements, absences):
                            a, b = _minutes(c.get("debut")), _minutes(c.get("fin"))
                            if a is not None and b is not None and b > a:
                                eff += b - a
                    contrat = round(_heures_hebdo(profils.get(e["email"], {})
                                                  .get("heures_contractuelles_hebdo", "")) * 60)
                    compteurs.append({"prenom": e["prenom"],
                                      "couleur": couleurs.get(e["email"], "#888"),
                                      "eff": _fmt_h(eff),
                                      "contrat": _fmt_h(contrat) if contrat else "",
                                      "sur": bool(contrat) and eff > contrat})
                v = {"sem": rot, "titre": titre, "lundi": lundi.isoformat(),
                     "compteurs": compteurs,
                     "conformite": alertes_conformite(data, emp_sm, lundi,
                                                      changements, absences)}
                if mode == "grille":
                    v["frise"] = _frise(act_l, rot, emp_sm, couleurs, set(jours_aff), montrer_h,
                                        lundi, changements, absences,
                                        masquer_vides=opts.get("lignes_vides") == "masquer",
                                        masquer_fermes=True)
                elif mode == "texte":
                    # Vue texte groupée par JOUR : seuls les présents du jour, avec
                    # leurs horaires effectifs (ponctuels + absences appliqués).
                    v["titre"] = f"Semaine {lundi.isocalendar()[1]} ({rot})"
                    tj = []
                    for j in jours_aff:
                        d = lundi + timedelta(days=j - 1)
                        fer = ferie_de(d)
                        lignes_j = []
                        for e in emp_sm:
                            chg = changement_de(changements, d.isoformat(), e["email"])
                            if chg is not None:
                                cr = chg.get("creneaux", []) or []
                            elif fer or absence_active(absences, e["email"], d) is not None:
                                cr = []
                            else:
                                cr = _jours_sem(act_l, e["email"], rot).get(str(j), []) or []
                            cr = [c for c in cr if creneau_valide(c)]
                            if not cr:
                                continue
                            lignes_j.append({"prenom": e["prenom"], "couleur": couleurs[e["email"]],
                                             "txt": " ".join(f'{c["debut"]}-{c["fin"]}' for c in cr)})
                        if lignes_j or fer:
                            tj.append({"label": f"{JOURS_ABBR[d.isoweekday()]} {d.strftime('%d/%m/%y')}",
                                       "ferie": fer, "lignes": lignes_j,
                                       "aujourdhui": d == jour_courant()})
                    v["texte_jours"] = tj
                else:  # tableau
                    # Tableau façon feuille de semaine : une colonne par jour, créneaux
                    # effectifs empilés (rouge si jour modifié), totaux travaillé/comptable.
                    v["titre"] = f"Semaine {lundi.isocalendar()[1]} ({rot})"
                    v["cols"] = [{"nom": JOURS_NOMS[j],
                                  "date": (lundi + timedelta(days=j - 1)).strftime("%d/%m/%Y"),
                                  "ferie": ferie_de(lundi + timedelta(days=j - 1)),
                                  "aujourdhui": lundi + timedelta(days=j - 1) == jour_courant()}
                                 for j in jours_aff]
                    lignes_t = []
                    for e in emp_sm:
                        cells, tot_eff, tot_trame = [], 0.0, 0.0
                        for j in jours_aff:
                            d = lundi + timedelta(days=j - 1)
                            fer = ferie_de(d)
                            cr_tr = [c for c in _jours_sem(act_l, e["email"], rot).get(str(j), []) or []
                                     if creneau_valide(c)]
                            chg = changement_de(changements, d.isoformat(), e["email"])
                            if chg is not None:
                                cr = [c for c in chg.get("creneaux", []) or [] if creneau_valide(c)]
                                modif = not meme_que_trame(cr, cr_tr)
                                tot_eff += total_jour(cr)
                            elif fer:
                                # Férié : cases vides (personne ne pointe), mais les
                                # heures de trame COMPTENT comme travaillées — un jour
                                # férié est payé (règle française).
                                cr, modif = [], False
                                tot_eff += total_jour(cr_tr)
                            elif absence_active(absences, e["email"], d) is not None:
                                cr, modif = [], bool(cr_tr)
                            else:
                                cr, modif = cr_tr, False
                                tot_eff += total_jour(cr)
                            tot_trame += total_jour(cr_tr)
                            cells.append({"creneaux": [f'{c["debut"]}-{c["fin"]}' for c in cr],
                                          "modifie": modif})
                        lignes_t.append({"prenom": e["prenom"], "cells": cells,
                                         "travaillees": _fmt_hmin(tot_eff),
                                         "comptables": _fmt_hmin(tot_trame)})
                    # Jours de fermeture sans personne (ex. dimanche) : colonne retirée.
                    ho = act_l.get("horaires_ouverture", HORAIRES_DEFAUT)
                    garder = [i for i, j in enumerate(jours_aff)
                              if ho.get(str(j)) or any(l["cells"][i]["creneaux"] for l in lignes_t)]
                    v["cols"] = [v["cols"][i] for i in garder]
                    for l in lignes_t:
                        l["cells"] = [l["cells"][i] for i in garder]
                    v["lignes"] = lignes_t
                # Vue JOUR (mobile) : un jour à la fois sur petit écran, construite
                # sur les mêmes règles que la frise (trame + ponctuels + absences +
                # fériés), quel que soit le mode d'affichage bureau choisi.
                v["jours_mobile"] = _frise(act_l, rot, emp_sm, couleurs, set(jours_aff),
                                           True, lundi, changements, absences,
                                           masquer_fermes=True)["jours"]
                vues.append(v)
        # Récapitulatif des changements ponctuels sur la période affichée.
        recap_chg = []
        if act:
            for lundi in lundis:
                tr_sem = trame_active_pour(data, lundi)
                for k in range(7):
                    dt = lundi + timedelta(days=k)
                    for em, ch in (changements.get(dt.isoformat(), {}) or {}).items():
                        if em not in emap:
                            continue
                        crs = ch.get("creneaux", []) or []
                        # Changement rétabli à la trame (horaires identiques) → pas une vraie
                        # modif : on ne l'affiche pas dans « Modifications apportées ».
                        if crs and meme_que_trame(crs, creneaux_trame_jour(tr_sem, em, dt)):
                            continue
                        # Jour vidé déjà couvert par une absence prolongée au même
                        # motif (ancien doublon) : l'absence suffit, on le masque.
                        if ponctuel_redondant(absences, em, dt, ch.get("motif"), crs):
                            continue
                        txt = " · ".join(f'{c["debut"]}–{c["fin"]}' for c in crs) if crs else "Non travaillé"
                        recap_chg.append({
                            "date": dt, "date_iso": dt.isoformat(),
                            "date_label": f'{JOURS_NOMS[dt.isoweekday()]} {dt.strftime("%d/%m")}',
                            "prenom": emap[em]["prenom"], "couleur": couleurs.get(em, "#888"),
                            "motif": ch.get("motif", "Non catégorisé"),
                            "creneaux_txt": txt, "absent": not crs})
            recap_chg.sort(key=lambda r: (r["date"], r["prenom"]))
        # Regroupé par collaborateur : nom + [jours modifiés] ; chaque jour porte son
        # détail (absence / horaires ajustés + motif) pour l'affichage au clic.
        recap_collab = []
        for r in recap_chg:
            g = next((x for x in recap_collab if x["prenom"] == r["prenom"]), None)
            if g is None:
                g = {"prenom": r["prenom"], "couleur": r["couleur"], "jours": []}
                recap_collab.append(g)
            etat = "🚫 Non travaillé" if r["absent"] else r["creneaux_txt"]
            g["jours"].append({"label": r["date_label"],
                               "detail": f'{etat} — {r["motif"]}'})
        # Heures réellement travaillées (depuis les relevés botRh) — le pont.
        heures_reel = None
        if act and opts.get("heures_travaillees", "aucun") != "aucun":
            from app import charger_reponses, reponse_de, MOIS_FR
            reps = charger_reponses(ref.month, ref.year)
            heures_reel = {"mois": f"{MOIS_FR[ref.month]} {ref.year}", "lignes": []}
            for e in emp_base:
                r = reponse_de(reps, e["prenom"], e["email"])
                if r:
                    plus, moins = r.get("heures_plus", 0), r.get("heures_moins", 0)
                    heures_reel["lignes"].append({"prenom": e["prenom"], "couleur": couleurs[e["email"]],
                                                  "plus": plus, "moins": moins,
                                                  "solde": round(plus - moins, 2)})
        # --- Saisie « Horaires ponctuels » : grille éditable du jour sélectionné ---
        ponctuel = request.args.get("ponctuel") == "1"
        saisie, ponctuel_jours = None, []
        if ponctuel and act:
            base_lundi = _lundi(ref)
            for k in range(7):
                d = base_lundi + timedelta(days=k)
                ponctuel_jours.append({"iso": d.isoformat(), "actif": d == ref,
                                       "label": f"{JOURS_NOMS[d.isoweekday()]} {d.strftime('%d/%m')}",
                                       "ferme": not act.get("horaires_ouverture", HORAIRES_DEFAUT).get(str(d.isoweekday()))})
            rows = []
            # Membres de la trame d'abord, puis les autres collaborateurs ACTIFS
            # (remplaçants potentiels — créés au préalable et non archivés).
            deja = {e["email"] for e in emp_base}
            saisissables = emp_base + [e for e in employes_base if e["email"] not in deja]
            for e in saisissables:
                remplacant = e["email"] not in deja
                cr_tr = creneaux_trame_jour(act, e["email"], ref)
                chg = changement_de(changements, ref.isoformat(), e["email"])
                abs_a = absence_active(absences, e["email"], ref) if chg is None else None
                if chg is not None:
                    cr_eff, motif_r = (chg.get("creneaux", []) or []), chg.get("motif", "Non catégorisé")
                elif abs_a is not None:
                    cr_eff, motif_r = [], abs_a.get("motif", "Non catégorisé")
                elif ferie_de(ref):
                    cr_eff, motif_r = [], "Non catégorisé"   # férié : personne par défaut
                else:
                    cr_eff, motif_r = cr_tr, "Non catégorisé"
                p, pt = _pad2(cr_eff), _pad2(cr_tr)
                rows.append({"email": e["email"], "prenom": e["prenom"],
                             "couleur": couleurs.get(e["email"], "#888"),
                             "modifie": chg is not None or abs_a is not None,
                             "motif": motif_r, "remplacant": remplacant,
                             "c1d": p[0]["debut"], "c1f": p[0]["fin"], "c2d": p[1]["debut"], "c2f": p[1]["fin"],
                             "t1d": pt[0]["debut"], "t1f": pt[0]["fin"], "t2d": pt[1]["debut"], "t2f": pt[1]["fin"],
                             "trame_txt": (" · ".join(f'{c["debut"]}–{c["fin"]}' for c in cr_tr)
                                           or ("hors trame" if remplacant else "repos"))})
            saisie = {"date_iso": ref.isoformat(), "rows": rows,
                      "date_label": f"{JOURS_NOMS[ref.isoweekday()]} {ref.strftime('%d/%m/%Y')}"}
        # --- Sous-vue « Absence prolongée » : formulaire + liste des absences ---
        abs_view = request.args.get("absence") == "1"
        absences_list, collabs_abs = [], []
        if abs_view:
            collabs_abs = [{"email": e["email"], "prenom": e["prenom"], "nom": e["nom"]} for e in emp_base]

            def _fr_date(iso):
                try:
                    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
                except (ValueError, TypeError):
                    return iso
            for a in sorted(absences, key=lambda x: (x.get("debut", ""), emap.get(x.get("email"), {}).get("prenom", ""))):
                em = a.get("email")
                if em not in emap:
                    continue
                absences_list.append({
                    "id": a.get("id", ""), "prenom": emap[em]["prenom"],
                    "couleur": couleurs.get(em, "#888"), "motif": a.get("motif", "Non catégorisé"),
                    "debut": _fr_date(a.get("debut", "")), "fin": _fr_date(a.get("fin", "")),
                    "commentaire": a.get("commentaire", "")})
        # Semaine antérieure au démarrage de la 1re trame activée → planning vide
        # avec message dédié (et non « aucune trame active »).
        debuts = [_debut_trame(t) for t in data.get("trames", []) if t.get("activee")]
        avant_trame = ""
        if act is None and debuts and min(debuts) > date.min:
            avant_trame = min(debuts).strftime("%d/%m/%Y")
        ctx.update(trame=act, tid=act.get("id") if act else None, pas_active=act is None,
                   avant_trame=avant_trame,
                   vues=vues, mode=mode, periode=periode, nav=nav, heures_reel=heures_reel,
                   motifs=MOTIFS, recap_chg=recap_chg, recap_collab=recap_collab,
                   ponctuel=ponctuel, saisie=saisie, ponctuel_jours=ponctuel_jours,
                   abs_view=abs_view, absences_list=absences_list, collabs_abs=collabs_abs,
                   ref_date=ref.isoformat(),
                   recap_changements=(opts.get("recap_changements") == "afficher"))
        return render_template("planning_equipe.html", **ctx)

    if onglet == "equipe":
        membres = []
        for e in employes_base:
            prof = profils.get(e["email"], {})
            membres.append({
                "email": e["email"], "prenom": e["prenom"], "nom": e["nom"],
                "fonction": poste_de(prof),
                "couleur_choisie": prof.get("couleur_planning", ""),   # "" = auto
                "couleur": couleurs[e["email"]],                       # effective (stable)
                "a_email": bool((e["email"] or "").strip()),
                "permission": prof.get("permission_planning", "Pas d'accès"),
                "vue": prof.get("vue_planning", "Le planning de tous"),
            })
        selected = next((m for m in membres if m["email"] == request.args.get("edit", "")), None)
        if selected and selected["a_email"]:
            import urllib.parse
            su = "Invitation — planning de la pharmacie"
            body = (f"Bonjour {selected['prenom']},\n\n"
                    "Tu es invité(e) à rejoindre / consulter le planning de l'équipe de la "
                    "pharmacie. Réponds à ce mail si tu as la moindre question.\n\n"
                    "Cordialement,\n\n" + SIGNATURE)
            selected["mail_invit"] = ("https://mail.google.com/mail/?view=cm&fs=1&to="
                                      + urllib.parse.quote(selected["email"])
                                      + "&su=" + urllib.parse.quote(su)
                                      + "&body=" + urllib.parse.quote(body))
        ctx.update(membres=membres, fonctions=FONCTIONS, palette=PALETTE_PLANNING,
                   permissions=PERMISSIONS, vues=VUES_PLANNING, selected=selected)
        return render_template("planning_equipe.html", **ctx)

    if onglet == "options":
        o = charger_options()
        masques = set(o.get("collaborateurs_masques", []))
        collabs = [{"email": e["email"], "prenom": e["prenom"], "nom": e["nom"],
                    "affiche": e["email"] not in masques} for e in employes_base]
        ctx.update(options=o, collabs_opt=collabs, jours_noms=JOURS_NOMS)
        return render_template("planning_equipe.html", **ctx)

    if onglet == "changements":
        from app import MOIS_FR
        changements = charger_changements()
        absences = charger_absences()

        def _fmt(cr):
            cr = cr or []
            txt = " ".join(f'{c["debut"]}–{c["fin"]}' for c in cr if c.get("debut") and c.get("fin"))
            return txt or "Non travaillé"

        def _entries(ym):
            """Tous les changements (ponctuels + absences) d'un mois 'AAAA-MM'."""
            y, mo = int(ym[:4]), int(ym[5:7])
            prem = date(y, mo, 1)
            dern = date(y, mo, calendar.monthrange(y, mo)[1])
            out = []
            for diso, parem in changements.items():
                if diso[:7] != ym:
                    continue
                try:
                    d = datetime.strptime(diso, "%Y-%m-%d").date()
                except ValueError:
                    continue
                for em, ch in (parem or {}).items():
                    if em not in emap:
                        continue
                    crs = ch.get("creneaux", []) or []
                    cr_tr = creneaux_trame_jour(trame_active_pour(data, d), em, d)
                    if meme_que_trame(crs, cr_tr):           # rétabli à la trame → pas une modif
                        continue
                    # Jour vidé déjà couvert par une absence prolongée au même
                    # motif (doublon) : l'absence suffit, on le masque du journal.
                    if ponctuel_redondant(absences, em, d, ch.get("motif"), crs):
                        continue
                    out.append({"type": "ponctuel", "prenom": emap[em]["prenom"],
                                "couleur": couleurs.get(em, "#888"),
                                "motif": ch.get("motif", "Non catégorisé"),
                                "email": em, "date_iso": diso, "date_label": d.strftime("%d/%m/%Y"),
                                "avant": _fmt(cr_tr), "apres": _fmt(crs),
                                "tri": (emap[em]["prenom"], diso)})
            for a in absences:
                em = a.get("email")
                if em not in emap:
                    continue
                try:
                    d1 = datetime.strptime(a.get("debut", ""), "%Y-%m-%d").date()
                    d2 = datetime.strptime(a.get("fin", ""), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if d2 < prem or d1 > dern:                   # ne touche pas ce mois
                    continue
                out.append({"type": "absence", "prenom": emap[em]["prenom"],
                            "couleur": couleurs.get(em, "#888"),
                            "motif": a.get("motif", "Non catégorisé"), "id": a.get("id", ""),
                            "commentaire": a.get("commentaire", ""),
                            "debut": d1.strftime("%d/%m/%Y"), "fin": d2.strftime("%d/%m/%Y"),
                            "tri": (emap[em]["prenom"], a.get("debut", ""))})
            out.sort(key=lambda x: x["tri"])
            return out

        # Mois disponibles (dates de changements + plages d'absences) + mois courant.
        mois_set = {diso[:7] for diso in changements.keys()}
        for a in absences:
            try:
                d1 = datetime.strptime(a.get("debut", ""), "%Y-%m-%d").date()
                d2 = datetime.strptime(a.get("fin", ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            m = d1.replace(day=1)
            while m <= d2:
                mois_set.add(m.strftime("%Y-%m"))
                m = _ajoute_mois(m, 1)
        today = date.today()
        mois_sel = request.args.get("mois") or today.strftime("%Y-%m")
        mois_set.add(mois_sel)
        mois_set.add(today.strftime("%Y-%m"))
        mois_list = [{"val": ym, "label": f"{MOIS_FR[int(ym[5:7])]} {ym[:4]} ({len(_entries(ym))})"}
                     for ym in sorted(mois_set, reverse=True)]
        groupes = []
        for e in _entries(mois_sel):
            g = next((x for x in groupes if x["prenom"] == e["prenom"]), None)
            if g is None:
                g = {"prenom": e["prenom"], "couleur": e["couleur"], "lignes": []}
                groupes.append(g)
            g["lignes"].append(e)
        ctx.update(chg_groupes=groupes, chg_mois_list=mois_list, chg_mois_sel=mois_sel,
                   chg_mois_label=f"{MOIS_FR[int(mois_sel[5:7])]} {mois_sel[:4]}",
                   chg_total=sum(len(g["lignes"]) for g in groupes))
        return render_template("planning_equipe.html", **ctx)

    if onglet == "effectifs":
        cfg = charger_effectifs()
        changements = charger_changements()
        absences = charger_absences()
        try:
            ref = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            ref = jour_courant()   # à 00h30, rester sur la semaine de la veille
        lundi = _lundi(ref)
        act = trame_active_pour(data, lundi)     # trame en vigueur pour CETTE semaine
        jours_eff, ticks, alertes_total = [], [], 0
        if act:
            ho_all = act.get("horaires_ouverture", HORAIRES_DEFAUT)
            amp_min, amp_max = _amplitude(ho_all)
            span = amp_max - amp_min
            ticks = [{"label": f"{h}h", "left": round((h * 60 - amp_min) / span * 100, 2)}
                     for h in range(amp_min // 60, amp_max // 60 + 1)]
            for k in range(7):
                dj = lundi + timedelta(days=k)
                jiso = dj.isoweekday()
                ouv = []
                for p in ho_all.get(str(jiso), []) or []:
                    a, b = _minutes(p[0]), _minutes(p[1])
                    if a is not None and b is not None and b > a:
                        ouv.append((a, b))
                if not ouv:
                    continue                       # jour fermé : pas de contrôle
                fer = ferie_de(dj)
                pres = []
                # Semaine passée : population complète (archivés inclus, historique).
                pop = employes_tous if lundi < _lundi(date.today()) else employes_base
                for e in pop:
                    est_ph = "pharmacien" in (poste_de(profils.get(e["email"], {})) or "").lower()
                    for c in creneaux_effectifs_jour(act, e["email"], dj, changements, absences):
                        pres.append((_minutes(c["debut"]), _minutes(c["fin"]), est_ph))
                # Découpage aux bornes (ouverture + présences) puis contrôle par segment.
                pts = sorted({x for ab in ouv for x in ab} | {x for (a, b, _) in pres for x in (a, b)})
                segs = []
                for a, b in zip(pts, pts[1:]):
                    m = (a + b) / 2
                    if not any(o1 <= m < o2 for o1, o2 in ouv):
                        continue
                    tot = sum(1 for (p1, p2, _) in pres if p1 <= m < p2)
                    ph = sum(1 for (p1, p2, x) in pres if x and p1 <= m < p2)
                    if tot < cfg["min_total"]:
                        typ = "rouge"
                        lab = f"{tot} présent{'s' if tot > 1 else ''} (min {cfg['min_total']})"
                    elif ph < cfg["min_pharmaciens"]:
                        typ = "orange"
                        lab = f"{ph} pharmacien{'s' if ph > 1 else ''} (min {cfg['min_pharmaciens']})"
                    else:
                        continue
                    if segs and segs[-1]["type"] == typ and segs[-1]["lab"] == lab and segs[-1]["fin"] == a:
                        segs[-1]["fin"] = b            # fusion des segments contigus
                    else:
                        segs.append({"deb": a, "fin": b, "type": typ, "lab": lab})
                barres_ouv = []
                for a, b in ouv:
                    left, width = _pos(a, b, amp_min, span)
                    barres_ouv.append({"left": left, "width": width})
                barres_alerte, alertes = [], []
                for s in segs:
                    left, width = _pos(s["deb"], s["fin"], amp_min, span)
                    txt = f'{_hhmm(s["deb"])}–{_hhmm(s["fin"])} : {s["lab"]}'
                    barres_alerte.append({"left": left, "width": width, "type": s["type"], "titre": txt})
                    alertes.append({"type": s["type"], "txt": txt})
                alertes_total += len(alertes)
                jours_eff.append({"nom": f"{JOURS_NOMS[jiso]} {dj.strftime('%d/%m')}", "ferie": fer,
                                  "ouverture": barres_ouv, "barres": barres_alerte,
                                  "alertes": alertes, "ok": not alertes})
        ctx.update(eff_cfg=cfg, eff_jours=jours_eff, eff_ticks=ticks, eff_total=alertes_total,
                   pas_active=act is None, ref_date=ref.isoformat(),
                   eff_semaine=f"{lundi.strftime('%d/%m')} – {(lundi + timedelta(days=6)).strftime('%d/%m/%Y')}",
                   eff_prec=(lundi - timedelta(days=7)).isoformat(),
                   eff_suiv=(lundi + timedelta(days=7)).isoformat(),
                   eff_auj=jour_courant().isoformat())
        return render_template("planning_equipe.html", **ctx)

    if onglet == "conges":
        absences = charger_absences()
        changements = charger_changements()
        conges = charger_conges()
        try:
            an_sel = int(request.args.get("periode", ""))
            p1, p2 = date(an_sel, 6, 1), date(an_sel + 1, 5, 31)
        except (ValueError, TypeError):
            p1, p2 = periode_conges()
        cp_lignes = []
        for e in employes_base:
            em = e["email"]
            b = bilan_cp(em, absences, changements, conges, p1, p2)
            cp_lignes.append({**b, "email": em, "prenom": e["prenom"], "nom": e["nom"],
                              "couleur": couleurs.get(em, "#888")})
        # Demandes de congés des employés (déposées depuis Mon espace).
        def _fr(iso):
            try:
                return datetime.strptime(iso, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return None
        cp_attente, cp_traitees = [], []
        restants = {l["email"]: l["restant"] for l in cp_lignes}
        for dm in sorted(charger_demandes_cp(), key=lambda x: x.get("demande_le", ""), reverse=True):
            em = dm.get("email")
            if em not in emap:
                continue
            d1, d2 = _fr(dm.get("debut", "")), _fr(dm.get("fin", ""))
            nb = _jours_ouvrables_cp(d1, d2, d1, d2) if d1 and d2 else 0
            row = {"id": dm.get("id", ""), "prenom": emap[em]["prenom"],
                   "couleur": couleurs.get(em, "#888"),
                   "debut": d1.strftime("%d/%m/%Y") if d1 else "?",
                   "fin": d2.strftime("%d/%m/%Y") if d2 else "?",
                   "nb": nb, "commentaire": dm.get("commentaire", ""),
                   "demande_le": dm.get("demande_le", ""), "traite_le": dm.get("traite_le", ""),
                   "statut": dm.get("statut", ""), "motif_refus": dm.get("motif_refus", ""),
                   "statut_label": STATUTS_CP.get(dm.get("statut", ""), dm.get("statut", "")),
                   "depasse": nb > restants.get(em, 0)}
            if dm.get("statut") == "en_attente":
                cp_attente.append(row)
            elif dm.get("statut") in ("acceptee", "refusee"):
                cp_traitees.append(row)
        an_cour = periode_conges()[0].year
        cp_periodes = [{"an": a, "label": f"1 juin {a} → 31 mai {a + 1}",
                        "actif": a == p1.year} for a in range(an_cour, an_cour - 3, -1)]
        # --- Vue annuelle : jours d'absence par collaborateur et par mois -----
        # (jours ouvrables lun-sam hors fériés, comme le décompte CP). Conflit =
        # jour où AU MOINS DEUX collaborateurs sont en congés payés en même temps.
        cp_jours, autres_jours = {}, {}      # email -> {date: motif}
        for a in absences:
            em = a.get("email")
            d1, d2 = _fr(a.get("debut", "")), _fr(a.get("fin", ""))
            if em not in emap or not d1 or not d2:
                continue
            d = max(d1, p1)
            while d <= min(d2, p2):
                if d.isoweekday() <= 6 and not ferie_de(d):
                    cible = cp_jours if a.get("motif") == "Congés payés" else autres_jours
                    cible.setdefault(em, {})[d] = a.get("motif", "Absence")
                d += timedelta(days=1)
        cp_compte = {}
        for jours in cp_jours.values():
            for d in jours:
                cp_compte[d] = cp_compte.get(d, 0) + 1
        mois_annuel = [_ajoute_mois(p1, k) for k in range(12)]
        annuel_lignes = []
        for e in employes_base:
            em, cellules = e["email"], []
            for m0 in mois_annuel:
                m_fin = m0.replace(day=calendar.monthrange(m0.year, m0.month)[1])
                cp_m = [d for d in cp_jours.get(em, {}) if m0 <= d <= m_fin]
                au_m = {d: mo for d, mo in autres_jours.get(em, {}).items() if m0 <= d <= m_fin}
                titre = " · ".join(filter(None, [
                    f"{len(cp_m)} j de congés payés" if cp_m else "",
                    f"{len(au_m)} j autres ({', '.join(sorted(set(au_m.values())))})" if au_m else ""]))
                cellules.append({"cp": len(cp_m), "autres": len(au_m),
                                 "conflit": any(cp_compte.get(d, 0) >= 2 for d in cp_m),
                                 "titre": titre})
            annuel_lignes.append({"prenom": e["prenom"], "couleur": couleurs.get(em, "#888"),
                                  "cellules": cellules,
                                  "total_cp": len(cp_jours.get(em, {})),
                                  "total_autres": len(autres_jours.get(em, {}))})
        annuel_mois = [f"{MOIS_ABBR[m.month]} {m.year % 100:02d}" for m in mois_annuel]
        annuel_conflits = sum(1 for n in cp_compte.values() if n >= 2)
        ctx.update(cp_lignes=cp_lignes, cp_periodes=cp_periodes, cp_annee=p1.year,
                   cp_label=f"1er juin {p1.year} → 31 mai {p2.year}",
                   cp_total_poses=round(sum(l["poses"] for l in cp_lignes), 1),
                   cp_attente=cp_attente, cp_traitees=cp_traitees[:8],
                   annuel_mois=annuel_mois, annuel_lignes=annuel_lignes,
                   annuel_conflits=annuel_conflits)
        return render_template("planning_equipe.html", **ctx)

    if onglet == "demandes":
        # « Mes demandes » : la pharmacie propose des congés ou demande des heures
        # supplémentaires à un collaborateur ; il répond depuis son Mon espace
        # (notification in-app, aucun e-mail). Afficher l'onglet marque les
        # réponses comme lues (efface la notification du tableau de bord).
        demandes = charger_demandes_admin()
        lues = False
        for dm in demandes:
            if dm.get("statut") in ("acceptee", "refusee") and not dm.get("lu_admin"):
                dm["lu_admin"] = True
                lues = True
        if lues:
            sauvegarder_demandes_admin(demandes)
        dem_attente, dem_historique = [], []
        for dm in sorted(demandes, key=lambda x: x.get("cree_le", ""), reverse=True):
            em = dm.get("email", "")
            info = {**dm,
                    "prenom": emap[em]["prenom"] if em in emap else em,
                    "couleur": couleurs.get(em, "#888"),
                    "type_label": TYPES_DEMANDE_ADMIN.get(dm.get("type", ""), dm.get("type", "")),
                    "statut_label": STATUTS_DEMANDE_ADMIN.get(dm.get("statut", ""), dm.get("statut", "")),
                    "quand": _quand_demande_admin(dm)}
            (dem_attente if dm.get("statut") == "en_attente" else dem_historique).append(info)
        ctx.update(dem_attente=dem_attente, dem_historique=dem_historique[:12],
                   dem_collabs=[{"email": e["email"], "prenom": e["prenom"], "nom": e["nom"]}
                                for e in employes_base],
                   dem_auj=date.today().isoformat())
        return render_template("planning_equipe.html", **ctx)

    if onglet == "totaux":
        from app import charger_reponses, reponse_de, MOIS_FR, periode_paie
        changements = charger_changements()
        absences = charger_absences()
        today = date.today()
        mois_sel = request.args.get("mois") or today.strftime("%Y-%m")
        try:
            an, mo = int(mois_sel[:4]), int(mois_sel[5:7])
            date(an, mo, 1)
        except (ValueError, TypeError):
            an, mo = today.year, today.month
            mois_sel = today.strftime("%Y-%m")
        # PÉRIODE DU RELEVÉ (et non le mois calendaire) : le planning et le
        # relevé doivent couvrir les MÊMES jours pour que l'écart ait un sens.
        # periode_paie (app.py) est la source unique : 24→fin de mois jusqu'à
        # juillet 2026 (l'utilisateur a volontairement compté fin juin + tout
        # juillet), 1er→25 en août 2026, 26→25 ensuite.
        p1, p2 = periode_paie(mo, an)
        # Trame de référence pour la liste des membres = celle en vigueur au
        # dernier jour de la période ; les heures se calculent jour par jour
        # avec la trame en vigueur à chaque date (succession conservée).
        act = trame_active_pour(data, p2)
        reps = charger_reponses(mo, an) or {}
        lignes, tot = [], {"trame": 0.0, "ajuste": 0.0, "solde_plan": 0.0,
                           "plus": 0.0, "moins": 0.0, "solde": 0.0, "ecart": 0.0}
        nb_releves = 0
        # Membres du mois : semaine passée → tous (historique), sinon actifs ;
        # + les remplaçants hors trame ayant des horaires ponctuels dans la période.
        membres_mois = membres_semaine(act, employes_tous, profils, _lundi(p2))
        vus_mois = set(membres_mois)
        for diso, parem in changements.items():
            if not (p1.isoformat() <= diso <= p2.isoformat()):
                continue
            for em2, ch in (parem or {}).items():
                if em2 not in vus_mois and em2 in emap and (ch.get("creneaux") or []):
                    membres_mois.append(em2)
                    vus_mois.add(em2)
        for em in membres_mois:
            e = emap[em]
            h_trame = h_ajuste = 0.0
            j_ponctuels = j_absents = 0
            # Détail des écarts à la trame pour le panneau dépliable : ponctuels
            # listés jour par jour, absences regroupées en plages consécutives.
            detail_plan = []
            abs_en_cours = None
            for k in range((p2 - p1).days + 1):
                d = p1 + timedelta(days=k)
                lbl = f"{JOURS_ABBR[d.isoweekday()]} {d.strftime('%d/%m')}"
                cr_tr = creneaux_trame_jour(trame_active_pour(data, d), em, d)
                ht = total_jour(cr_tr)
                chg = changement_de(changements, d.isoformat(), em)
                a = absence_active(absences, em, d) if chg is None else None
                if chg is not None:
                    crs = chg.get("creneaux", []) or []
                    ha = total_jour(crs)
                    if not meme_que_trame(crs, cr_tr):
                        j_ponctuels += 1
                        if abs_en_cours:
                            detail_plan.append(abs_en_cours)
                            abs_en_cours = None
                        detail_plan.append({
                            "type": "ponctuel", "label": lbl,
                            "motif": chg.get("motif", "Non catégorisé"),
                            "avant": round(ht, 2), "apres": round(ha, 2),
                            "delta": round(ha - ht, 2)})
                elif a is not None:
                    ha = 0.0
                    if ht > 0:
                        j_absents += 1
                        motif_a = a.get("motif", "Absence")
                        if abs_en_cours and abs_en_cours["motif"] == motif_a:
                            abs_en_cours["au"] = lbl
                            abs_en_cours["jours"] += 1
                            abs_en_cours["delta"] = round(abs_en_cours["delta"] - ht, 2)
                        else:
                            if abs_en_cours:
                                detail_plan.append(abs_en_cours)
                            abs_en_cours = {"type": "absence", "label": lbl,
                                            "au": lbl, "motif": motif_a,
                                            "jours": 1, "delta": round(-ht, 2)}
                else:
                    ha = ht
                h_trame += ht
                h_ajuste += ha
            if abs_en_cours:
                detail_plan.append(abs_en_cours)
            h_trame, h_ajuste = round(h_trame, 2), round(h_ajuste, 2)
            solde_plan = round(h_ajuste - h_trame, 2)
            r = reponse_de(reps, e["prenom"], em)
            if r:
                plus = float(r.get("heures_plus", 0) or 0)
                moins = float(r.get("heures_moins", 0) or 0)
                solde = round(plus - moins, 2)
                ecart = round(solde - solde_plan, 2)
                nb_releves += 1
                tot["plus"] += plus
                tot["moins"] += moins
                tot["solde"] += solde
                tot["ecart"] += ecart
            else:
                plus = moins = solde = ecart = None
            tot["trame"] += h_trame
            tot["ajuste"] += h_ajuste
            tot["solde_plan"] += solde_plan
            lignes.append({"prenom": e["prenom"], "nom": e["nom"],
                           "couleur": couleurs.get(em, "#888"),
                           "trame": h_trame, "ajuste": h_ajuste,
                           "solde_plan": solde_plan,
                           "j_ponctuels": j_ponctuels, "j_absents": j_absents,
                           "plus": plus, "moins": moins, "solde": solde,
                           "ecart": ecart,
                           "coherent": ecart is not None and abs(ecart) <= 0.01,
                           "detail_plan": detail_plan,
                           "detail_releve": (r.get("jours") or []) if r else []})
        tot = {k: round(v, 2) for k, v in tot.items()}
        # Sélecteur : les 12 derniers mois + le mois sélectionné.
        mois_set = {(_ajoute_mois(today.replace(day=1), -k)).strftime("%Y-%m")
                    for k in range(12)}
        mois_set.add(mois_sel)
        mois_list = [{"val": ym, "label": f"{MOIS_FR[int(ym[5:7])]} {ym[:4]}"}
                     for ym in sorted(mois_set, reverse=True)]
        ctx.update(tot_lignes=lignes, tot_totaux=tot, tot_nb_releves=nb_releves,
                   tot_mois_list=mois_list, tot_mois_sel=mois_sel,
                   tot_mois_label=f"{MOIS_FR[mo]} {an}",
                   tot_periode=f"du {p1.strftime('%d/%m/%Y')} au {p2.strftime('%d/%m/%Y')}",
                   pas_active=act is None)
        return render_template("planning_equipe.html", **ctx)

    # sous-onglets à venir
    return render_template("planning_equipe.html", **ctx)


@bp.route("/admin/planning-equipe/creer", methods=["POST"])
def creer_trame():
    """Crée une nouvelle trame (non activée) et la sélectionne. Champs : date de
    démarrage, nb de semaines tournantes, commentaire, et option « importer » (copie
    les horaires + l'ouverture d'une trame existante)."""
    if not _admin():
        return redirect(url_for("admin"))
    import copy
    data = charger_trames()
    t = _nouvelle_trame((request.form.get("commentaire") or "").strip())
    t["date_demarrage"] = (request.form.get("date_demarrage") or "").strip()
    try:
        t["nb_semaines"] = max(1, min(15, int(request.form.get("nb_semaines", 2))))
    except ValueError:
        t["nb_semaines"] = 2
    src = trame_par_id(data, request.form.get("importer", ""))
    if src:
        t["employes"] = copy.deepcopy(src.get("employes", {}))
        t["horaires_ouverture"] = copy.deepcopy(src.get("horaires_ouverture", HORAIRES_DEFAUT))
        t["semaine_demarrage"] = src.get("semaine_demarrage", "A")
    data.setdefault("trames", []).append(t)
    sauvegarder_trames(data)
    return redirect(url_for(".vue", onglet="trame", trame=t["id"], msg="trame_creee"))


@bp.route("/admin/planning-equipe/supprimer-trame", methods=["POST"])
def supprimer_trame():
    """Supprime DÉFINITIVEMENT une trame (et tous ses horaires). RÈGLE MÉTIER :
    une trame ACTIVÉE ne peut pas être supprimée — les anciennes trames activées
    doivent être conservées, sans elles l'historique des plannings serait perdu."""
    if not _admin():
        return redirect(url_for("admin"))
    tid = request.form.get("tid", "")
    data = charger_trames()
    t = trame_par_id(data, tid)
    if t and t.get("activee"):
        return redirect(url_for(".vue", onglet="trame", trame=tid, msg="trame_protegee"))
    data["trames"] = [x for x in data.get("trames", []) if x.get("id") != tid]
    sauvegarder_trames(data)
    return redirect(url_for(".vue", onglet="trame", msg="trame_suppr"))


@bp.route("/admin/planning-equipe/activer", methods=["POST"])
def toggle_trame():
    """Active / désactive une trame. RÈGLE MÉTIER : plusieurs trames activées
    coexistent et se SUCCÈDENT par date de démarrage — pour une semaine donnée,
    le planning prend la trame activée la plus récente qui a démarré avant elle.
    Les anciennes trames activées portent l'historique : les laisser activées."""
    if not _admin():
        return redirect(url_for("admin"))
    tid = request.form.get("tid", "")
    data = charger_trames()
    t = trame_par_id(data, tid)
    if t:
        t["activee"] = not t.get("activee")
        msg = "activee" if t["activee"] else "desactivee"
        sauvegarder_trames(data)
    else:
        msg = "no_trame"
    return redirect(url_for(".vue", onglet="trame", trame=tid, msg=msg))


@bp.route("/admin/planning-equipe/trame", methods=["POST"])
def enregistrer_trame():
    if not _admin():
        return redirect(url_for("admin"))
    sem = request.form.get("sem", "A")
    if sem not in SEMAINES:
        sem = "A"
    tid = request.form.get("tid", "")
    data = charger_trames()
    trame = trame_par_id(data, tid)
    if not trame:
        return redirect(url_for(".vue", onglet="trame", msg="no_trame"))
    emps = trame.setdefault("employes", {})
    for e in charger_employes():
        jours = {}
        for j in range(1, 8):
            creneaux = []
            for s in range(2):
                base = f"tr__{e['email']}__{sem}__{j}__{s}__"
                deb = (request.form.get(base + "debut") or "").strip()
                fin = (request.form.get(base + "fin") or "").strip()
                if deb and fin:
                    creneaux.append({"debut": deb, "fin": fin})
            creneaux.sort(key=lambda c: _minutes(c["debut"]) if _minutes(c["debut"]) is not None else 0)
            jours[str(j)] = creneaux
        emps.setdefault(e["email"], {})[sem] = jours
    trame["maj"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    sauvegarder_trames(data)
    return redirect(url_for(".vue", onglet="trame", sem=sem, trame=tid, msg="trame_ok"))


@bp.route("/admin/planning-equipe/options", methods=["POST"])
def enregistrer_options():
    """Enregistre les options d'affichage du planning."""
    if not _admin():
        return redirect(url_for("admin"))
    base = [e["email"] for e in charger_employes()]
    affiches = request.form.getlist("collab")
    jours = [d for d in request.form.getlist("jour") if d in ("1", "2", "3", "4", "5", "6", "7")]
    o = charger_options()
    o["collaborateurs_masques"] = [em for em in base if em not in affiches]
    o["jours"] = jours or ["1", "2", "3", "4", "5", "6", "7"]
    o["periode"] = request.form.get("periode", "hebdo")
    o["mode"] = request.form.get("mode", "grille")
    o["lignes_vides"] = request.form.get("lignes_vides", "afficher")
    o["horaires_grille"] = request.form.get("horaires_grille", "afficher")
    o["recap_changements"] = request.form.get("recap_changements", "masquer")
    o["heures_travaillees"] = request.form.get("heures_travaillees", "aucun")
    sauvegarder_options(o)
    # Retour direct au planning pour voir l'effet des options.
    return redirect(url_for(".vue", onglet="planning", msg="options_ok"))


@bp.route("/admin/planning-equipe/effectifs", methods=["POST"])
def enregistrer_effectifs():
    """Enregistre les minimums d'effectifs (total + pharmaciens)."""
    if not _admin():
        return redirect(url_for("admin"))
    o = charger_effectifs()
    for cle in ("min_total", "min_pharmaciens"):
        try:
            o[cle] = max(0, int(request.form.get(cle, o[cle])))
        except (TypeError, ValueError):
            pass
    _ecrire_json(EFFECTIFS_FILE, o)
    return redirect(url_for(".vue", onglet="effectifs",
                            date=request.form.get("date", ""), msg="effectifs_ok"))


@bp.route("/admin/planning-equipe/conges", methods=["POST"])
def enregistrer_conges():
    """Enregistre les droits CP (droit annuel + report) par collaborateur."""
    if not _admin():
        return redirect(url_for("admin"))
    conges = charger_conges()
    for e in charger_employes():
        em = e["email"]
        cle_d, cle_r = f"droit_{em}", f"report_{em}"
        if cle_d not in request.form and cle_r not in request.form:
            continue
        cf = conges.get(em) if isinstance(conges.get(em), dict) else {}
        for champ, cle in (("droit", cle_d), ("report", cle_r)):
            try:
                v = float((request.form.get(cle) or "").replace(",", "."))
                cf[champ] = round(v, 1) if v != int(v) else int(v)
            except (TypeError, ValueError):
                pass
        conges[em] = cf
    _ecrire_json(CONGES_FILE, conges)
    return redirect(url_for(".vue", onglet="conges",
                            periode=request.form.get("periode", ""), msg="conges_ok"))


def _notifier_demande_cp(action, dm, prenom):
    """Mail de congés via le runner GitHub (SMTP bloqué sur le serveur gratuit) :
    dépôt → notification à l'admin, décision → réponse à l'employé.
    Best-effort : ne bloque jamais le traitement de la demande."""
    try:
        from app import declencher_workflow
        d1 = datetime.strptime(dm.get("debut", ""), "%Y-%m-%d").date()
        d2 = datetime.strptime(dm.get("fin", ""), "%Y-%m-%d").date()
        declencher_workflow("demande_conges", {
            "action": action,
            "prenom": prenom,
            "email": dm.get("email", ""),
            "debut": d1.strftime("%d/%m/%Y"),
            "fin": d2.strftime("%d/%m/%Y"),
            "nb": _jours_ouvrables_cp(d1, d2, d1, d2),
            "commentaire": dm.get("commentaire", ""),
            "motif_refus": dm.get("motif_refus", ""),
        })
    except Exception:
        current_app.logger.exception("Échec notification demande de congés")


@bp.route("/admin/planning-equipe/conges/traiter", methods=["POST"])
def traiter_demande_cp():
    """Accepte ou refuse une demande de congés. Accepter = créer l'absence
    « Congés payés » au planning (le décompte CP suit automatiquement)."""
    if not _admin():
        return redirect(url_for("admin"))
    did = request.form.get("id", "")
    action = request.form.get("action", "")
    demandes = charger_demandes_cp()
    dm = next((x for x in demandes if x.get("id") == did), None)
    if not dm or dm.get("statut") != "en_attente":
        return redirect(url_for(".vue", onglet="conges", msg="cp_deja"))
    if action == "accepter":
        absences = charger_absences()
        aid = datetime.now().strftime("%Y%m%d%H%M%S%f")
        absences.append({"id": aid, "email": dm["email"],
                         "debut": dm.get("debut", ""), "fin": dm.get("fin", ""),
                         "motif": "Congés payés",
                         "commentaire": (dm.get("commentaire") or "Demande employé").strip()})
        sauvegarder_absences(absences)
        dm["statut"], dm["absence_id"], msg = "acceptee", aid, "cp_acceptee"
    elif action == "refuser":
        dm["statut"] = "refusee"
        dm["motif_refus"] = (request.form.get("motif_refus") or "").strip()[:200]
        msg = "cp_refusee"
    else:
        return redirect(url_for(".vue", onglet="conges"))
    dm["traite_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    sauvegarder_demandes_cp(demandes)
    emp = next((e for e in charger_employes()
                if e.get("email", "").lower() == dm.get("email", "").lower()), None)
    _notifier_demande_cp(dm["statut"], dm, emp["prenom"] if emp else dm.get("email", ""))
    return redirect(url_for(".vue", onglet="conges", msg=msg))


@bp.route("/mon-espace/conges/demander", methods=["POST"])
def demander_conges():
    """Dépôt d'une demande de congés par un employé (jeton signé, pas de session)."""
    from tokens import resoudre_employe
    token = request.form.get("token", "")
    emp = resoudre_employe(token, charger_employes())
    if not emp:
        abort(403)
    retour = f"/mon-espace?token={token}&prenom={emp['prenom']}"
    try:
        d1 = datetime.strptime(request.form.get("debut", ""), "%Y-%m-%d").date()
        d2 = datetime.strptime(request.form.get("fin", ""), "%Y-%m-%d").date()
    except ValueError:
        return redirect(retour + "&cp=dates#conges")
    if d2 < d1 or d1 < date.today():
        return redirect(retour + "&cp=dates#conges")
    demandes = charger_demandes_cp()
    demandes.append({"id": uuid.uuid4().hex[:10], "email": emp["email"],
                     "debut": d1.isoformat(), "fin": d2.isoformat(),
                     "commentaire": (request.form.get("commentaire") or "").strip()[:300],
                     "statut": "en_attente",
                     "demande_le": datetime.now().strftime("%d/%m/%Y %H:%M")})
    sauvegarder_demandes_cp(demandes)
    _notifier_demande_cp("deposee", demandes[-1], emp["prenom"])
    return redirect(retour + "&cp=ok#conges")


@bp.route("/mon-espace/conges/annuler", methods=["POST"])
def annuler_demande_conges():
    """L'employé annule sa demande tant qu'elle est en attente."""
    from tokens import resoudre_employe
    token = request.form.get("token", "")
    emp = resoudre_employe(token, charger_employes())
    if not emp:
        abort(403)
    demandes = charger_demandes_cp()
    for dm in demandes:
        if (dm.get("id") == request.form.get("id", "") and dm.get("email") == emp["email"]
                and dm.get("statut") == "en_attente"):
            dm["statut"] = "annulee"
            dm["traite_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            sauvegarder_demandes_cp(demandes)
            break
    return redirect(f"/mon-espace?token={token}&prenom={emp['prenom']}&cp=annulee#conges")


@bp.route("/admin/planning-equipe/demandes/creer", methods=["POST"])
def creer_demande_admin():
    """La pharmacie envoie une demande (congés ou heures sup) à un collaborateur.
    Il la verra dans son Mon espace — aucun e-mail n'est envoyé."""
    if not _admin():
        return redirect(url_for("admin"))
    email = request.form.get("email", "")
    typ = request.form.get("type", "")
    if typ not in TYPES_DEMANDE_ADMIN or not any(
            e["email"] == email for e in charger_employes()):
        return redirect(url_for(".vue", onglet="demandes", msg="dem_invalide"))
    try:
        d1 = datetime.strptime(request.form.get("debut", ""), "%Y-%m-%d").date()
    except ValueError:
        return redirect(url_for(".vue", onglet="demandes", msg="dem_invalide"))
    dm = {"id": uuid.uuid4().hex[:10], "email": email, "type": typ,
          "debut": d1.isoformat(), "fin": d1.isoformat(),
          "commentaire": (request.form.get("commentaire") or "").strip()[:300],
          "statut": "en_attente", "lu_admin": True,
          "cree_le": datetime.now().strftime("%d/%m/%Y %H:%M")}
    if typ == "conges":
        try:
            d2 = datetime.strptime(request.form.get("fin", ""), "%Y-%m-%d").date()
        except ValueError:
            return redirect(url_for(".vue", onglet="demandes", msg="dem_invalide"))
        if d2 < d1 or d1 < date.today():
            return redirect(url_for(".vue", onglet="demandes", msg="dem_dates"))
        dm["fin"] = d2.isoformat()
    else:                                            # heures supplémentaires
        hd, hf = _norm_hhmm(request.form.get("h_debut")), _norm_hhmm(request.form.get("h_fin"))
        if not hd or not hf or _minutes(hf) <= _minutes(hd) or d1 < date.today():
            return redirect(url_for(".vue", onglet="demandes", msg="dem_dates"))
        dm["h_debut"], dm["h_fin"] = hd, hf
    demandes = charger_demandes_admin()
    demandes.append(dm)
    sauvegarder_demandes_admin(demandes)
    return redirect(url_for(".vue", onglet="demandes", msg="dem_envoyee"))


@bp.route("/admin/planning-equipe/demandes/annuler", methods=["POST"])
def annuler_demande_admin():
    """Annule une demande tant que le collaborateur n'a pas répondu."""
    if not _admin():
        return redirect(url_for("admin"))
    demandes = charger_demandes_admin()
    for dm in demandes:
        if dm.get("id") == request.form.get("id", "") and dm.get("statut") == "en_attente":
            dm["statut"] = "annulee"
            dm["traite_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            sauvegarder_demandes_admin(demandes)
            break
    return redirect(url_for(".vue", onglet="demandes", msg="dem_annulee"))


@bp.route("/mon-espace/demandes/repondre", methods=["POST"])
def repondre_demande_admin():
    """Le collaborateur accepte ou refuse une demande de la pharmacie (jeton
    signé, pas de session). Accepter des congés = l'absence « Congés payés »
    est créée au planning ; accepter des heures sup = un horaire ponctuel
    « Heures sup/récup/échanges » ajoute le créneau au jour concerné."""
    from tokens import resoudre_employe
    token = request.form.get("token", "")
    emp = resoudre_employe(token, charger_employes())
    if not emp:
        abort(403)
    retour = f"/mon-espace?token={token}&prenom={emp['prenom']}"
    action = request.form.get("action", "")
    demandes = charger_demandes_admin()
    dm = next((x for x in demandes if x.get("id") == request.form.get("id", "")
               and x.get("email") == emp["email"]), None)
    if not dm or dm.get("statut") != "en_attente" or action not in ("accepter", "refuser"):
        return redirect(retour + "&dem=deja#demandes")
    if action == "accepter":
        if dm.get("type") == "conges":
            absences = charger_absences()
            aid = datetime.now().strftime("%Y%m%d%H%M%S%f")
            absences.append({"id": aid, "email": emp["email"],
                             "debut": dm.get("debut", ""), "fin": dm.get("fin", ""),
                             "motif": "Congés payés",
                             "commentaire": (dm.get("commentaire") or "Proposé par la pharmacie").strip()})
            sauvegarder_absences(absences)
            dm["absence_id"] = aid
        else:                                        # heures supplémentaires
            try:
                d_obj = datetime.strptime(dm.get("debut", ""), "%Y-%m-%d").date()
            except ValueError:
                d_obj = None
            if d_obj:
                chgs = charger_changements()
                act = trame_active_pour(charger_trames(), d_obj)
                existants = [c for c in creneaux_effectifs_jour(
                    act, emp["email"], d_obj, chgs, charger_absences())
                    if creneau_valide(c)] if act else []
                creneaux = existants + [{"debut": dm.get("h_debut", ""), "fin": dm.get("h_fin", "")}]
                creneaux.sort(key=lambda c: _minutes(c["debut"]) or 0)
                chgs.setdefault(d_obj.isoformat(), {})[emp["email"]] = {
                    "motif": "Heures sup/récup/échanges", "creneaux": creneaux,
                    "maj": datetime.now().strftime("%d/%m/%Y %H:%M")}
                sauvegarder_changements(chgs)
        dm["statut"] = "acceptee"
    else:
        dm["statut"] = "refusee"
    dm["reponse"] = (request.form.get("commentaire") or "").strip()[:300]
    dm["traite_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    dm["lu_admin"] = False    # notification « réponse reçue » côté pharmacie
    sauvegarder_demandes_admin(demandes)
    return redirect(retour + "&dem=repondu#demandes")


@bp.route("/admin/planning-equipe/changement", methods=["POST"])
def enregistrer_changement():
    """Changement ponctuel des heures d'un collaborateur pour une date réelle
    (retard, absence, heures sup…). Surcharge la trame pour ce jour-là."""
    if not _admin():
        return redirect(url_for("admin"))
    email = request.form.get("email", "")
    date_iso = request.form.get("date", "")
    motif = request.form.get("motif", "")
    creneaux = []
    if not request.form.get("non_travaille"):
        for s in (1, 2):
            deb = _norm_hhmm(request.form.get(f"h{s}d"))
            fin = _norm_hhmm(request.form.get(f"h{s}f"))
            if deb and fin:
                creneaux.append({"debut": deb, "fin": fin})
        creneaux.sort(key=lambda c: _minutes(c["debut"]) if _minutes(c["debut"]) is not None else 0)
    if email and date_iso:
        data = charger_changements()
        # « ↺ Rétablir les horaires » : si les créneaux saisis sont identiques à la trame,
        # ce n'est pas une modification → on supprime l'éventuel changement au lieu d'en créer un.
        try:
            d_obj = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            d_obj = None
        act = trame_active_pour(charger_trames(), d_obj) if d_obj else None
        motif_norm = motif if motif in MOTIFS else "Non catégorisé"
        # Sur un jour FÉRIÉ, saisir les horaires de trame note la personne
        # PRÉSENTE (censée travailler) : pas de retour-trame dans ce cas.
        retour_trame = bool(creneaux) and d_obj is not None and not ferie_de(d_obj) and \
            meme_que_trame(creneaux, creneaux_trame_jour(act, email, d_obj))
        # Jour vidé déjà couvert par une absence prolongée au même motif :
        # doublon sans intérêt → on ne crée rien (et on nettoie l'existant).
        redondant = d_obj is not None and \
            ponctuel_redondant(charger_absences(), email, d_obj, motif_norm, creneaux)
        if retour_trame or redondant:
            if date_iso in data and email in data[date_iso]:
                del data[date_iso][email]
                if not data[date_iso]:
                    del data[date_iso]
        else:
            data.setdefault(date_iso, {})[email] = {
                "motif": motif_norm,
                "creneaux": creneaux,
                "maj": datetime.now().strftime("%d/%m/%Y %H:%M")}
        sauvegarder_changements(data)
    # Retour centré sur le jour modifié (ancre #j-date), pas en haut de page.
    return redirect(url_for(".vue", onglet="planning", date=date_iso,
                            _anchor=f"j-{date_iso}"))


@bp.route("/admin/planning-equipe/changement/supprimer", methods=["POST"])
def supprimer_changement():
    """Annule un changement ponctuel : le jour reprend les horaires de la trame."""
    if not _admin():
        return redirect(url_for("admin"))
    email = request.form.get("email", "")
    date_iso = request.form.get("date", "")
    data = charger_changements()
    if date_iso in data and email in data[date_iso]:
        del data[date_iso][email]
        if not data[date_iso]:
            del data[date_iso]
        sauvegarder_changements(data)
    if request.form.get("retour") == "changements":
        return redirect(url_for(".vue", onglet="changements", mois=request.form.get("mois", "")))
    return redirect(url_for(".vue", onglet="planning", date=date_iso,
                            _anchor=f"j-{date_iso}"))


@bp.route("/admin/planning-equipe/saisie-ponctuelle", methods=["POST"])
def saisie_ponctuelle():
    """Enregistre toute la grille « Horaires ponctuels » d'un jour en une fois :
    pour chaque collaborateur, les créneaux saisis surchargent la trame (ou la
    rétablissent si identiques / si le jour est laissé tel quel)."""
    if not _admin():
        return redirect(url_for("admin"))
    date_iso = request.form.get("date", "")
    try:
        d_obj = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except ValueError:
        d_obj = None
    if not d_obj:
        return redirect(url_for(".vue", onglet="planning"))
    act = trame_active_pour(charger_trames(), d_obj)
    fer = ferie_de(d_obj)
    data = charger_changements()
    absences = charger_absences()
    for em in request.form.getlist("email"):
        creneaux = []
        for s in (1, 2):
            deb = _norm_hhmm(request.form.get(f"h{s}d_{em}"))
            fin = _norm_hhmm(request.form.get(f"h{s}f_{em}"))
            if deb and fin:
                creneaux.append({"debut": deb, "fin": fin})
        creneaux.sort(key=lambda c: _minutes(c["debut"]) if _minutes(c["debut"]) is not None else 0)
        motif = request.form.get(f"motif_{em}", "")
        motif = motif if motif in MOTIFS else "Non catégorisé"
        cr_tr = creneaux_trame_jour(act, em, d_obj)
        present = date_iso in data and em in data.get(date_iso, {})
        # Retour à la trame (mêmes horaires) OU jour de repos laissé vide → pas de
        # changement. Sur un FÉRIÉ : vide = défaut (personne ne travaille) ; des
        # horaires, même de trame, notent la personne présente. Jour vidé déjà
        # couvert par une absence prolongée au même motif : doublon → rien.
        if (not creneaux and (fer or not cr_tr)) \
                or (not fer and meme_que_trame(creneaux, cr_tr)) \
                or ponctuel_redondant(absences, em, d_obj, motif, creneaux):
            if present:
                del data[date_iso][em]
        else:
            data.setdefault(date_iso, {})[em] = {
                "motif": motif, "creneaux": creneaux,
                "maj": datetime.now().strftime("%d/%m/%Y %H:%M")}
    if date_iso in data and not data[date_iso]:
        del data[date_iso]
    sauvegarder_changements(data)
    return redirect(url_for(".vue", onglet="planning", ponctuel=1, date=date_iso))


@bp.route("/admin/planning-equipe/absence", methods=["POST"])
def ajouter_absence():
    """Déclare une absence prolongée (plage de dates) pour un collaborateur."""
    if not _admin():
        return redirect(url_for("admin"))
    email = request.form.get("email", "")
    debut = request.form.get("debut", "")
    fin = request.form.get("fin", "") or debut
    motif = request.form.get("motif", "")
    commentaire = (request.form.get("commentaire", "") or "").strip()
    # Validation des dates ; on remet dans l'ordre si inversées.
    try:
        d1 = datetime.strptime(debut, "%Y-%m-%d").date()
        d2 = datetime.strptime(fin, "%Y-%m-%d").date()
    except ValueError:
        d1 = d2 = None
    if email and d1 and d2:
        if d2 < d1:
            d1, d2 = d2, d1
        absences = charger_absences()
        absences.append({
            "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "email": email, "debut": d1.isoformat(), "fin": d2.isoformat(),
            "motif": motif if motif in MOTIFS else "Non catégorisé",
            "commentaire": commentaire})
        sauvegarder_absences(absences)
    return redirect(url_for(".vue", onglet="planning", absence=1))


@bp.route("/admin/planning-equipe/absence/supprimer", methods=["POST"])
def supprimer_absence():
    """Supprime une absence prolongée par son id."""
    if not _admin():
        return redirect(url_for("admin"))
    aid = request.form.get("id", "")
    absences = [a for a in charger_absences() if a.get("id") != aid]
    sauvegarder_absences(absences)
    if request.form.get("retour") == "changements":
        return redirect(url_for(".vue", onglet="changements", mois=request.form.get("mois", "")))
    return redirect(url_for(".vue", onglet="planning", absence=1))


@bp.route("/admin/planning-equipe/permission", methods=["POST"])
def set_permission():
    """Modifie directement la permission d'un collaborateur (depuis sa carte)."""
    if not _admin():
        return redirect(url_for("admin"))
    email = request.form.get("email", "")
    perm = request.form.get("permission_planning", "")
    profils = charger_profils()
    prof = profils.get(email, {})
    if perm in PERMISSIONS:
        prof["permission_planning"] = perm
        profils[email] = prof
        sauvegarder_profils(profils)
    return redirect(url_for(".vue", onglet="equipe", edit=request.form.get("edit", "") or None))


@bp.route("/admin/planning-equipe/equipe-collab", methods=["POST"])
def enregistrer_collab_equipe():
    """Enregistre la fonction + la couleur (planning) d'UN collaborateur."""
    if not _admin():
        return redirect(url_for("admin"))
    email = request.form.get("email", "")
    fonc = request.form.get("poste", "")
    coul = (request.form.get("couleur_planning", "") or "").strip()
    auto = request.form.get("auto_couleur")
    profils = charger_profils()
    prof = profils.get(email, {})
    # Écrit dans le champ unique `poste` ; un ancien intitulé hors liste reste
    # accepté tel quel (option « (ancien intitulé) » du menu déroulant).
    if fonc in FONCTIONS or fonc == poste_de(prof) or not fonc:
        prof["poste"] = fonc
        prof.pop("fonction_planning", None)
    perm = request.form.get("permission_planning", "")
    vue = request.form.get("vue_planning", "")
    prof["permission_planning"] = perm if perm in PERMISSIONS else "Pas d'accès"
    prof["vue_planning"] = vue if vue in VUES_PLANNING else "Le planning de tous"
    # couleur libre (#RRGGBB) via le color picker, ou « auto » si la case est cochée.
    if auto or not coul:
        prof["couleur_planning"] = ""
    elif len(coul) == 7 and coul[0] == "#" and all(ch in "0123456789abcdefABCDEF" for ch in coul[1:]):
        prof["couleur_planning"] = coul.upper()
    else:
        prof["couleur_planning"] = ""
    profils[email] = prof
    sauvegarder_profils(profils)
    return redirect(url_for(".vue", onglet="equipe", edit=email, msg="equipe_ok"))


@bp.route("/admin/planning-equipe/equipe", methods=["POST"])
def enregistrer_equipe():
    """Enregistre fonction + couleur (planning) de chaque collaborateur dans son profil."""
    if not _admin():
        return redirect(url_for("admin"))
    profils = charger_profils()
    for e in charger_employes():
        email = e["email"]
        fonc = request.form.get(f"fonc__{email}", "")
        coul = request.form.get(f"coul__{email}", "")
        prof = profils.get(email, {})
        if fonc in FONCTIONS or not fonc:
            prof["poste"] = fonc
            prof.pop("fonction_planning", None)
        prof["couleur_planning"] = coul if coul in PALETTE_PLANNING else ""
        profils[email] = prof
    sauvegarder_profils(profils)
    return redirect(url_for(".vue", onglet="equipe", msg="equipe_ok"))


@bp.route("/admin/planning-equipe/ordre", methods=["POST"])
def deplacer_collaborateur():
    """Monte/descend un collaborateur dans l'ordre d'affichage (frise + saisie)."""
    if not _admin():
        return redirect(url_for("admin"))
    email = request.form.get("email", "")
    sens = request.form.get("sens", "")
    tid = request.form.get("tid", "")
    sem = request.form.get("sem", "A")
    data = charger_trames()
    trame = trame_par_id(data, tid)
    if trame:
        memb = membres_ordonnes(trame, charger_employes())
        if email in memb:
            i = memb.index(email)
            j = i - 1 if sens == "up" else i + 1
            if 0 <= j < len(memb):
                memb[i], memb[j] = memb[j], memb[i]
        trame["membres"] = memb
        sauvegarder_trames(data)
    return redirect(url_for(".vue", onglet="trame", trame=tid, sem=sem))


@bp.route("/admin/planning-equipe/reordonner", methods=["POST"])
def reordonner():
    """Réordonne les membres de la trame (glisser-déposer). Reçoit la liste ordonnée
    des e-mails en JSON ; persiste trame['membres'] dans cet ordre."""
    if not _admin():
        return current_app.response_class('{"error":"non autorisé"}', status=403,
                                          mimetype="application/json")
    import json as _json
    payload = request.get_json(force=True, silent=True) or {}
    tid = payload.get("tid", "")
    emails = payload.get("emails", []) or []
    data = charger_trames()
    trame = trame_par_id(data, tid)
    if trame:
        current = membres_ordonnes(trame, charger_employes())
        cur = set(current)
        neworder = [em for em in emails if em in cur]      # seulement des membres valides
        for em in current:                                  # complète si oubli
            if em not in neworder:
                neworder.append(em)
        trame["membres"] = neworder
        sauvegarder_trames(data)
    return current_app.response_class(_json.dumps({"status": "ok"}), mimetype="application/json")


@bp.route("/admin/planning-equipe/trame-collab", methods=["POST"])
def toggle_collab_trame():
    """Retire / ajoute un collaborateur à la trame (inclusion par trame)."""
    if not _admin():
        return redirect(url_for("admin"))
    tid = request.form.get("tid", "")
    email = request.form.get("email", "")
    action = request.form.get("action", "")
    sem = request.form.get("sem", "A")
    data = charger_trames()
    trame = trame_par_id(data, tid)
    if trame:
        memb = membres_ordonnes(trame, charger_employes())
        if action == "retirer":
            memb = [m for m in memb if m != email]
        elif action == "ajouter" and email not in memb:
            memb.append(email)
        trame["membres"] = memb
        sauvegarder_trames(data)
    return redirect(url_for(".vue", onglet="trame", trame=tid, sem=sem))


def _parse_horaires_form(form):
    """Lit toutes les semaines de rotation depuis le formulaire de l'éditeur Horaires
    -> {sem: {jour: [creneaux]}}."""
    emp_data = {}
    for s in SEMAINES:
        jours = {}
        for j in range(1, 8):
            creneaux = []
            for slot in range(2):
                base = f"h__{s}__{j}__{slot}__"
                deb = _norm_hhmm(form.get(base + "debut"))
                fin = _norm_hhmm(form.get(base + "fin"))
                if deb and fin:
                    creneaux.append({"debut": deb, "fin": fin})
            creneaux.sort(key=lambda c: _minutes(c["debut"]) if _minutes(c["debut"]) is not None else 0)
            jours[str(j)] = creneaux
        emp_data[s] = jours
    return emp_data


@bp.route("/admin/planning-equipe/horaires-collab", methods=["POST"])
def enregistrer_horaires_collab():
    """Enregistre TOUTES les semaines de rotation (A, B…) d'UN collaborateur."""
    if not _admin():
        return redirect(url_for("admin"))
    tid = request.form.get("tid", "")
    email = request.form.get("email", "")
    data = charger_trames()
    trame = trame_par_id(data, tid)
    if not trame:
        return redirect(url_for(".vue", onglet="trame", msg="no_trame"))
    trame.setdefault("employes", {})[email] = _parse_horaires_form(request.form)
    trame["maj"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    sauvegarder_trames(data)
    # Retour direct à la page Trame (vue d'équipe) après validation.
    return redirect(url_for(".vue", onglet="trame", trame=tid, msg="horaires_ok"))


@bp.route("/admin/planning-equipe/copier-semaine", methods=["POST"])
def copier_semaine():
    """Enregistre ce qui est saisi PUIS recopie une semaine de rotation sur une autre
    (ex. A -> B), et persiste — en un clic, sans validation séparée."""
    if not _admin():
        return redirect(url_for("admin"))
    import copy
    tid = request.form.get("tid", "")
    email = request.form.get("email", "")
    data = charger_trames()
    trame = trame_par_id(data, tid)
    if not trame:
        return redirect(url_for(".vue", onglet="trame", msg="no_trame"))
    emp_data = _parse_horaires_form(request.form)
    sens = request.form.get("copier_sens", "")     # ex. "A:B"
    if ":" in sens:
        src, dst = sens.split(":", 1)
        if src in emp_data and dst in emp_data:
            emp_data[dst] = copy.deepcopy(emp_data[src])
    trame.setdefault("employes", {})[email] = emp_data
    trame["maj"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    sauvegarder_trames(data)
    return redirect(url_for(".vue", onglet="trame", trame=tid, horaires=email, msg="copie_sem_ok"))


@bp.route("/admin/planning-equipe/imprimer-frise")
def imprimer_frise():
    """Page imprimable de la frise complète (toute l'équipe) pour une semaine.
    Avec ?date=AAAA-MM-JJ : imprime la semaine RÉELLE telle qu'affichée au
    planning (changements ponctuels, absences, fériés, options d'affichage).
    Sans date : aperçu de la trame brute (usage onglet Trame), comme avant."""
    if not _admin():
        return redirect(url_for("admin"))
    data = charger_trames()
    profils = charger_profils()
    employes_tous = charger_employes()
    couleurs = couleurs_map(employes_tous, profils)
    emap = {e["email"]: e for e in employes_tous}
    try:
        lundi = _lundi(datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date())
    except (ValueError, TypeError):
        lundi = None
    if lundi:
        act = trame_active_pour(data, lundi)
        if not act:
            abort(404)
        opts = charger_options()
        changements = charger_changements()
        absences = charger_absences()
        masques = set(opts.get("collaborateurs_masques", []))
        rot = semaine_rotation(act, lundi)
        emp_inclus = [emap[em] for em in membres_semaine(act, employes_tous, profils, lundi)
                      if em not in masques]
        if opts.get("lignes_vides") == "masquer":
            emp_inclus = [e for e in emp_inclus
                          if total_semaine(_jours_sem(act, e["email"], rot)) > 0]
        jours_aff = {j for j in range(1, 8)
                     if str(j) in opts.get("jours", []) or not opts.get("jours")}
        frise = _frise(act, rot, emp_inclus, couleurs, jours_aff,
                       opts.get("horaires_grille") != "masquer",
                       lundi, changements, absences,
                       masquer_vides=opts.get("lignes_vides") == "masquer",
                       masquer_fermes=True)
        titre = (f"Planning du {lundi.strftime('%d/%m')} au "
                 f"{(lundi + timedelta(days=6)).strftime('%d/%m/%Y')} · Semaine {rot}")
        return render_template("planning_frise_impr.html", frise=frise, trame=act,
                               sem=rot, titre=titre, reel=True)
    trame = (trame_par_id(data, request.args.get("tid", ""))
             or trame_active_pour(data, date.today())
             or trame_selectionnee(data))
    if not trame:
        abort(404)
    sem = request.args.get("sem", "A")
    if sem not in SEMAINES:
        sem = "A"
    emp_inclus = [emap[em] for em in membres_ordonnes(trame, employes_tous)]
    frise = _frise(trame, sem, emp_inclus, couleurs)
    return render_template("planning_frise_impr.html", frise=frise, trame=trame, sem=sem)


@bp.route("/admin/planning-equipe/imprimer-collaborateur")
def imprimer_collaborateur():
    """Fiche imprimable d'un collaborateur : ses horaires sur les semaines A/B."""
    if not _admin():
        return redirect(url_for("admin"))
    data = charger_trames()
    trame = trame_par_id(data, request.args.get("tid", "")) or trame_selectionnee(data)
    if not trame:
        abort(404)
    email = request.args.get("email", "")
    emp = next((e for e in charger_employes() if e["email"] == email), None)
    if not emp:
        abort(404)
    couleur = couleurs_map(charger_employes(), charger_profils()).get(email, "#888")
    fmt = request.args.get("format", "grille")
    semaines = []
    for s in SEMAINES:
        info = {"sem": s, "total": total_semaine(_jours_sem(trame, email, s))}
        if fmt == "grille":
            info["frise"] = _frise_solo(trame, s, email, couleur)   # rendu Gantt
        else:
            info["jours"] = [{"nom": JOURS_NOMS[j],
                              "creneaux": [c for c in _jours_sem(trame, email, s).get(str(j), [])
                                           if creneau_valide(c)]} for j in range(1, 8)]
        semaines.append(info)
    return render_template("planning_collaborateur_impr.html", emp=emp, couleur=couleur,
                           semaines=semaines, trame=trame, fmt=fmt)


@bp.route("/admin/planning-equipe/ouverture", methods=["POST"])
def enregistrer_ouverture():
    """Horaires d'ouverture de la pharmacie (par trame) : 2 plages/jour, vide = fermé."""
    if not _admin():
        return redirect(url_for("admin"))
    tid = request.form.get("tid", "")
    data = charger_trames()
    trame = trame_par_id(data, tid)
    if not trame:
        return redirect(url_for(".vue", onglet="trame", msg="no_trame"))
    horaires = {}
    for j in range(1, 8):
        plages = []
        for r in range(2):
            deb = _norm_hhmm(request.form.get(f"ouv__{j}__{r}__debut"))
            fin = _norm_hhmm(request.form.get(f"ouv__{j}__{r}__fin"))
            if deb and fin:
                plages.append([deb, fin])
        horaires[str(j)] = plages
    trame["horaires_ouverture"] = horaires
    sauvegarder_trames(data)
    return redirect(url_for(".vue", onglet="trame", trame=tid, msg="ouv_ok"))


@bp.route("/admin/planning-equipe/config", methods=["POST"])
def enregistrer_config():
    if not _admin():
        return redirect(url_for("admin"))
    tid = request.form.get("tid", "")
    data = charger_trames()
    trame = trame_par_id(data, tid)
    if not trame:
        return redirect(url_for(".vue", onglet="trame", msg="no_trame"))
    trame["date_demarrage"] = (request.form.get("date_demarrage") or "").strip()
    trame["commentaire"] = (request.form.get("commentaire") or "").strip()
    trame["semaine_demarrage"] = request.form.get("semaine_demarrage", "A")
    try:
        trame["nb_semaines"] = max(1, min(15, int(request.form.get("nb_semaines", 2))))
    except ValueError:
        trame["nb_semaines"] = 2
    sauvegarder_trames(data)
    return redirect(url_for(".vue", onglet="trame", trame=tid, msg="config_ok"))
