"""Surveillance du site botRh — vérifie chaque jour que le site répond, et
envoie une alerte à l'admin s'il est hors-ligne (ex. expiration mensuelle
PythonAnywhere oubliée). N'envoie un mail QUE si le site est en panne.

Lancé par GitHub Actions (le runner a un accès Internet ; il peut envoyer du
SMTP, contrairement au serveur).
"""
import os
import smtplib
import urllib.request
from email.mime.text import MIMEText

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
CLE = os.environ.get("API_CLE", "botRh-trigger-2026")
ADMIN_EMAIL = "pharmanantu@gmail.com"

ok = False
detail = ""
try:
    with urllib.request.urlopen(f"{BASE_URL}/healthcheck?cle={CLE}", timeout=25) as r:
        contenu = r.read().decode("utf-8", "ignore")
    # Marqueur prouvant que c'est bien l'app qui répond (pas une page d'erreur PA)
    ok = '"gmail_user_set"' in contenu
    detail = f"HTTP {r.status} mais réponse inattendue" if not ok else "OK"
except Exception as e:
    detail = f"{type(e).__name__}: {e}"

if ok:
    print("Site en ligne, rien à signaler.")
    raise SystemExit(0)

print(f"Site injoignable ({detail}) — envoi de l'alerte.")
gmail_user = os.environ["GMAIL_USER"]
gmail_pwd = os.environ["GMAIL_APP_PASSWORD"]
corps = (
    f"⚠️ Le site botRh ne répond pas correctement.\n\n"
    f"URL : {BASE_URL}\n"
    f"Détail : {detail}\n\n"
    f"ACTION : connectez-vous sur https://www.pythonanywhere.com (compte "
    f"pharmacie92000) → onglet Web → bouton « Run until 1 month from today » "
    f"pour réactiver le site.\n\n"
    f"Tant que le site est hors-ligne, les employés ne peuvent pas remplir "
    f"leur relevé et les relances/récap échouent.\n\n"
    f"(Vérification automatique quotidienne — botRh)"
)
msg = MIMEText(corps, "plain", "utf-8")
msg["From"] = gmail_user
msg["To"] = ADMIN_EMAIL
msg["Subject"] = "⚠️ botRh — le site est hors-ligne"
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(gmail_user, gmail_pwd)
    server.sendmail(gmail_user, ADMIN_EMAIL, msg.as_string())
print("Alerte envoyée à l'admin.")
