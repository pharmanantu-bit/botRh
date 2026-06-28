"""Module PLANNING D'ÉQUIPE (Blueprint Flask) — façon Mon Planning Pharma.

UNE seule entrée de menu « Planning » ; tout le reste en sous-onglets internes
(?onglet=). Cœur 100 % local (aucun réseau) → marche sur PythonAnywhere gratuit.

Itération 1 : la TRAME (semaines tournantes A/B, saisie d'heures tapées) + la FRISE
colorée (rendu Gantt horizontal). Sous-onglets Effectifs / Changements / Totaux à venir.

Cf. docs/planning_specs.md pour le modèle complet.
"""
import os
import uuid
from datetime import datetime

from flask import (Blueprint, request, render_template, redirect, url_for,
                   session, abort, current_app)

from app import (_lire_json, _ecrire_json, BASE_DIR, charger_employes,
                 charger_profils, sauvegarder_profils, couleur_collaborateur,
                 PALETTE_PLANNING)

bp = Blueprint("planning_equipe", __name__)

TRAME_FILE = os.path.join(BASE_DIR, "planning_trame.json")

JOURS_NOMS = {1: "Lundi", 2: "Mardi", 3: "Mercredi", 4: "Jeudi",
              5: "Vendredi", 6: "Samedi", 7: "Dimanche"}
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


def _frise(trame, sem, employes, couleurs):
    horaires = trame.get("horaires_ouverture", HORAIRES_DEFAUT)
    amp_min, amp_max = _amplitude(horaires)
    span = amp_max - amp_min
    ticks = [{"label": f"{h}h", "left": round((h * 60 - amp_min) / span * 100, 2)}
             for h in range(amp_min // 60, amp_max // 60 + 1)]
    jours = []
    for j in range(1, 8):
        # plages d'ouverture (ombrage)
        ouv = []
        for p in horaires.get(str(j), []) or []:
            d, f = _minutes(p[0]), _minutes(p[1])
            if d is not None and f is not None and f > d:
                left, width = _pos(d, f, amp_min, span)
                ouv.append({"left": left, "width": width})
        lignes = []
        for e in employes:
            couleur = couleurs.get(e["email"], "#888")
            barres = []
            for c in _jours_sem(trame, e["email"], sem).get(str(j), []) or []:
                if creneau_valide(c):
                    d, f = _minutes(c["debut"]), _minutes(c["fin"])
                    left, width = _pos(d, f, amp_min, span)
                    barres.append({"left": left, "width": width, "couleur": couleur,
                                   "label": f"{c['debut']}–{c['fin']}"})
            lignes.append({"prenom": e["prenom"], "couleur": couleur, "barres": barres,
                           "total": total_jour(_jours_sem(trame, e["email"], sem).get(str(j), []))})
        jours.append({"iso": j, "nom": JOURS_NOMS[j], "ouverture": ouv, "lignes": lignes,
                      "ferme": not horaires.get(str(j))})
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

FONCTIONS = ["Pharmacien", "Préparateur", "Rayonniste", "Conseillère",
             "Étudiant pharmacie", "Apprentie", "Ménage", "Autre"]
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
    employes_base = charger_employes()
    couleurs = couleurs_map(employes_base, profils)        # couleurs stables
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
        memb = membres_ordonnes(act, employes_base)
        emp_frise = [emap[em] for em in memb]
        ctx["trame"] = act
        ctx["tid"] = act.get("id") if act else None
        ctx["frise"] = _frise(act, sem, emp_frise, couleurs) if (act and memb) else None
        ctx["pas_active"] = act is None
        return render_template("planning_equipe.html", **ctx)

    if onglet == "equipe":
        membres = []
        for e in employes_base:
            prof = profils.get(e["email"], {})
            membres.append({
                "email": e["email"], "prenom": e["prenom"], "nom": e["nom"],
                "fonction": prof.get("fonction_planning", ""),
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
    fonc = request.form.get("fonction_planning", "")
    coul = (request.form.get("couleur_planning", "") or "").strip()
    auto = request.form.get("auto_couleur")
    profils = charger_profils()
    prof = profils.get(email, {})
    prof["fonction_planning"] = fonc if fonc in FONCTIONS else ""
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
        prof["fonction_planning"] = fonc if fonc in FONCTIONS else ""
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
