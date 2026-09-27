"""Newsletter « Apothical Inside » — exécuté par GitHub Actions.

Le serveur PythonAnywhere gratuit ne peut pas envoyer d'e-mail (SMTP bloqué),
l'envoi est délégué à ce runner. Deux modes (variable d'env MODE) :
  - "rappel" (cron du 1er du mois) : mail à l'admin « la newsletter du mois
    t'attend » avec le lien du brouillon — rien n'est envoyé à l'équipe ;
  - "envoi" (repository_dispatch 'newsletter_envoi', déclenché par le bouton
    « Valider et envoyer » de l'admin) : récupère le PDF validé via
    /export_newsletter + la liste des collaborateurs ACTIFS via
    /export_employes, et envoie le PDF à chacun.
"""
import os
import json
import smtplib
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import date

from signature_mail import SIGNATURE

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
ADMIN_EMAIL = "pharmanantu@gmail.com"
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
CLE = os.environ.get("API_CLE", "")

MOIS_FR = {1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril", 5: "Mai",
           6: "Juin", 7: "Juillet", 8: "Août", 9: "Septembre",
           10: "Octobre", 11: "Novembre", 12: "Décembre"}


def titre_mois(num):
    return f"{MOIS_FR[int(num[5:7])]} {num[:4]}"


def _envoyer(serveur, dest, sujet, corps, pdf=None, nom_pdf=""):
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = dest
    msg["Subject"] = sujet
    msg.attach(MIMEText(corps, "plain", "utf-8"))
    if pdf:
        piece = MIMEApplication(pdf, _subtype="pdf")
        piece.add_header("Content-Disposition", "attachment", filename=nom_pdf)
        msg.attach(piece)
    serveur.sendmail(GMAIL_USER, [dest], msg.as_string())


def mode_rappel():
    """Le 1er du mois : rappel à l'admin de préparer le numéro (aucun envoi équipe)."""
    auj = date.today()
    num = f"{auj.year:04d}-{auj.month:02d}"
    sujet = f"📰 Apothical Inside {titre_mois(num)} — à préparer"
    corps = (
        f"Bonjour,\n\n"
        f"Nouveau mois, nouveau numéro : la newsletter « Apothical Inside — "
        f"{titre_mois(num)} » attend sa rédaction.\n\n"
        f"1. Jette tes notes en vrac (évènements, travaux, animations, vie d'équipe…)\n"
        f"2. Clique « ✨ Rédiger avec l'IA », relis et ajuste\n"
        f"3. Ajoute les photos, vérifie l'aperçu PDF\n"
        f"4. « ✅ Valider et envoyer » — rien ne part sans toi\n\n"
        f"👉 {BASE_URL}/admin/newsletter\n\n{SIGNATURE}"
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        _envoyer(srv, ADMIN_EMAIL, sujet, corps)
    print("Rappel envoyé à l'admin.")


def mode_envoi():
    """Après validation admin : PDF validé → mail à chaque collaborateur actif."""
    payload = json.loads(os.environ.get("PAYLOAD") or "{}")
    num = str(payload.get("num", "")).strip()
    if not num:
        print("Payload sans numéro — rien à envoyer.")
        return
    with urllib.request.urlopen(f"{BASE_URL}/export_newsletter?cle={CLE}&num={num}",
                                timeout=60) as r:
        pdf = r.read()
    with urllib.request.urlopen(f"{BASE_URL}/export_employes?cle={CLE}", timeout=30) as r:
        employes = json.loads(r.read().decode("utf-8"))
    print(f"PDF {len(pdf)} octets, {len(employes)} collaborateur(s) actif(s).")

    nom_pdf = f"Apothical_Inside_{titre_mois(num).replace(' ', '_')}.pdf"
    sujet = f"📰 Apothical Inside — {titre_mois(num)}"
    envoyes, echecs = 0, 0
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        for e in employes:
            dest = (e.get("email") or "").strip()
            if not dest:
                continue
            corps = (
                f"Bonjour {e.get('prenom', '')},\n\n"
                f"Voici « Apothical Inside », la newsletter du mois de "
                f"{titre_mois(num)} de la pharmacie — en pièce jointe.\n\n"
                f"Bonne lecture !\n\n{SIGNATURE}"
            )
            try:
                _envoyer(srv, dest, sujet, corps, pdf, nom_pdf)
                envoyes += 1
                print(f"  OK  {e.get('prenom', '')}")
            except Exception as ex:  # un échec individuel ne bloque pas les autres
                echecs += 1
                print(f"  KO  {e.get('prenom', '')} : {ex}")
        # Copie à l'admin pour archive
        _envoyer(srv, ADMIN_EMAIL, f"{sujet} (copie d'archive — {envoyes} envoyé(s))",
                 f"Newsletter envoyée à {envoyes} collaborateur(s), {echecs} échec(s).",
                 pdf, nom_pdf)
    print(f"Terminé : {envoyes} envoyé(s), {echecs} échec(s).")
    if envoyes == 0:
        raise SystemExit(1)


if __name__ == "__main__":
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise SystemExit("Identifiants Gmail manquants (secrets GitHub).")
    if os.environ.get("MODE", "rappel") == "envoi":
        mode_envoi()
    else:
        mode_rappel()
