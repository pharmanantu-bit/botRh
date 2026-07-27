import smtplib
import os
import csv
import logging
import hashlib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from config import GMAIL_USER, GMAIL_APP_PASSWORD, EMPLOYEES_FILE, DOCUMENTS_FOLDER, LOGS_FOLDER

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
from tokens import generer_token
from signature_mail import SIGNATURE


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
            email = row.get("email", "").strip()
            if not email:
                continue
            employees.append({
                "nom": row.get("nom", "").strip(),
                "prenom": row.get("prenom", "").strip(),
                "email": email,
            })
    return employees


MOIS_FICHIER = {
    1: "Janvier", 2: "Fevrier", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Aout",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Decembre"
}

MOIS_FR = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"
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


def build_email(employee, documents, mois_annee, intro=None):
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = employee["email"]
    msg["Subject"] = f"Feuille d'heures — {mois_annee}"

    token = generer_token(employee['prenom'], employee['email'])
    lien = f"{BASE_URL}/releve?token={token}&prenom={employee['prenom']}"

    lien_espace = f"{BASE_URL}/mon-espace?token={token}&prenom={employee['prenom']}"

    bloc_intro = f"{intro}\n\n" if intro else ""
    mois_actuel = datetime.now().month
    mois_prec_nom = MOIS_FR[12 if mois_actuel == 1 else mois_actuel - 1]
    body = f"""Bonjour {employee['prenom']},

{bloc_intro}Merci de bien vouloir remplir votre feuille d'heures du mois de {mois_annee} en ligne, via le lien ci-dessous, au plus tard le 25 de ce mois.
Le relevé couvre la période du 26 {mois_prec_nom} au 25 {MOIS_FR[mois_actuel]} inclus (les heures effectuées après le 25 comptent pour le mois suivant).

Remplir votre relevé en ligne :
{lien}

Consulter votre historique de relevés :
{lien_espace}

Belle journée,

{SIGNATURE}
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


def send_accuse_candidature(prenom, email, poste=""):
    """Envoie au candidat l'accusé de réception + la mention d'information RGPD
    (finalité, base légale, durée de conservation, droits). Exécuté côté RUNNER
    (le serveur PythonAnywhere gratuit n'a pas d'Internet sortant). Renvoie True si
    l'envoi a réussi. Idempotence/traçabilité gérées par l'appelant."""
    email = (email or "").strip()
    if not email:
        return False
    import rgpd_recrutement
    msg = MIMEText(rgpd_recrutement.texte_information(prenom, poste), "plain", "utf-8")
    msg["From"] = GMAIL_USER
    msg["To"] = email
    msg["Subject"] = rgpd_recrutement.objet_information(poste)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, email, msg.as_string())
    return True


def send_emails(only_to=None, intro=None):
    """Envoie les relevés à tous les employés.
    only_to : si fourni (adresse e-mail), envoie un seul mail de test à cette
    adresse au lieu de toute la liste (utilisé pour valider le pipeline).
    intro : texte optionnel inséré en haut du mail (ex: annonce du nouveau système)."""
    setup_logging()
    MOIS_FR = {
        1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
        5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
        9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"
    }
    mois_annee = f"{MOIS_FR[datetime.now().month]} {datetime.now().year}"

    if only_to:
        employees = [{"nom": "Test", "prenom": "Test", "email": only_to}]
        logging.info(f"MODE TEST — envoi unique à {only_to}")
    else:
        employees = load_employees()

    # Aucune pièce jointe : l'employé remplit son relevé en ligne via le lien.
    documents = []

    logging.info(f"Début envoi — {len(employees)} employé(s), sans pièce jointe")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        for emp in employees:
            try:
                msg = build_email(emp, documents, mois_annee, intro=intro)
                server.sendmail(GMAIL_USER, emp["email"], msg.as_string())
                logging.info(f"Mail envoyé à {emp['prenom']} {emp['nom']} <{emp['email']}>")
            except Exception as e:
                logging.error(f"Échec envoi à {emp['email']}: {e}")

    logging.info("Envoi terminé.")


if __name__ == "__main__":
    send_emails()
