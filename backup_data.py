"""Sauvegarde automatique des données botRh — exécuté par GitHub Actions.

Récupère toutes les données du serveur (employés, relevés, planning), les
compresse en ZIP et les envoie par mail à l'admin (dans sa propre boîte =
copie sûre et récupérable, indépendante du serveur PythonAnywhere).
"""
import os
import io
import zipfile
import smtplib
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
CLE = os.environ.get("API_CLE", "botRh-trigger-2026")
ADMIN_EMAIL = "pharmanantu@gmail.com"

horodatage = datetime.now().strftime("%Y-%m-%d")

# 1) Récupérer toutes les données du serveur
with urllib.request.urlopen(f"{BASE_URL}/export_backup?cle={CLE}", timeout=60) as r:
    donnees = r.read()  # JSON (bytes)

# 2) Compresser en ZIP
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr(f"botRh_sauvegarde_{horodatage}.json", donnees)
zip_bytes = buf.getvalue()
print(f"Sauvegarde compressée : {len(zip_bytes)} octets.")

# 3) Envoyer par mail à l'admin
gmail_user = os.environ["GMAIL_USER"]
gmail_pwd = os.environ["GMAIL_APP_PASSWORD"]
msg = MIMEMultipart()
msg["From"] = gmail_user
msg["To"] = ADMIN_EMAIL
msg["Subject"] = f"[Sauvegarde botRh] {horodatage}"
msg.attach(MIMEText(
    "Sauvegarde automatique des données botRh (employés, relevés, planning, "
    "dossiers RH et index des documents).\n\n"
    "Conservez cet e-mail : en cas de problème serveur, le fichier ZIP joint "
    "permet de restaurer les données.\n\n"
    "(Sauvegarde quotidienne automatique — botRh)", "plain", "utf-8"))
part = MIMEBase("application", "zip")
part.set_payload(zip_bytes)
encoders.encode_base64(part)
part.add_header("Content-Disposition", f'attachment; filename="botRh_sauvegarde_{horodatage}.zip"')
msg.attach(part)
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(gmail_user, gmail_pwd)
    server.sendmail(gmail_user, ADMIN_EMAIL, msg.as_string())
print(f"Sauvegarde envoyée à {ADMIN_EMAIL}.")
