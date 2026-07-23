"""Notification de demande de congés — exécuté par GitHub Actions.

Le serveur PythonAnywhere gratuit ne peut pas envoyer d'e-mail (SMTP bloqué).
Le serveur déclenche le workflow 'notif_conges' (repository_dispatch
'demande_conges') qui exécute ce script :
  - action "deposee"  → mail à l'admin (nouvelle demande à traiter) ;
  - action "acceptee" → mail à l'employé (congés validés, posés au planning) ;
  - action "refusee"  → mail à l'employé (refus + motif éventuel).

Les données arrivent via la variable d'environnement PAYLOAD (JSON).
"""
import os
import json
import smtplib
from email.mime.text import MIMEText

ADMIN_EMAIL = "pharmanantu@gmail.com"
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
URL_ADMIN_CONGES = "https://pharmacie92000.pythonanywhere.com/admin/planning-equipe?onglet=conges"


def construire_mail(p):
    """Retourne (destinataire, sujet, corps) selon l'action du payload,
    ou None si rien à envoyer (action inconnue, email manquant)."""
    action = str(p.get("action", "")).strip()
    prenom = str(p.get("prenom", "")).strip() or "Un employé"
    email_emp = str(p.get("email", "")).strip()
    debut = str(p.get("debut", "")).strip() or "?"
    fin = str(p.get("fin", "")).strip() or "?"
    nb = int(p.get("nb", 0) or 0)
    commentaire = str(p.get("commentaire", "")).strip()
    motif_refus = str(p.get("motif_refus", "")).strip()
    s = "s" if nb > 1 else ""
    duree = f"{nb} jour{s} ouvrable{s} (lun-sam hors fériés)"

    if action == "deposee":
        sujet = f"botRh — {prenom} demande des congés du {debut} au {fin}"
        corps = (
            f"{prenom} vient de déposer une demande de congés payés.\n\n"
            f"Du : {debut}\n"
            f"Au : {fin}\n"
            f"Durée : {duree}\n"
            f"Commentaire : {commentaire or 'aucun'}\n\n"
            f"Accepter ou refuser la demande (le solde CP est affiché sur place) :\n"
            f"{URL_ADMIN_CONGES}"
        )
        return (ADMIN_EMAIL, sujet, corps)

    if action == "acceptee" and email_emp:
        sujet = f"Congés du {debut} au {fin} : acceptés ✓"
        corps = (
            f"Bonjour {prenom},\n\n"
            f"Bonne nouvelle : votre demande de congés du {debut} au {fin} "
            f"({duree}) est ACCEPTÉE.\n\n"
            f"Elle est posée au planning et déduite de votre solde de congés, "
            f"que vous pouvez suivre dans « Mon espace » (bloc Mes congés payés).\n\n"
            f"Belle journée,\nLa direction"
        )
        return (email_emp, sujet, corps)

    if action == "refusee" and email_emp:
        sujet = f"Congés du {debut} au {fin} : demande refusée"
        corps = (
            f"Bonjour {prenom},\n\n"
            f"Votre demande de congés du {debut} au {fin} ({duree}) n'a pas pu "
            f"être acceptée.\n"
            f"Motif : {motif_refus or 'non précisé'}\n\n"
            f"N'hésitez pas à en parler avec la direction ou à proposer d'autres "
            f"dates depuis « Mon espace ».\n\n"
            f"Belle journée,\nLa direction"
        )
        return (email_emp, sujet, corps)

    return None


def main():
    p = json.loads(os.environ.get("PAYLOAD") or "{}") or {}
    m = construire_mail(p)
    if not m:
        print(f"Payload sans action exploitable ({p.get('action')!r}) — rien envoyé.")
        return
    destinataire, sujet, corps = m
    msg = MIMEText(corps, "plain", "utf-8")
    msg["From"] = GMAIL_USER
    msg["To"] = destinataire
    msg["Subject"] = sujet
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, destinataire, msg.as_string())
    print(f"Mail « {sujet} » envoyé à {destinataire}.")


if __name__ == "__main__":
    main()
