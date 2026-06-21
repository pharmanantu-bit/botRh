import json
import os
import csv
import io
import hashlib
from datetime import datetime
from flask import Flask, request, render_template, abort, redirect, url_for, session, send_file
from werkzeug.utils import secure_filename
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# Dossier du projet — sert d'ancrage pour tous les chemins de fichiers, car
# sous le serveur WSGI (PythonAnywhere) le répertoire courant n'est pas celui
# du projet : sans ça, employees.csv, documents/, reponses_*.json sont introuvables.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Configuration sensible chargée depuis le .env (avec valeurs par défaut pour
# ne rien casser). Pour durcir la sécurité, définir ces clés dans le .env du
# serveur — en priorité ADMIN_PASSWORD avec un mot de passe fort.
from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "botRh-admin-2026")

SECRET = os.getenv("TOKEN_SECRET", "pharmacie-nanterre-2026")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "pharma92")
API_CLE = os.getenv("API_CLE", "botRh-trigger-2026")
# employees.csv (versionné) = liste de départ ; employees_live.csv (gitignore)
# = liste gérée par l'admin sur le serveur. On lit le live s'il existe, et c'est
# lui qui fait foi (évite tout conflit de déploiement avec git).
EMPLOYEES_FILE = os.path.join(BASE_DIR, "employees.csv")
EMPLOYEES_LIVE = os.path.join(BASE_DIR, "employees_live.csv")

def employees_path(pour_ecriture=False):
    if pour_ecriture:
        return EMPLOYEES_LIVE
    return EMPLOYEES_LIVE if os.path.exists(EMPLOYEES_LIVE) else EMPLOYEES_FILE

def reponses_file(mois=None, annee=None):
    if mois is None:
        mois = datetime.now().month
    if annee is None:
        annee = datetime.now().year
    return os.path.join(BASE_DIR, f"reponses_{mois}_{annee}.json")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "planning_img")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

MOIS_FR = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"
}


from tokens import generer_token, resoudre_employe, reponse_de


def charger_reponses(mois=None, annee=None):
    f = reponses_file(mois, annee)
    if os.path.exists(f):
        with open(f, encoding="utf-8") as fp:
            return json.load(fp)
    return {}


def sauvegarder_reponse(token, data):
    f = reponses_file()
    reponses = charger_reponses()
    reponses[token] = data
    with open(f, "w", encoding="utf-8") as fp:
        json.dump(reponses, fp, ensure_ascii=False, indent=2)


@app.route("/releve")
def formulaire():
    token = request.args.get("token", "")
    prenom = request.args.get("prenom", "")
    if not token or not prenom:
        abort(404)

    import calendar
    mois = datetime.now().month
    annee = datetime.now().year
    mois_annee = f"{MOIS_FR[mois]} {annee}"

    mois_prec = 12 if mois == 1 else mois - 1
    annee_prec = annee - 1 if mois == 1 else annee
    nb_jours_prec = calendar.monthrange(annee_prec, mois_prec)[1]
    nb_jours_mois = calendar.monthrange(annee, mois)[1]

    jours_prec = list(range(24, nb_jours_prec + 1))
    jours_mois = list(range(1, nb_jours_mois + 1))

    JOURS_FR = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
    from datetime import date as dt_date

    noms_jours_prec = {j: JOURS_FR[dt_date(annee_prec, mois_prec, j).weekday()] for j in jours_prec}
    noms_jours_mois = {j: JOURS_FR[dt_date(annee, mois, j).weekday()] for j in jours_mois}
    weekend_prec = {j for j in jours_prec if dt_date(annee_prec, mois_prec, j).weekday() >= 5}
    weekend_mois = {j for j in jours_mois if dt_date(annee, mois, j).weekday() >= 5}

    reponses = charger_reponses()
    emp = resoudre_employe(token, charger_employes())
    token_canon = generer_token(emp["prenom"], emp["email"]) if emp else token
    deja_rempli = (reponse_de(reponses, emp["prenom"], emp["email"]) is not None) if emp else (token_canon in reponses)
    modifiable = datetime.now().day <= 25

    return render_template("form.html",
        prenom=prenom,
        token=token,
        mois_annee=mois_annee,
        mois_nom=MOIS_FR[mois].upper(),
        mois_prec_nom=MOIS_FR[mois_prec].upper(),
        jours_prec=jours_prec,
        jours_mois=jours_mois,
        noms_jours_prec=noms_jours_prec,
        noms_jours_mois=noms_jours_mois,
        weekend_prec=weekend_prec,
        weekend_mois=weekend_mois,
        deja_rempli=deja_rempli,
        modifiable=modifiable
    )


@app.route("/envoyer", methods=["POST"])
def envoyer():
    token = request.form.get("token", "")
    prenom = request.form.get("prenom", "")
    heures_plus = request.form.get("heures_plus", "0")
    heures_moins = request.form.get("heures_moins", "0")
    commentaire = request.form.get("commentaire", "")
    date_signature = request.form.get("date_signature", "")
    signature = request.form.get("signature", "")

    if not token or not prenom or not date_signature or not signature:
        abort(400)

    # Valider le jeton et identifier l'employé (bloque les soumissions forgées),
    # puis stocker sous le jeton canonique (nouveau) pour des recherches fiables.
    emp = resoudre_employe(token, charger_employes())
    if not emp:
        abort(403)
    if datetime.now().day > 25:
        return ("La période de saisie de ce mois est clôturée (date limite : le 25). "
                "Vos heures effectuées seront comptabilisées le mois suivant."), 403
    prenom = emp["prenom"]
    token = generer_token(prenom, emp["email"])

    mois = datetime.now().month
    annee = datetime.now().year

    sauvegarder_reponse(token, {
        "prenom": prenom,
        "heures_plus": float(heures_plus or 0),
        "heures_moins": float(heures_moins or 0),
        "commentaire": commentaire,
        "date_signature": date_signature,
        "signature": signature,
        "date": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "mois": mois,
        "annee": annee,
    })

    try:
        notifier_releve({
            "prenom": prenom,
            "email": emp["email"],
            "heures_plus": heures_plus,
            "heures_moins": heures_moins,
            "commentaire": commentaire,
            "date_signature": date_signature,
            "signature": signature,
            "date": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "mois": mois,
            "annee": annee,
        })
    except Exception:
        pass

    return render_template("merci.html", prenom=prenom,
        heures_plus=heures_plus, heures_moins=heures_moins,
        mois_annee=f"{MOIS_FR[mois]} {annee}"
    )


def planning_file():
    mois = datetime.now().month
    annee = datetime.now().year
    return os.path.join(BASE_DIR, f"planning_{mois}_{annee}.json")

def charger_planning():
    f = planning_file()
    if os.path.exists(f):
        with open(f, encoding="utf-8") as fp:
            return json.load(fp)
    return {}

def sauvegarder_planning(data):
    with open(planning_file(), "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)

def hm_to_float(s):
    s = s.strip().replace("min", "").replace(" ", "")
    if "h" in s:
        parts = s.split("h")
        h = float(parts[0] or 0)
        m = float(parts[1] or 0) if len(parts) > 1 else 0
        return round(h + m / 60, 2)
    return 0.0

def charger_employes():
    employes = []
    chemin = employees_path()
    if os.path.exists(chemin):
        with open(chemin, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                employes.append({"prenom": row["prenom"], "nom": row["nom"], "email": row["email"]})
    return employes


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        mdp = request.form.get("password", "")
        if mdp == ADMIN_PASSWORD:
            session["admin"] = True
        else:
            return render_template("admin_login.html", erreur=True)

    if not session.get("admin"):
        return render_template("admin_login.html", erreur=False)

    reponses = charger_reponses()
    employes = charger_employes()
    planning = charger_planning()
    mois = datetime.now().month
    annee = datetime.now().year

    resultats = []
    for emp in employes:
        token = generer_token(emp["prenom"], emp["email"])
        reponse = reponse_de(reponses, emp["prenom"], emp["email"])
        plan = planning.get(emp["prenom"], {})
        plan_total = plan.get("total", None)

        if reponse:
            solde = round(reponse["heures_plus"] - reponse["heures_moins"], 2)
            if plan_total is not None:
                ecart = round(solde - plan_total, 2)
                if abs(ecart) <= 0.5:
                    statut_comp = "OK"
                else:
                    statut_comp = "A VERIFIER"
            else:
                ecart = None
                statut_comp = None
        else:
            solde = None
            ecart = None
            statut_comp = None

        resultats.append({
            "prenom": emp["prenom"],
            "nom": emp["nom"],
            "email": emp["email"],
            "repondu": reponse is not None,
            "heures_plus": reponse["heures_plus"] if reponse else "-",
            "heures_moins": reponse["heures_moins"] if reponse else "-",
            "solde": solde if solde is not None else "-",
            "commentaire": reponse.get("commentaire", "") if reponse else "",
            "date": reponse["date"] if reponse else "-",
            "plan_trame": plan.get("trame", "-"),
            "plan_total": plan_total if plan_total is not None else "-",
            "plan_absences": plan.get("absences", ""),
            "plan_jours": plan.get("jours", "-"),
            "ecart": ecart if ecart is not None else "-",
            "statut_comp": statut_comp,
            "lien": f"/releve?token={token}&prenom={emp['prenom']}",
        })

    repondus = sum(1 for r in resultats if r["repondu"])
    a_planifier = charger_planning() != {}
    return render_template("admin.html", resultats=resultats, repondus=repondus,
                           total=len(resultats), mois_annee=f"{MOIS_FR[mois]} {annee}",
                           a_planifier=a_planifier)


def sauvegarder_employes(employes):
    with open(employees_path(pour_ecriture=True), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["nom", "prenom", "email"])
        writer.writeheader()
        writer.writerows(employes)


@app.route("/admin/employes", methods=["GET", "POST"])
def admin_employes():
    if not session.get("admin"):
        return redirect(url_for("admin"))

    employes = charger_employes()
    message = None

    if request.method == "POST":
        action = request.form.get("action")

        if action == "ajouter":
            prenom = request.form.get("prenom", "").strip()
            nom = request.form.get("nom", "").strip()
            email = request.form.get("email", "").strip()
            if prenom and nom and email:
                employes.append({"prenom": prenom, "nom": nom, "email": email})
                sauvegarder_employes(employes)
                message = f"{prenom} {nom} ajouté."

        elif action == "supprimer":
            email = request.form.get("email", "")
            employes = [e for e in employes if e["email"] != email]
            sauvegarder_employes(employes)
            message = "Employé supprimé."

        elif action == "modifier":
            email_orig = request.form.get("email_orig", "")
            for e in employes:
                if e["email"] == email_orig:
                    e["prenom"] = request.form.get("prenom", e["prenom"]).strip()
                    e["nom"] = request.form.get("nom", e["nom"]).strip()
                    e["email"] = request.form.get("email", e["email"]).strip()
                    break
            sauvegarder_employes(employes)
            message = "Employé modifié."

        employes = charger_employes()

    return render_template("admin_employes.html", employes=employes, message=message)


@app.route("/mon-espace")
def mon_espace():
    token = request.args.get("token", "")
    prenom = request.args.get("prenom", "")
    if not token or not prenom:
        abort(404)

    emp = resoudre_employe(token, charger_employes())
    token_canon = generer_token(emp["prenom"], emp["email"]) if emp else token

    historique = []
    for fichier in sorted(os.listdir(BASE_DIR), reverse=True):
        if fichier.startswith("reponses_") and fichier.endswith(".json"):
            parts = fichier.replace("reponses_","").replace(".json","").split("_")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                m, a = int(parts[0]), int(parts[1])
                with open(os.path.join(BASE_DIR, fichier), encoding="utf-8") as fp:
                    reponses = json.load(fp)
                r = reponse_de(reponses, emp["prenom"], emp["email"]) if emp else reponses.get(token)
                if r:
                    solde = round(r["heures_plus"] - r["heures_moins"], 2)
                    historique.append({
                        "mois_annee": f"{MOIS_FR[m]} {a}",
                        "heures_plus": r["heures_plus"],
                        "heures_moins": r["heures_moins"],
                        "solde": solde,
                        "commentaire": r.get("commentaire", ""),
                        "date": r["date"],
                        "lien": f"/releve?token={token}&prenom={prenom}",
                    })

    return render_template("mon_espace.html", prenom=prenom, token=token, historique=historique)


@app.route("/admin/absences")
def admin_absences():
    if not session.get("admin"):
        return redirect(url_for("admin"))

    employes = charger_employes()
    annee_courante = datetime.now().year
    annee = int(request.args.get("annee", annee_courante))
    mois_actuel = datetime.now().month if annee == annee_courante else 12

    annees_dispo = set()
    for fichier in os.listdir(BASE_DIR):
        if fichier.startswith("reponses_") and fichier.endswith(".json"):
            parts = fichier.replace("reponses_","").replace(".json","").split("_")
            if len(parts) == 2 and parts[1].isdigit():
                annees_dispo.add(int(parts[1]))
    annees_dispo.add(annee_courante)
    annees_dispo = sorted(annees_dispo, reverse=True)

    mois_disponibles = []
    stats = {}  # {prenom: {mois: h_moins}}

    for emp in employes:
        stats[emp["prenom"]] = {}

    for m in range(1, mois_actuel + 1):
        f = reponses_file(m, annee)
        if os.path.exists(f):
            with open(f, encoding="utf-8") as fp:
                reponses = json.load(fp)
            mois_disponibles.append(m)
            for emp in employes:
                token = generer_token(emp["prenom"], emp["email"])
                r = reponse_de(reponses, emp["prenom"], emp["email"])
                stats[emp["prenom"]][m] = round(r["heures_moins"], 2) if r else None

    # Calcul cumul H- et classement
    classement = []
    for emp in employes:
        p = emp["prenom"]
        total_moins = sum(stats[p][m] for m in mois_disponibles if stats[p].get(m) is not None)
        mois_max = None
        val_max = 0
        for m in mois_disponibles:
            v = stats[p].get(m)
            if v and v > val_max:
                val_max = v
                mois_max = m
        classement.append({
            "prenom": p,
            "nom": emp["nom"],
            "total": round(total_moins, 2),
            "mois_max": MOIS_FR[mois_max] if mois_max else "-",
            "val_max": val_max,
            "mois_data": [stats[p].get(m, 0) or 0 for m in mois_disponibles],
        })

    classement.sort(key=lambda x: x["total"], reverse=True)

    return render_template("admin_absences.html",
        classement=classement,
        mois_disponibles=mois_disponibles,
        mois_noms={m: MOIS_FR[m][:3] for m in range(1, 13)},
        annee=annee,
        annees_dispo=annees_dispo,
    )


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin"))

    employes = charger_employes()
    annee_courante = datetime.now().year
    annee = int(request.args.get("annee", annee_courante))
    mois_actuel = datetime.now().month if annee == annee_courante else 12

    # Trouver toutes les années disponibles
    annees_dispo = set()
    for fichier in os.listdir(BASE_DIR):
        if fichier.startswith("reponses_") and fichier.endswith(".json"):
            parts = fichier.replace("reponses_","").replace(".json","").split("_")
            if len(parts) == 2 and parts[1].isdigit():
                annees_dispo.add(int(parts[1]))
    annees_dispo.add(annee_courante)
    annees_dispo = sorted(annees_dispo, reverse=True)

    # Charger toutes les réponses de l'année
    donnees = {}  # {prenom: {mois: {h+, h-}}}
    mois_disponibles = []

    for m in range(1, mois_actuel + 1):
        f = reponses_file(m, annee)
        if os.path.exists(f):
            with open(f, encoding="utf-8") as fp:
                reponses = json.load(fp)
            mois_disponibles.append(m)
            for emp in employes:
                token = generer_token(emp["prenom"], emp["email"])
                r = reponse_de(reponses, emp["prenom"], emp["email"])
                if emp["prenom"] not in donnees:
                    donnees[emp["prenom"]] = {}
                if r:
                    donnees[emp["prenom"]][m] = {
                        "plus": r["heures_plus"],
                        "moins": r["heures_moins"],
                        "solde": round(r["heures_plus"] - r["heures_moins"], 2),
                    }
                else:
                    donnees[emp["prenom"]][m] = None

    # Calcul cumul annuel par employé
    cumuls = {}
    for emp in employes:
        p = emp["prenom"]
        total_plus = sum(donnees[p][m]["plus"] for m in mois_disponibles if donnees.get(p, {}).get(m))
        total_moins = sum(donnees[p][m]["moins"] for m in mois_disponibles if donnees.get(p, {}).get(m))
        cumuls[p] = {
            "plus": round(total_plus, 2),
            "moins": round(total_moins, 2),
            "solde": round(total_plus - total_moins, 2),
        }

    # Classement absentéisme
    classement_abs = []
    for emp in employes:
        p = emp["prenom"]
        total_moins = sum(donnees[p][m]["moins"] for m in mois_disponibles if donnees.get(p, {}).get(m))
        mois_max = None
        val_max = 0
        for m in mois_disponibles:
            d = donnees[p].get(m)
            if d and d["moins"] > val_max:
                val_max = d["moins"]
                mois_max = m
        classement_abs.append({
            "prenom": p,
            "nom": emp["nom"],
            "total": round(total_moins, 2),
            "mois_max": MOIS_FR[mois_max] if mois_max else "-",
            "val_max": val_max,
            "mois_data": [(donnees[p].get(m) or {}).get("moins", 0) or 0 for m in mois_disponibles],
        })
    classement_abs.sort(key=lambda x: x["total"], reverse=True)

    return render_template("admin_dashboard.html",
        employes=employes,
        donnees=donnees,
        cumuls=cumuls,
        mois_disponibles=mois_disponibles,
        mois_noms={m: MOIS_FR[m][:3] for m in range(1, 13)},
        annee=annee,
        annees_dispo=annees_dispo,
        classement_abs=classement_abs,
    )


@app.route("/admin/historique")
def admin_historique():
    if not session.get("admin"):
        return redirect(url_for("admin"))

    employes = charger_employes()
    nb_total = len(employes)
    historique = []

    for fichier in sorted(os.listdir(BASE_DIR), reverse=True):
        if fichier.startswith("reponses_") and fichier.endswith(".json"):
            parts = fichier.replace("reponses_", "").replace(".json", "").split("_")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                m, a = int(parts[0]), int(parts[1])
                with open(os.path.join(BASE_DIR, fichier), encoding="utf-8") as f:
                    data = json.load(f)
                historique.append({
                    "mois": m,
                    "annee": a,
                    "label": f"{MOIS_FR[m]} {a}",
                    "repondus": len(data),
                    "total": nb_total,
                    "fichier": f"{m}_{a}",
                })

    return render_template("admin_historique.html", historique=historique)


@app.route("/admin/historique/<int:mois>/<int:annee>")
def admin_historique_mois(mois, annee):
    if not session.get("admin"):
        return redirect(url_for("admin"))

    reponses = charger_reponses(mois, annee)
    employes = charger_employes()
    mois_annee = f"{MOIS_FR[mois]} {annee}"

    resultats = []
    for emp in employes:
        token = generer_token(emp["prenom"], emp["email"])
        reponse = reponse_de(reponses, emp["prenom"], emp["email"])
        resultats.append({
            "prenom": emp["prenom"],
            "nom": emp["nom"],
            "repondu": reponse is not None,
            "heures_plus": reponse["heures_plus"] if reponse else "-",
            "heures_moins": reponse["heures_moins"] if reponse else "-",
            "commentaire": reponse.get("commentaire", "") if reponse else "",
            "signature": reponse.get("signature", "") if reponse else "",
            "date": reponse["date"] if reponse else "-",
        })

    repondus = sum(1 for r in resultats if r["repondu"])
    return render_template("admin_historique_mois.html", resultats=resultats,
                           repondus=repondus, total=len(resultats),
                           mois_annee=mois_annee, mois=mois, annee=annee)


@app.route("/admin/historique/export/<int:mois>/<int:annee>")
def admin_historique_export(mois, annee):
    if not session.get("admin"):
        return redirect(url_for("admin"))

    reponses = charger_reponses(mois, annee)
    employes = charger_employes()
    mois_annee = f"{MOIS_FR[mois]} {annee}"

    wb = Workbook()
    ws = wb.active
    ws.title = f"Relevés {mois_annee}"
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells("A1:I1")
    ws["A1"] = f"Relevés d'heures — {mois_annee}"
    ws["A1"].font = Font(bold=True, size=13, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="1F4E79")
    ws["A1"].alignment = Alignment(horizontal="center")

    headers = ["Prénom", "Nom", "H+", "H−", "Signature", "Date signature", "Commentaire", "Date envoi", "Statut"]
    ws.append(headers)
    for cell in ws[2]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2E75B6")
        cell.alignment = Alignment(horizontal="center")
        cell.border = border

    for emp in employes:
        token = generer_token(emp["prenom"], emp["email"])
        r = reponse_de(reponses, emp["prenom"], emp["email"])
        if r:
            row = [emp["prenom"], emp["nom"], r["heures_plus"], r["heures_moins"],
                   r.get("signature", ""), r.get("date_signature", ""), r.get("commentaire", ""), r["date"], "Reçu"]
            fill = PatternFill("solid", fgColor="C6EFCE")
        else:
            row = [emp["prenom"], emp["nom"], "-", "-", "", "", "", "-", "En attente"]
            fill = PatternFill("solid", fgColor="FFEB9C")
        ws.append(row)
        for cell in ws[ws.max_row]:
            cell.border = border
            cell.alignment = Alignment(horizontal="center")
        ws[ws.max_row][8].fill = fill

    for i, w in enumerate([14, 16, 8, 8, 20, 14, 30, 16, 12], 1):
        ws.column_dimensions[chr(64+i)].width = w

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"Releves_{MOIS_FR[mois]}_{annee}.xlsx"
    return send_file(output, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/planning", methods=["GET", "POST"])
def admin_planning():
    if not session.get("admin"):
        return redirect(url_for("admin"))

    employes = charger_employes()
    planning = charger_planning()
    mois = datetime.now().month
    annee = datetime.now().year

    img_name = f"planning_{mois}_{annee}.png"
    img_path = os.path.join(UPLOAD_FOLDER, img_name)
    img_url = f"/static/planning_img/{img_name}" if os.path.exists(img_path) else None

    if request.method == "POST":
        # Sauvegarde image si uploadée
        if "image" in request.files:
            img = request.files["image"]
            if img and img.filename:
                img.save(img_path)
                img_url = f"/static/planning_img/{img_name}"

        nouveau = {}
        for emp in employes:
            p = emp["prenom"]
            trame = request.form.get(f"trame_{p}", "").strip()
            total = request.form.get(f"total_{p}", "").strip()
            absences = request.form.get(f"absences_{p}", "").strip()
            jours = request.form.get(f"jours_{p}", "").strip()
            nouveau[p] = {
                "trame": hm_to_float(trame) if trame else 0,
                "total": hm_to_float(total) if total else 0,
                "absences": absences,
                "jours": int(jours) if jours.isdigit() else 0,
            }
        sauvegarder_planning(nouveau)
        return redirect(url_for("admin"))

    return render_template("admin_planning.html", employes=employes, planning=planning,
                           mois_annee=f"{MOIS_FR[mois]} {annee}", img_url=img_url)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin"))


def construire_recap_xlsx(mois, annee):
    """Construit le classeur Excel récapitulatif des relevés du mois donné."""
    reponses = charger_reponses(mois, annee)
    employes = charger_employes()
    mois_annee = f"{MOIS_FR[mois]} {annee}"

    wb = Workbook()
    ws = wb.active
    ws.title = f"Relevés {mois_annee}"

    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells("A1:G1")
    ws["A1"] = f"Relevés d'heures — {mois_annee}"
    ws["A1"].font = Font(bold=True, size=13, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="1F4E79")
    ws["A1"].alignment = Alignment(horizontal="center")

    headers = ["Prénom", "Nom", "H+", "H−", "Signature", "Date signature", "Commentaire", "Date envoi", "Statut"]
    ws.append(headers)
    for cell in ws[2]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2E75B6")
        cell.alignment = Alignment(horizontal="center")
        cell.border = border

    for emp in employes:
        token = generer_token(emp["prenom"], emp["email"])
        r = reponse_de(reponses, emp["prenom"], emp["email"])
        if r:
            statut = "Reçu"
            fill = PatternFill("solid", fgColor="C6EFCE")
            row = [emp["prenom"], emp["nom"], r["heures_plus"], r["heures_moins"], r.get("signature", ""), r.get("date_signature", ""), r.get("commentaire", ""), r["date"], statut]
        else:
            statut = "En attente"
            fill = PatternFill("solid", fgColor="FFEB9C")
            row = [emp["prenom"], emp["nom"], "-", "-", "", "", "", "-", statut]
        ws.append(row)
        for cell in ws[ws.max_row]:
            cell.border = border
            cell.alignment = Alignment(horizontal="center")
        ws[ws.max_row][6].fill = fill

    for i, w in enumerate([14, 16, 8, 8, 20, 14, 30, 16, 12], 1):
        ws.column_dimensions[chr(64+i)].width = w

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.route("/admin/export")
def admin_export():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    mois = datetime.now().month
    annee = datetime.now().year
    output = construire_recap_xlsx(mois, annee)
    return send_file(output, as_attachment=True,
                     download_name=f"Releves_{MOIS_FR[mois]}_{annee}.xlsx", mimetype=XLSX_MIME)


@app.route("/export_recap")
def export_recap():
    """Récap Excel des relevés du mois (pour l'envoi paie automatique par le
    runner GitHub). Clé requise."""
    cle = request.args.get("cle", "")
    if cle != API_CLE:
        abort(403)
    mois = int(request.args.get("mois", datetime.now().month))
    annee = int(request.args.get("annee", datetime.now().year))
    output = construire_recap_xlsx(mois, annee)
    return send_file(output, as_attachment=True,
                     download_name=f"Releves_{MOIS_FR[mois]}_{annee}.xlsx", mimetype=XLSX_MIME)


def envoyer_confirmation(sujet, message):
    import smtplib
    from email.mime.text import MIMEText
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    gmail_user = os.getenv("GMAIL_USER")
    gmail_pwd = os.getenv("GMAIL_APP_PASSWORD")
    msg = MIMEText(message, "plain", "utf-8")
    msg["From"] = gmail_user
    msg["To"] = "pharmanantu@gmail.com"
    msg["Subject"] = sujet
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pwd)
        server.sendmail(gmail_user, "pharmanantu@gmail.com", msg.as_string())


def notifier_releve(donnees):
    """Déclenche le workflow GitHub Actions 'nouveau_releve' qui génère le PDF
    du relevé et l'envoie à l'admin. Le serveur gratuit ne pouvant pas faire de
    SMTP, l'envoi est délégué au runner GitHub. Nécessite GITHUB_TOKEN dans le .env."""
    import urllib.request
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    gh_token = os.getenv("GITHUB_TOKEN")
    if not gh_token:
        return
    url = "https://api.github.com/repos/pharmanantu-bit/botRh/dispatches"
    body = json.dumps({
        "event_type": "nouveau_releve",
        "client_payload": donnees,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {gh_token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "botRh")
    urllib.request.urlopen(req, timeout=15)


@app.route("/deploy")
def deploy():
    import subprocess
    cle = request.args.get("cle", "")
    if cle != "botRh-deploy-2026":
        abort(403)
    try:
        result = subprocess.run(["git", "pull"], cwd="/home/pharmacie92000/botRh",
                                capture_output=True, text=True, timeout=30)
        # Toucher le fichier WSGI force le reload sur PythonAnywhere
        import pathlib
        pathlib.Path("/var/www/pharmacie92000_pythonanywhere_com_wsgi.py").touch()
        return f"OK\n{result.stdout}", 200
    except Exception as e:
        return f"Erreur: {e}", 500


@app.route("/trigger")
def trigger():
    cle = request.args.get("cle", "")
    if cle != API_CLE:
        abort(403)

    jour = datetime.now().day
    mois = datetime.now().month
    annee = datetime.now().year
    mois_annee = f"{MOIS_FR[mois]} {annee}"

    if jour == 20:
        from email_sender import send_emails, load_employees
        send_emails()
        nb = len(load_employees())
        envoyer_confirmation(
            f"botRh — Relevés {mois_annee} envoyés",
            f"Les relevés d'heures de {mois_annee} ont bien été envoyés à {nb} employés.\n\nConsulte l'admin : https://pharmacie92000.pythonanywhere.com/admin"
        )
        return "Envoi relevés OK", 200
    elif jour == 22:
        from relance_sender import send_relances, load_employees
        a_relancer = send_relances()
        nb_total = len(load_employees())
        nb_relances = len(a_relancer)
        if nb_relances == 0:
            msg_conf = f"Tous les employés ont répondu — aucune relance envoyée pour {mois_annee}."
        else:
            noms = ", ".join(e["prenom"] for e in a_relancer)
            msg_conf = f"{nb_relances}/{nb_total} relances envoyées pour {mois_annee}.\n\nEmployés relancés : {noms}\n\nConsulte l'admin : https://pharmacie92000.pythonanywhere.com/admin"
        envoyer_confirmation(f"botRh — Relances {mois_annee}", msg_conf)
        return "Envoi relances OK", 200
    else:
        return f"Rien à faire (jour {jour})", 200


@app.route("/export_reponses")
def export_reponses():
    """Renvoie les réponses (qui a rempli son relevé) du mois demandé, pour que
    les relances envoyées depuis GitHub Actions sachent qui relancer. Clé requise."""
    cle = request.args.get("cle", "")
    if cle != API_CLE:
        abort(403)
    mois = int(request.args.get("mois", datetime.now().month))
    annee = int(request.args.get("annee", datetime.now().year))
    f = reponses_file(mois, annee)
    data = {}
    if os.path.exists(f):
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
    return app.response_class(json.dumps(data, ensure_ascii=False),
                              mimetype="application/json")


@app.route("/export_employes")
def export_employes():
    """Renvoie la liste des employés gérée via l'admin, pour que les envois
    (GitHub Actions) utilisent toujours la liste à jour. Le serveur est la
    source unique de vérité. Clé requise."""
    cle = request.args.get("cle", "")
    if cle != API_CLE:
        abort(403)
    return app.response_class(json.dumps(charger_employes(), ensure_ascii=False),
                              mimetype="application/json")


@app.route("/healthcheck")
def healthcheck():
    """Diagnostic de l'état du serveur (config .env, employés, documents).
    Ne renvoie aucune valeur secrète, seulement des booléens/compteurs.
    Avec &smtp=1 : teste l'authentification Gmail sans envoyer d'e-mail."""
    import json as _json
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    cle = request.args.get("cle", "")
    if cle != API_CLE:
        abort(403)

    gmail_user = os.getenv("GMAIL_USER")
    gmail_pwd = os.getenv("GMAIL_APP_PASSWORD")
    info = {
        "gmail_user_set": bool(gmail_user),
        "gmail_pwd_set": bool(gmail_pwd) and not str(gmail_pwd).startswith("x"),
        "smtp_login_ok": None,
    }

    try:
        from email_sender import load_employees
        info["employees_count"] = len(load_employees())
    except Exception as e:
        info["employees_error"] = str(e)

    docs_dir = os.path.join(BASE_DIR, "documents")
    info["documents_count"] = (
        len([f for f in os.listdir(docs_dir) if f.endswith(".docx")])
        if os.path.isdir(docs_dir) else 0
    )

    info["github_token_set"] = bool(os.getenv("GITHUB_TOKEN"))
    if request.args.get("github") == "1":
        import urllib.request
        try:
            req = urllib.request.Request("https://api.github.com/", headers={"User-Agent": "botRh"})
            with urllib.request.urlopen(req, timeout=15) as r:
                info["github_reachable"] = (r.status == 200)
        except Exception as e:
            info["github_reachable"] = False
            info["github_error"] = str(e)

    if request.args.get("dispatch") == "1":
        import urllib.request, urllib.error
        gh_token = os.getenv("GITHUB_TOKEN")
        try:
            corps = json.dumps({
                "event_type": "nouveau_releve",
                "client_payload": {"prenom": "DIAG", "heures_plus": 0, "heures_moins": 0,
                                   "commentaire": "diagnostic dispatch", "date_signature": "",
                                   "signature": "", "date": "", "mois": datetime.now().month,
                                   "annee": datetime.now().year},
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.github.com/repos/pharmanantu-bit/botRh/dispatches",
                data=corps, method="POST")
            req.add_header("Authorization", f"Bearer {gh_token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", "botRh")
            with urllib.request.urlopen(req, timeout=15) as r:
                info["dispatch_status"] = r.status
        except urllib.error.HTTPError as e:
            info["dispatch_status"] = e.code
            info["dispatch_error"] = e.read().decode("utf-8", "ignore")[:300]
        except Exception as e:
            info["dispatch_error"] = str(e)

    if request.args.get("smtp") == "1" and info["gmail_user_set"] and info["gmail_pwd_set"]:
        import smtplib
        try:
            s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15)
            s.login(gmail_user, gmail_pwd)
            s.quit()
            info["smtp_login_ok"] = True
        except Exception as e:
            info["smtp_login_ok"] = False
            info["smtp_error"] = str(e)

    return app.response_class(_json.dumps(info, ensure_ascii=False, indent=2),
                              mimetype="application/json")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
