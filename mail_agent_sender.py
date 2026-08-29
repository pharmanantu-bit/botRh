"""Envoi (runner GitHub) d'un e-mail décidé par l'Agent RH.

Payload repository_dispatch « mail_agent » : {"to", "subject", "body"} + pièce jointe
optionnelle {"attachment_name", "attachment_b64"} (PDF encodé base64).
Le corps est envoyé tel quel (texte brut), signé côté serveur si besoin.
"""
import os
import sys
import json
import base64
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication


def main():
    user, pwd = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    p = json.loads(os.getenv("PAYLOAD") or "{}")
    dest, sujet, corps = (p.get("to") or "").strip(), (p.get("subject") or "").strip(), p.get("body") or ""
    if not user or not pwd:
        print("Identifiants Gmail manquants."); sys.exit(1)
    if not dest or "@" not in dest or not corps:
        print("Payload incomplet (to/body)."); sys.exit(1)
    pj_nom, pj_b64 = (p.get("attachment_name") or "").strip(), p.get("attachment_b64") or ""
    if pj_nom and pj_b64:
        msg = MIMEMultipart()
        msg.attach(MIMEText(corps, "plain", "utf-8"))
        piece = MIMEApplication(base64.b64decode(pj_b64), Name=pj_nom)
        piece["Content-Disposition"] = f'attachment; filename="{pj_nom}"'
        msg.attach(piece)
    else:
        msg = MIMEText(corps, "plain", "utf-8")
    msg["From"], msg["To"], msg["Subject"] = user, dest, sujet or "(sans objet)"
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, [dest], msg.as_string())
    print("Mail envoyé (agent RH).")


if __name__ == "__main__":
    main()
