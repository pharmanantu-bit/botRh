import smtplib
import os
import csv
import json
import hashlib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from dotenv import load_dotenv

from signature_mail import SIGNATURE

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
EMPLOYEES_FILE = os.path.join(BASE_DIR, "employees.csv")
def reponses_file():
    now = datetime.now()
    return os.path.join(BASE_DIR, f"reponses_{now.month}_{now.year}.json")
LOGS_FOLDER = os.path.join(BASE_DIR, "logs")
SECRET = os.getenv("TOKEN_SECRET", "pharmacie-nanterre-2026")
BASE_URL = "https://pharmacie92000.pythonanywhere.com"

MOIS_FR = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"
}


def setup_logging():
    os.makedirs(LOGS_FOLDER, exist_ok=True)
    log_file = os.path.join(LOGS_FOLDER, f"relance_{datetime.now().strftime('%Y_%m')}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


from tokens import generer_token, reponse_de


def load_employees():
    employees = []
    with open(EMPLOYEES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            employees.append({
                "nom": row["nom"].strip(),
                "prenom": row["prenom"].strip(),
                "email": row["email"].strip(),
            })
    return employees


def charger_reponses():
    f = reponses_file()
    if os.path.exists(f):
        with open(f, encoding="utf-8") as fp:
            return json.load(fp)
    return {}


def send_relances():
    setup_logging()
    mois_annee = f"{MOIS_FR[datetime.now().month]} {datetime.now().year}"
    jours_restants = max(0, 25 - datetime.now().day)
    employees = load_employees()
    reponses = charger_reponses()

    # Filtrer uniquement ceux qui n'ont pas encore répondu
    a_relancer = []
    for emp in employees:
        if reponse_de(reponses, emp["prenom"], emp["email"]) is None:
            a_relancer.append(emp)

    logging.info(f"Relances ciblées — {len(a_relancer)}/{len(employees)} employé(s) n'ont pas répondu")

    if not a_relancer:
        logging.info("Tous les employés ont répondu, aucune relance envoyée.")
        return a_relancer

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        for emp in a_relancer:
            try:
                token = generer_token(emp["prenom"], emp["email"])
                lien = f"{BASE_URL}/releve?token={token}&prenom={emp['prenom']}"

                msg = MIMEMultipart()
                msg["From"] = GMAIL_USER
                msg["To"] = emp["email"]
                if jours_restants <= 0:
                    urgence = "C'est le dernier jour : la saisie est clôturée le 25."
                    sujet_delai = "dernier jour"
                elif jours_restants == 1:
                    urgence = "⏰ Plus qu'un jour : à remplir avant le 25 (clôture demain)."
                    sujet_delai = "plus qu'1 jour"
                else:
                    urgence = f"⏰ Il vous reste {jours_restants} jours : à remplir avant le 25."
                    sujet_delai = f"plus que {jours_restants} jours"

                msg["Subject"] = f"Rappel ({sujet_delai}) — Feuille d'heures {mois_annee}"

                body = f"""Bonjour {emp['prenom']},

Sauf erreur de notre part, nous n'avons pas encore reçu votre feuille d'heures du mois de {mois_annee}.

{urgence}

Remplissez-la en quelques minutes en ligne via ce lien :
{lien}

Merci d'avance,

{SIGNATURE}
"""
                msg.attach(MIMEText(body, "plain", "utf-8"))
                server.sendmail(GMAIL_USER, emp["email"], msg.as_string())
                logging.info(f"Relance envoyée à {emp['prenom']} {emp['nom']} <{emp['email']}>")

            except Exception as e:
                logging.error(f"Échec relance à {emp['email']}: {e}")

    logging.info("Relances terminées.")
    return a_relancer


if __name__ == "__main__":
    send_relances()
