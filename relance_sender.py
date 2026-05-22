import smtplib
import os
import csv
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
CONTACTS_FILE = "contacts (3).csv"
LOGS_FOLDER = "logs"

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


def load_employees():
    employees = []
    with open(CONTACTS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("E-mail 1 - Value", "").strip()
            if not email:
                continue
            employees.append({
                "nom": row.get("Last Name", "").strip(),
                "prenom": row.get("First Name", "").strip(),
                "email": email,
            })
    return employees


def send_relances():
    setup_logging()
    mois_annee = f"{MOIS_FR[datetime.now().month]} {datetime.now().year}"
    employees = load_employees()

    logging.info(f"Envoi relances — {len(employees)} employé(s)")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        for emp in employees:
            try:
                msg = MIMEMultipart()
                msg["From"] = GMAIL_USER
                msg["To"] = emp["email"]
                msg["Subject"] = f"Rappel — Feuille d'heures {mois_annee} à retourner"

                body = f"""Bonjour {emp['prenom']},

Sauf erreur de notre part, nous n'avons pas encore reçu votre feuille d'heures du mois de {mois_annee}.

Merci de bien vouloir nous la retourner dès que possible.

Belle journée,
La direction
"""
                msg.attach(MIMEText(body, "plain", "utf-8"))
                server.sendmail(GMAIL_USER, emp["email"], msg.as_string())
                logging.info(f"Relance envoyée à {emp['prenom']} {emp['nom']} <{emp['email']}>")

            except Exception as e:
                logging.error(f"Échec relance à {emp['email']}: {e}")

    logging.info("Relances terminées.")


if __name__ == "__main__":
    send_relances()
