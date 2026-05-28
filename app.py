import json
import os
import csv
import io
import hashlib
from datetime import datetime
from flask import Flask, request, render_template, abort, redirect, url_for, session, send_file
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

app = Flask(__name__)
app.secret_key = "botRh-admin-2026"

SECRET = "pharmacie-nanterre-2026"
ADMIN_PASSWORD = "pharma92"
REPONSES_FILE = "reponses_web.json"
EMPLOYEES_FILE = "employees.csv"

MOIS_FR = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"
}


def generer_token(prenom, email):
    chaine = f"{prenom}{email}{SECRET}"
    return hashlib.md5(chaine.encode()).hexdigest()[:10]


def charger_reponses():
    if os.path.exists(REPONSES_FILE):
        with open(REPONSES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def sauvegarder_reponse(token, data):
    reponses = charger_reponses()
    reponses[token] = data
    with open(REPONSES_FILE, "w", encoding="utf-8") as f:
        json.dump(reponses, f, ensure_ascii=False, indent=2)


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

    reponses = charger_reponses()
    deja_rempli = token in reponses

    return render_template("form.html",
        prenom=prenom,
        token=token,
        mois_annee=mois_annee,
        mois_nom=MOIS_FR[mois].upper(),
        mois_prec_nom=MOIS_FR[mois_prec].upper(),
        jours_prec=jours_prec,
        jours_mois=jours_mois,
        deja_rempli=deja_rempli
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

    return render_template("merci.html", prenom=prenom,
        heures_plus=heures_plus, heures_moins=heures_moins,
        mois_annee=f"{MOIS_FR[mois]} {annee}"
    )


def charger_employes():
    employes = []
    if os.path.exists(EMPLOYEES_FILE):
        with open(EMPLOYEES_FILE, newline="", encoding="utf-8") as f:
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
    mois = datetime.now().month
    annee = datetime.now().year

    resultats = []
    for emp in employes:
        token = generer_token(emp["prenom"], emp["email"])
        reponse = reponses.get(token)
        resultats.append({
            "prenom": emp["prenom"],
            "nom": emp["nom"],
            "email": emp["email"],
            "repondu": reponse is not None,
            "heures_plus": reponse["heures_plus"] if reponse else "-",
            "heures_moins": reponse["heures_moins"] if reponse else "-",
            "commentaire": reponse.get("commentaire", "") if reponse else "",
            "date": reponse["date"] if reponse else "-",
            "lien": f"/releve?token={token}&prenom={emp['prenom']}",
        })

    repondus = sum(1 for r in resultats if r["repondu"])
    return render_template("admin.html", resultats=resultats, repondus=repondus,
                           total=len(resultats), mois_annee=f"{MOIS_FR[mois]} {annee}")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin"))


@app.route("/admin/export")
def admin_export():
    if not session.get("admin"):
        return redirect(url_for("admin"))

    reponses = charger_reponses()
    employes = charger_employes()
    mois = datetime.now().month
    annee = datetime.now().year
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
        r = reponses.get(token)
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
    filename = f"Releves_{MOIS_FR[mois]}_{annee}.xlsx"
    return send_file(output, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/trigger")
def trigger():
    cle = request.args.get("cle", "")
    if cle != "botRh-trigger-2026":
        abort(403)

    jour = datetime.now().day
    if jour == 20:
        from email_sender import send_emails
        send_emails()
        return "Envoi relevés OK", 200
    elif jour == 22:
        from relance_sender import send_relances
        send_relances()
        return "Envoi relances OK", 200
    else:
        return f"Rien à faire (jour {jour})", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
