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
            employees.append(row)
    return employees


def get_documents():
    docs = []
    if os.path.isdir(DOCUMENTS_FOLDER):
        for filename in os.listdir(DOCUMENTS_FOLDER):
            if filename.endswith((".docx", ".doc", ".pdf")):
                docs.append(os.path.join(DOCUMENTS_FOLDER, filename))
    return docs


def build_email(employee, documents, mois_annee):
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = employee["email"]
    msg["Subject"] = f"Relevé mensuel des heures — {mois_annee}"

    body = f"""Bonjour {employee['prenom']},

Veuillez trouver en pièce jointe le document à compléter pour votre relevé mensuel des heures du mois de {mois_annee}.

Merci de le retourner complété dès que possible.

Cordialement,
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
    mois_annee = datetime.now().strftime("%B %Y")

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
