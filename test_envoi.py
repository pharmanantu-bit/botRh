import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv

load_dotenv()

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
TEST_EMAIL = "pharmanantu@gmail.com"
DOCUMENTS_FOLDER = "documents"

msg = MIMEMultipart()
msg["From"] = GMAIL_USER
msg["To"] = TEST_EMAIL
msg["Subject"] = "TEST — Relevé mensuel des heures Mai 2026"

body = """Bonjour,

Ceci est un mail de test du bot d'envoi automatique.

Vous trouverez en pièce jointe le relevé mensuel des heures à compléter.

Cordialement,
La direction
"""
msg.attach(MIMEText(body, "plain", "utf-8"))

MOIS_FICHIER = {
    1: "Janvier", 2: "Fevrier", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Aout",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Decembre"
}
from datetime import datetime
mois = datetime.now().month
annee = datetime.now().year
filename = f"Releve_heures_{MOIS_FICHIER[mois]}_{annee}.docx"
filepath = os.path.join(DOCUMENTS_FOLDER, filename)
with open(filepath, "rb") as f:
    part = MIMEBase("application", "octet-stream")
    part.set_payload(f.read())
encoders.encode_base64(part)
part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
msg.attach(part)
print(f"Pièce jointe : {filename}")

print(f"Envoi à {TEST_EMAIL}...")
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    server.sendmail(GMAIL_USER, TEST_EMAIL, msg.as_string())

print("Mail envoyé avec succès !")
