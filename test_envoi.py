import smtplib
import os
import hashlib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
TEST_EMAIL = "pharmanantu@gmail.com"
BASE_URL = "https://pharmacie92000.pythonanywhere.com"
SECRET = "pharmacie-nanterre-2026"

def generer_token(prenom, email):
    chaine = f"{prenom}{email}{SECRET}"
    return hashlib.md5(chaine.encode()).hexdigest()[:10]

prenom = "Test"
token = generer_token(prenom, TEST_EMAIL)
lien = f"{BASE_URL}/releve?token={token}&prenom={prenom}"

msg = MIMEMultipart()
msg["From"] = GMAIL_USER
msg["To"] = TEST_EMAIL
msg["Subject"] = "TEST — Relevé mensuel des heures Mai 2026"

body = f"""Bonjour,

Ceci est un mail de test du bot botRh.

Vous pouvez remplir votre relevé en ligne via ce lien personnalisé :
{lien}

Cordialement,
La direction
"""
msg.attach(MIMEText(body, "plain", "utf-8"))

print(f"Envoi à {TEST_EMAIL}...")
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    server.sendmail(GMAIL_USER, TEST_EMAIL, msg.as_string())

print("Mail envoyé avec succès !")
print(f"Lien généré : {lien}")
