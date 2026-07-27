"""Envoi du dossier paie mensuel à l'expert-comptable — exécuté par GitHub
Actions (repository_dispatch `envoi_comptable`, déclenché par le bouton
« Envoyer au comptable » de l'admin, une fois tous les relevés validés).

Le serveur PythonAnywhere gratuit ne peut pas envoyer d'e-mail : ce runner
récupère le résumé paie (/export_resume_paie : ventilation des heures sup
25 % / 50 % par semaine civile, régime CCN pharmacie d'officine) et le récap
Excel (/export_recap), puis envoie le tout à l'expert-comptable
(destinataires dans le payload), copie à l'admin. `construire_mail_comptable`
est pur pour être testable sans SMTP (pattern conges_sender)."""
import os
import json
import smtplib
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from signature_mail import SIGNATURE

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
CLE = os.environ.get("API_CLE") or "botRh-trigger-2026"
ADMIN_EMAIL = "pharmanantu@gmail.com"

MOIS_FR = {1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril", 5: "Mai", 6: "Juin",
           7: "Juillet", 8: "Août", 9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"}


def _fmt_h(v):
    """7.75 -> « 7h45 » ; 7.0 -> « 7h » (plus lisible pour le comptable)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    h = int(v)
    m = round((v - h) * 60)
    return f"{h}h{m:02d}" if m else f"{h}h"


def construire_mail_comptable(resume, mois_annee):
    """resume : liste par collaborateur produite par /export_resume_paie.
    Renvoie (sujet, corps). Tous les relevés transmis sont réputés VALIDÉS
    (le serveur ne déclenche l'envoi qu'à cette condition)."""
    lignes_collab, sans_releve = [], []
    tot25 = tot50 = totcomp = 0.0
    for it in resume:
        nom = f"{it['prenom']} {it['nom'].upper()}"
        if it.get("statut") == "manquant":
            sans_releve.append(nom)
            continue
        contrat = it.get("contrat_hebdo") or 0
        entete = f"  - {nom}" + (f" ({_fmt_h(contrat)}/sem)" if contrat else "")
        entete += f" : +{_fmt_h(it['plus'])} / -{_fmt_h(it['moins'])}"
        if it["statut"] == "ok":
            if contrat >= 35:
                entete += (f" → heures sup : {_fmt_h(it['sup25'])} à +25 %"
                           f", {_fmt_h(it['sup50'])} à +50 %")
                tot25 += it["sup25"]
                tot50 += it["sup50"]
            else:
                entete += (f" → {_fmt_h(it['complementaires'])} complémentaires"
                           " (majoration 10 % / 25 %)")
                totcomp += it["complementaires"]
        elif it["statut"] == "sans_contrat":
            entete += " → heures contractuelles non renseignées : ventilation à faire"
        else:  # sans_detail
            entete += (f" → solde net {'+' if it['solde'] >= 0 else ''}{it['solde']} h"
                       " (pas de détail par jour : ventilation 25/50 à faire)")
        extras = []
        if it.get("saisi_par_admin"):
            extras.append("saisi par la pharmacie")
        if it.get("corrige"):
            extras.append("corrigé par la pharmacie")
        if it.get("commentaire"):
            extras.append(f"commentaire : {it['commentaire']}")
        if extras:
            entete += f" [{' ; '.join(extras)}]"
        lignes_collab.append(entete)

    sujet = f"Relevés d'heures validés — {mois_annee} — Pharmacie Apothical Nanterre Université"
    lignes = [
        "Bonjour,",
        "",
        f"Veuillez trouver ci-joint le récapitulatif Excel des relevés d'heures de {mois_annee},",
        "vérifiés et validés, pour l'établissement des bulletins de paie.",
        "",
        f"Résumé par collaborateur ({len(lignes_collab)} relevé(s) validé(s)) :",
        *lignes_collab,
        "",
        f"Total équipe : {_fmt_h(tot25)} à +25 % · {_fmt_h(tot50)} à +50 %"
        + (f" · {_fmt_h(totcomp)} complémentaires" if totcomp else ""),
    ]
    if sans_releve:
        lignes += ["", f"Sans relevé ce mois ({len(sans_releve)}) : " + ", ".join(sans_releve)]
    lignes += [
        "",
        "NB : ventilation calculée par semaine civile sur la base des heures",
        "contractuelles ± heures déclarées jour par jour, selon le régime de la",
        "convention collective de la pharmacie d'officine (+25 % de la 36e à la",
        "43e heure hebdomadaire, +50 % au-delà ; heures complémentaires des",
        "temps partiels majorées 10 % / 25 %). Le détail jour par jour figure",
        "dans les relevés en cas de besoin.",
        "",
        "Nous restons à votre disposition,",
        "",
        SIGNATURE,
    ]
    return sujet, "\n".join(lignes)


def main():
    p = json.loads(os.environ.get("PAYLOAD") or "{}") or {}
    mois = int(p.get("mois") or datetime.now().month)
    annee = int(p.get("annee") or datetime.now().year)
    destinataires = [d.strip() for d in (p.get("destinataires") or "").split(",") if d.strip()]
    if not destinataires:
        print("Aucun destinataire comptable dans le payload — rien à envoyer.")
        return
    mois_annee = f"{MOIS_FR[mois]} {annee}"

    with urllib.request.urlopen(
            f"{BASE_URL}/export_resume_paie?cle={CLE}&mois={mois}&annee={annee}", timeout=30) as r:
        resume = json.loads(r.read().decode("utf-8"))
    with urllib.request.urlopen(
            f"{BASE_URL}/export_recap?cle={CLE}&mois={mois}&annee={annee}", timeout=60) as r:
        xlsx = r.read()

    sujet, corps = construire_mail_comptable(resume, mois_annee)

    gmail_user = os.environ["GMAIL_USER"]
    gmail_pwd = os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = ", ".join(destinataires)
    msg["Cc"] = ADMIN_EMAIL
    msg["Subject"] = sujet
    msg.attach(MIMEText(corps, "plain", "utf-8"))
    part = MIMEBase("application", "octet-stream")
    part.set_payload(xlsx)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition",
                    f'attachment; filename="Releves_{MOIS_FR[mois]}_{annee}.xlsx"')
    msg.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pwd)
        server.sendmail(gmail_user, destinataires + [ADMIN_EMAIL], msg.as_string())
    print(f"Dossier paie {mois_annee} envoyé à {', '.join(destinataires)} (copie admin).")


if __name__ == "__main__":
    main()
