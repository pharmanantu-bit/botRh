import json
import os
import hashlib
from datetime import datetime
from flask import Flask, request, render_template, abort

app = Flask(__name__)

SECRET = "pharmacie-nanterre-2026"
REPONSES_FILE = "reponses_web.json"

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

    if not token or not prenom:
        abort(400)

    mois = datetime.now().month
    annee = datetime.now().year

    sauvegarder_reponse(token, {
        "prenom": prenom,
        "heures_plus": float(heures_plus or 0),
        "heures_moins": float(heures_moins or 0),
        "commentaire": commentaire,
        "date": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "mois": mois,
        "annee": annee,
    })

    return render_template("merci.html", prenom=prenom,
        heures_plus=heures_plus, heures_moins=heures_moins,
        mois_annee=f"{MOIS_FR[mois]} {annee}"
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
