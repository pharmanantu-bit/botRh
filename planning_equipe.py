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

from app import (_lire_json, _ecrire_json, BASE_DIR, charger_employes,
                 charger_profils, sauvegarder_profils, couleur_collaborateur,
                 collaborateur_actif, poste_de, POSTES, PALETTE_PLANNING)

bp = Blueprint("planning_equipe", __name__)

TRAME_FILE = os.path.join(BASE_DIR, "planning_trame.json")

JOURS_NOMS = {1: "Lundi", 2: "Mardi", 3: "Mercredi", 4: "Jeudi",
              5: "Vendredi", 6: "Samedi", 7: "Dimanche"}
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
          "Formation", "Autre"]


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
    for t in trames:
        if t.get("activee"):
            return t
    return trames[0] if trames else None


def _label_trame(t):
    base = t.get("date_demarrage") or t.get("cree_le") or "sans date"
    com = (t.get("commentaire") or "").strip()
    actif = " — Activée" if t.get("activee") else " — désactivée"
    return f"{base}{(' · ' + com) if com else ''}{actif}"


def couleurs_map(employes_base, profils):
    """{email: couleur} stable (index sur l'ordre d'origine, pas l'ordre d'affichage)."""
    return {e["email"]: couleur_collaborateur(profils.get(e["email"], {}), i)
            for i, e in enumerate(employes_base)}


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


def _jours_sem(trame, email, sem):
    return (trame.get("employes", {}).get(email, {}) or {}).get(sem, {}) or {}


def _creneaux_txt(trame, email, sem, j):
    """Horaires d'un jour en texte : « 09:00–13:00, 14:00–19:00 » (ou « repos »)."""
    cr = [c for c in _jours_sem(trame, email, sem).get(str(j), []) if creneau_valide(c)]
    return ", ".join(f"{c['debut']}–{c['fin']}" for c in cr) or "repos"


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


def _frise(trame, sem, employes, couleurs, jours_affiches=None, montrer_horaires=True,
           lundi_date=None, changements=None, absences=None):
    horaires = trame.get("horaires_ouverture", HORAIRES_DEFAUT)
    amp_min, amp_max = _amplitude(horaires)
    span = amp_max - amp_min
    ticks = [{"label": f"{h}h", "left": round((h * 60 - amp_min) / span * 100, 2)}
             for h in range(amp_min // 60, amp_max // 60 + 1)]
    jours = []
    for j in range(1, 8):
        if jours_affiches is not None and j not in jours_affiches:
            continue
        date_iso = (lundi_date + timedelta(days=j - 1)).isoformat() if lundi_date else ""
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
            if chg is not None and chg.get("creneaux") and meme_que_trame(chg.get("creneaux"), cr_trame):
                chg = None
            # Absence prolongée couvrant ce jour (le changement ponctuel reste prioritaire).
            abs_a = absence_active(absences or [], e["email"],
                                   lundi_date + timedelta(days=j - 1)) if (date_iso and chg is None) else None
            if chg is not None:
                cr_eff = chg.get("creneaux", []) or []
                motif = chg.get("motif", "")
                modifie = True
            elif abs_a is not None:
                cr_eff, motif, modifie = [], abs_a.get("motif", "Absence"), True
            else:
                cr_eff, motif, modifie = cr_trame, "", False
            barres = []
            for c in cr_eff:
                if creneau_valide(c):
                    d, f = _minutes(c["debut"]), _minutes(c["fin"])
                    left, width = _pos(d, f, amp_min, span)
                    barres.append({"left": left, "width": width, "couleur": couleur,
                                   "label": (f"{c['debut']}–{c['fin']}" if montrer_horaires else "")})
            lignes.append({"prenom": e["prenom"], "email": e["email"], "couleur": couleur,
                           "barres": barres, "total": total_jour(cr_eff),
                           "modifie": modifie, "motif": motif,
                           "creneaux": _pad2(cr_eff), "creneaux_trame": _pad2(cr_trame)})
        nom = JOURS_NOMS[j]
        if lundi_date:
            nom += " " + (lundi_date + timedelta(days=j - 1)).strftime("%d/%m")
        jours.append({"iso": j, "nom": nom, "date_iso": date_iso, "ouverture": ouv,
                      "lignes": lignes, "ferme": not horaires.get(str(j))})
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
           ("changements", "Changements"), ("totaux", "Totaux / Fin de mois")]
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
    # Seuls les collaborateurs ACTIFS apparaissent au planning (ni archivés ni
    # inactifs). Leurs heures de trame sont conservées en cas de réactivation.
    employes_base = [e for e in employes_tous
                     if collaborateur_actif(profils.get(e["email"], {}))]
    emap = {e["email"]: e for e in employes_base}

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
        # Le planning suit TOUJOURS la trame ACTIVE (pas la trame éditée dans l'onglet Trame).
        act = next((t for t in data.get("trames", []) if t.get("activee")), None)
        opts = charger_options()
        changements = charger_changements()
        absences = charger_absences()
        masques = set(opts.get("collaborateurs_masques", []))
        emp_base = [emap[em] for em in membres_ordonnes(act, employes_base) if em not in masques]
        jours_aff = [j for j in range(1, 8)
                     if str(j) in opts.get("jours", []) or not opts.get("jours")]
        montrer_h = opts.get("horaires_grille") != "masquer"
        mode = opts.get("mode", "grille")
        periode = opts.get("periode", "hebdo")
        # Date de référence pour la navigation réelle.
        try:
            ref = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            ref = date.today()
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
               "aujourdhui": url_for(".vue", onglet="planning", date=date.today().isoformat())}
        if periode == "hebdo":
            cur = _lundi(ref)
            for k in range(-2, 7):
                L = cur + timedelta(days=7 * k)
                nav["boutons"].append({"url": url_for(".vue", onglet="planning", date=L.isoformat()),
                                     "label": L.strftime("%d/%m"),
                                     "sub": ("Sem. " + semaine_rotation(act, L)) if act else "",
                                     "actif": L == cur})
        else:
            prem = ref.replace(day=1)
            for k in range(-3, 12):
                m = _ajoute_mois(prem, k)
                nav["boutons"].append({"url": url_for(".vue", onglet="planning", date=m.isoformat()),
                                     "label": f"{MOIS_ABBR[m.month]} {m.year % 100:02d}", "sub": "",
                                     "actif": (m.year, m.month) == (ref.year, ref.month)})
        # Vues (1 par semaine), rotation A/B calculée depuis la date.
        vues = []
        if act:
            for lundi in lundis:
                rot = semaine_rotation(act, lundi)
                emp_sm = emp_base
                if opts.get("lignes_vides") == "masquer":
                    emp_sm = [e for e in emp_base if total_semaine(_jours_sem(act, e["email"], rot)) > 0]
                fin = lundi + timedelta(days=6)
                titre = f"{lundi.strftime('%d/%m')} – {fin.strftime('%d/%m/%Y')} · Semaine {rot}"
                v = {"sem": rot, "titre": titre}
                if mode == "grille":
                    v["frise"] = _frise(act, rot, emp_sm, couleurs, set(jours_aff), montrer_h,
                                        lundi, changements, absences)
                elif mode == "texte":
                    v["texte"] = [{"prenom": e["prenom"], "couleur": couleurs[e["email"]],
                                   "jours": [{"nom": JOURS_NOMS[j] + " " + (lundi + timedelta(days=j - 1)).strftime("%d/%m"),
                                              "txt": _creneaux_txt(act, e["email"], rot, j)} for j in jours_aff]}
                                  for e in emp_sm]
                else:  # tableau
                    v["cols"] = [JOURS_NOMS[j] + " " + (lundi + timedelta(days=j - 1)).strftime("%d/%m") for j in jours_aff]
                    v["lignes"] = [{"prenom": e["prenom"], "couleur": couleurs[e["email"]],
                                    "cells": [_creneaux_txt(act, e["email"], rot, j) for j in jours_aff],
                                    "total": total_semaine(_jours_sem(act, e["email"], rot))} for e in emp_sm]
                vues.append(v)
        # Récapitulatif des changements ponctuels sur la période affichée.
        recap_chg = []
        if act:
            for lundi in lundis:
                for k in range(7):
                    dt = lundi + timedelta(days=k)
                    for em, ch in (changements.get(dt.isoformat(), {}) or {}).items():
                        if em not in emap:
                            continue
                        crs = ch.get("creneaux", []) or []
                        # Changement rétabli à la trame (horaires identiques) → pas une vraie
                        # modif : on ne l'affiche pas dans « Modifications apportées ».
                        if crs and meme_que_trame(crs, creneaux_trame_jour(act, em, dt)):
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
            for e in emp_base:
                cr_tr = creneaux_trame_jour(act, e["email"], ref)
                chg = changement_de(changements, ref.isoformat(), e["email"])
                abs_a = absence_active(absences, e["email"], ref) if chg is None else None
                if chg is not None:
                    cr_eff, motif_r = (chg.get("creneaux", []) or []), chg.get("motif", "Non catégorisé")
                elif abs_a is not None:
                    cr_eff, motif_r = [], abs_a.get("motif", "Non catégorisé")
                else:
                    cr_eff, motif_r = cr_tr, "Non catégorisé"
                p, pt = _pad2(cr_eff), _pad2(cr_tr)
                rows.append({"email": e["email"], "prenom": e["prenom"],
                             "couleur": couleurs.get(e["email"], "#888"),
                             "modifie": chg is not None or abs_a is not None,
                             "motif": motif_r,
                             "c1d": p[0]["debut"], "c1f": p[0]["fin"], "c2d": p[1]["debut"], "c2f": p[1]["fin"],
                             "t1d": pt[0]["debut"], "t1f": pt[0]["fin"], "t2d": pt[1]["debut"], "t2f": pt[1]["fin"],
                             "trame_txt": " · ".join(f'{c["debut"]}–{c["fin"]}' for c in cr_tr) or "repos"})
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
        ctx.update(trame=act, tid=act.get("id") if act else None, pas_active=act is None,
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
                    "Cordialement,\nLa pharmacie")
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
        act = next((t for t in data.get("trames", []) if t.get("activee")), None)

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
                    cr_tr = creneaux_trame_jour(act, em, d)
                    if meme_que_trame(crs, cr_tr):           # rétabli à la trame → pas une modif
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
    """Supprime DÉFINITIVEMENT une trame (et tous ses horaires)."""
    if not _admin():
        return redirect(url_for("admin"))
    tid = request.form.get("tid", "")
    data = charger_trames()
    data["trames"] = [t for t in data.get("trames", []) if t.get("id") != tid]
    sauvegarder_trames(data)
    return redirect(url_for(".vue", onglet="trame", msg="trame_suppr"))


@bp.route("/admin/planning-equipe/activer", methods=["POST"])
def toggle_trame():
    """Active / désactive une trame. Une seule trame active à la fois (l'active sert
    au planning) : activer une trame désactive les autres."""
    if not _admin():
        return redirect(url_for("admin"))
    tid = request.form.get("tid", "")
    data = charger_trames()
    t = trame_par_id(data, tid)
    if t:
        if not t.get("activee"):
            for x in data.get("trames", []):
                x["activee"] = (x.get("id") == tid)   # une seule active
            msg = "activee"
        else:
            t["activee"] = False
            msg = "desactivee"
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
    return redirect(url_for(".vue", onglet="options", msg="options_ok"))


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
        act = next((t for t in charger_trames().get("trames", []) if t.get("activee")), None)
        retour_trame = bool(creneaux) and d_obj is not None and \
            meme_que_trame(creneaux, creneaux_trame_jour(act, email, d_obj))
        if retour_trame:
            if date_iso in data and email in data[date_iso]:
                del data[date_iso][email]
                if not data[date_iso]:
                    del data[date_iso]
        else:
            data.setdefault(date_iso, {})[email] = {
                "motif": motif if motif in MOTIFS else "Non catégorisé",
                "creneaux": creneaux,
                "maj": datetime.now().strftime("%d/%m/%Y %H:%M")}
        sauvegarder_changements(data)
    return redirect(url_for(".vue", onglet="planning", date=date_iso))


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
    return redirect(url_for(".vue", onglet="planning", date=date_iso))


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
    act = next((t for t in charger_trames().get("trames", []) if t.get("activee")), None)
    data = charger_changements()
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
        # Retour à la trame (mêmes horaires) OU jour de repos laissé vide → pas de changement.
        if meme_que_trame(creneaux, cr_tr) or (not creneaux and not cr_tr):
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
    return redirect(url_for(".vue", onglet="trame", trame=tid, horaires=email, msg="horaires_ok"))


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
    """Page imprimable de la frise complète (toute l'équipe) pour une semaine."""
    if not _admin():
        return redirect(url_for("admin"))
    data = charger_trames()
    trame = (trame_par_id(data, request.args.get("tid", ""))
             or next((t for t in data.get("trames", []) if t.get("activee")), None)
             or trame_selectionnee(data))
    if not trame:
        abort(404)
    sem = request.args.get("sem", "A")
    if sem not in SEMAINES:
        sem = "A"
    employes_base = charger_employes()
    couleurs = couleurs_map(employes_base, charger_profils())
    emap = {e["email"]: e for e in employes_base}
    emp_inclus = [emap[em] for em in membres_ordonnes(trame, employes_base)]
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
