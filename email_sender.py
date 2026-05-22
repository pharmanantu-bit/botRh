import smtplib
import os
import csv
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from config import GMAIL_USER, GMAIL_APP_PASSWORD, EMPLOYEES_FILE, DOCUMENTS_FOLDER, LOGS_FOLDER


def setup_logging():
    os.makedirs(LOGS_FOLDER, exist_ok=True)
    log_file = os.path.join(LOGS_FOLDER, f"envoi_{datetime.now().strftime('%Y_%m')}.log")
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
    with open(EMPLOYEES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("E-mail 1 - Value", "").strip()
            if not email:
                continue
            employees.append({
                "nom": row.get("Last Name", "").strip(),
                "prenom": row.get("First Name", "").strip(),
                "email": email,
                "poste": row.get("Organization Title", "").strip(),
            })
    return employees


MOIS_FICHIER = {
    1: "Janvier", 2: "Fevrier", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Aout",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Decembre"
}

def get_documents():
    mois = datetime.now().month
    annee = datetime.now().year
    nom = f"Releve_heures_{MOIS_FICHIER[mois]}_{annee}.docx"
    chemin = os.path.join(DOCUMENTS_FOLDER, nom)
    if os.path.isfile(chemin):
        return [chemin]
    logging.warning(f"Document introuvable : {chemin}")
    return []


def build_email(employee, documents, mois_annee):
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = employee["email"]
    msg["Subject"] = f"Feuille d'heures — {mois_annee}"

    body = f"""Bonjour {employee['prenom']},

Vous trouverez ci-joint la feuille d'heures du mois de {mois_annee}. Merci de bien vouloir la compléter et nous la retourner au plus tard le 25 de ce mois.

Belle journée,
La direction
"""
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for doc_path in documents:
        with open(doc_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        filename = os.path.basename(doc_path)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)

    return msg


def send_emails():
    setup_logging()
    MOIS_FR = {
        1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
        5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
        9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"
    }
    mois_annee = f"{MOIS_FR[datetime.now().month]} {datetime.now().year}"

    employees = load_employees()
    documents = get_documents()

    if not documents:
        logging.warning("Aucun document trouvé dans le dossier 'documents/'. Envoi annulé.")
        return

    logging.info(f"Début envoi — {len(employees)} employé(s), {len(documents)} document(s)")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        for emp in employees:
            try:
                msg = build_email(emp, documents, mois_annee)
                server.sendmail(GMAIL_USER, emp["email"], msg.as_string())
                logging.info(f"Mail envoyé à {emp['prenom']} {emp['nom']} <{emp['email']}>")
            except Exception as e:
                logging.error(f"Échec envoi à {emp['email']}: {e}")

    logging.info("Envoi terminé.")


if __name__ == "__main__":
    send_emails()
