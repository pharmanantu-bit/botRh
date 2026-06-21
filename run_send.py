"""Orchestrateur d'envoi exécuté par GitHub Actions.

Le serveur PythonAnywhere gratuit bloque le SMTP sortant ; l'envoi des e-mails
est donc fait ici, sur le runner GitHub (qui a un accès Internet complet).
Le serveur ne sert plus qu'à héberger les pages web et à fournir les réponses.

Modes :
- auto      : relevés le 20, relances le 22, rien sinon (défaut planifié)
- test      : envoie un seul mail de test à l'admin (validation du pipeline)
- releves   : force l'envoi des relevés à tous
- relances  : force l'envoi des relances
"""
import sys
import json
import urllib.request
from datetime import datetime

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
CLE = "botRh-trigger-2026"
ADMIN_EMAIL = "pharmanantu@gmail.com"

mode = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "auto"
jour = datetime.now().day
if mode == "auto":
    mode = "releves" if jour == 20 else ("relances" if jour == 22 else "rien")

print(f"run_send.py — mode={mode} (jour du mois={jour})")

if mode == "test":
    from email_sender import send_emails
    send_emails(only_to=ADMIN_EMAIL)
    print(f"Mail de test envoyé à {ADMIN_EMAIL}.")

elif mode == "releves":
    from email_sender import send_emails
    send_emails()
    print("Relevés envoyés.")

elif mode == "relances":
    # Récupérer auprès du serveur la liste de ceux qui ont déjà répondu,
    # puis l'écrire localement pour que relance_sender ne relance que les autres.
    import relance_sender
    now = datetime.now()
    url = f"{BASE_URL}/export_reponses?cle={CLE}&mois={now.month}&annee={now.year}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = r.read().decode("utf-8")
        with open(relance_sender.reponses_file(), "w", encoding="utf-8") as f:
            f.write(data)
        print(f"Réponses récupérées du serveur : {len(json.loads(data))} déjà répondu.")
    except Exception as e:
        print(f"Avertissement : réponses non récupérées ({e}). Tout le monde sera relancé.")
    relance_sender.send_relances()
    print("Relances envoyées.")

else:
    print("Rien à faire aujourd'hui.")
