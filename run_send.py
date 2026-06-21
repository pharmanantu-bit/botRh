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
import os
import json
import urllib.request
from datetime import datetime

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
CLE = os.environ.get("API_CLE", "botRh-trigger-2026")
ADMIN_EMAIL = "pharmanantu@gmail.com"


def sync_employes():
    """Récupère la liste des employés gérée sur le serveur (page admin) et
    l'écrit dans le fichier local, pour que l'envoi utilise toujours la liste
    à jour. Le serveur est la source unique de vérité."""
    import csv
    import config
    url = f"{BASE_URL}/export_employes?cle={CLE}"
    with urllib.request.urlopen(url, timeout=30) as r:
        employes = json.loads(r.read().decode("utf-8"))
    with open(config.EMPLOYEES_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["nom", "prenom", "email"])
        w.writeheader()
        for e in employes:
            w.writerow({"nom": e.get("nom", ""), "prenom": e.get("prenom", ""),
                        "email": e.get("email", "")})
    print(f"Liste employés synchronisée depuis le serveur : {len(employes)} employé(s).")
    return len(employes)

# Texte d'annonce inséré en haut du mail, activé seulement si INTRO=oui
# (utilisé pour le 1er envoi qui présente le nouveau système en ligne).
INTRO_JUIN = (
    "🆕 Nouveauté : votre feuille d'heures se remplit désormais directement EN LIGNE. "
    "Plus besoin de document à imprimer ou à renvoyer — cliquez simplement sur le lien "
    "ci-dessous et remplissez-la en ligne, exactement comme le relevé que vous remplissiez "
    "auparavant. C'est rapide et tout est enregistré automatiquement."
)
intro = INTRO_JUIN if os.environ.get("INTRO") == "oui" else None

mode = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "auto"
jour = datetime.now().day
if mode == "auto":
    mode = "releves" if jour == 20 else ("relances" if jour == 22 else "rien")

# Exception ponctuelle : pas de relance le 22 juin 2026 — les relevés ont été
# envoyés manuellement le 21, une relance le lendemain serait prématurée.
if mode == "relances" and datetime.now().strftime("%Y-%m-%d") == "2026-06-22":
    print("Relance du 22/06/2026 sautée (envoi manuel la veille).")
    mode = "rien"

print(f"run_send.py — mode={mode} (jour du mois={jour}) intro={'oui' if intro else 'non'}")

if mode == "test":
    from email_sender import send_emails
    send_emails(only_to=ADMIN_EMAIL, intro=intro)
    print(f"Mail de test envoyé à {ADMIN_EMAIL}.")

elif mode == "releves":
    sync_employes()
    from email_sender import send_emails
    send_emails(intro=intro)
    print("Relevés envoyés.")

elif mode == "relances":
    sync_employes()
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
