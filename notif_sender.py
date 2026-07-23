"""Notification de relevé soumis — exécuté par GitHub Actions.

Le serveur PythonAnywhere gratuit ne peut pas envoyer d'e-mail (SMTP bloqué).
Quand un employé soumet son relevé en ligne, le serveur déclenche le workflow
'notif_releve' qui exécute ce script : il génère un PDF du relevé et l'envoie
par mail à l'admin, avec le résumé dans le corps.

Les données du relevé arrivent via la variable d'environnement PAYLOAD (JSON).
"""
import os
import io
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from signature_mail import SIGNATURE
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

ADMIN_EMAIL = "pharmanantu@gmail.com"
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

MOIS_FR = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"
}

p = json.loads(os.environ.get("PAYLOAD") or "{}") or {}
prenom = str(p.get("prenom", "")).strip() or "Employé"
email_emp = str(p.get("email", "")).strip()
hp = float(p.get("heures_plus", 0) or 0)
hm = float(p.get("heures_moins", 0) or 0)
solde = round(hp - hm, 2)
signe = "+" if solde >= 0 else ""
commentaire = str(p.get("commentaire", "")).strip() or "aucun"
date_signature = str(p.get("date_signature", "")).strip()
signature = str(p.get("signature", "")).strip()
date_soumission = str(p.get("date", "")).strip()
# "periode" = [mois, annee] (format compact pour rester sous la limite de 10
# propriétés du repository_dispatch) ; repli sur les anciennes clés mois/annee.
periode = p.get("periode") or [p.get("mois", 0), p.get("annee", 0)]
mois = int((periode[0] if len(periode) > 0 else 0) or 0)
annee = int((periode[1] if len(periode) > 1 else 0) or 0)
mois_annee = f"{MOIS_FR.get(mois, '')} {annee}".strip()
jours = p.get("jours") or []  # détail jour par jour : [{label, plus, moins}, ...]


def generer_pdf():
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=2*cm, bottomMargin=2*cm,
                            leftMargin=2*cm, rightMargin=2*cm)
    styles = getSampleStyleSheet()
    titre = ParagraphStyle("titre", parent=styles["Title"],
                           textColor=colors.HexColor("#1F4E79"), fontSize=18)
    sous = ParagraphStyle("sous", parent=styles["Normal"], fontSize=12,
                          textColor=colors.HexColor("#444444"), spaceAfter=6)
    petit = ParagraphStyle("petit", parent=styles["Normal"], fontSize=8,
                           textColor=colors.HexColor("#888888"))

    elements = [
        Paragraph("Relevé d'heures", titre),
        Paragraph(f"{prenom} — {mois_annee}", sous),
        Spacer(1, 0.6*cm),
    ]

    lignes = [
        ["Heures supplémentaires (H+)", f"{hp:g} h"],
        ["Heures d'absence (H−)", f"{hm:g} h"],
        ["Solde du mois", f"{signe}{solde:g} h"],
        ["Commentaire", commentaire],
        ["Date de signature", date_signature],
        ["Signature", signature],
        ["Soumis le", date_soumission],
    ]
    t = Table(lignes, colWidths=[6.5*cm, 9.5*cm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#1F4E79")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#EAF1FB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDDDD")),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 0.8*cm))

    # Détail jour par jour (enregistré depuis juin 2026 ; absent des relevés antérieurs)
    sous_titre = ParagraphStyle("soustitre", parent=styles["Heading2"],
                                fontSize=12, textColor=colors.HexColor("#1F4E79"),
                                spaceAfter=6)
    elements.append(Paragraph("Détail jour par jour", sous_titre))
    if jours:
        data = [["Jour", "H +", "H −"]]
        tot_p = tot_m = 0.0
        for j in jours:
            jp = float(j.get("plus", 0) or 0)
            jm = float(j.get("moins", 0) or 0)
            tot_p += jp
            tot_m += jm
            data.append([str(j.get("label", "")),
                         f"+{jp:g}" if jp else "—",
                         f"-{jm:g}" if jm else "—"])
        data.append(["Total", f"+{tot_p:g}", f"-{tot_m:g}"])
        td = Table(data, colWidths=[8*cm, 4*cm, 4*cm])
        td.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("TEXTCOLOR", (1, 1), (1, -2), colors.HexColor("#276221")),
            ("TEXTCOLOR", (2, 1), (2, -2), colors.HexColor("#9C0006")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#EAF1FB")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDDDD")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        elements.append(td)
    else:
        elements.append(Paragraph(
            "Détail jour par jour non disponible pour ce relevé.", petit))

    elements.append(Spacer(1, 1*cm))
    elements.append(Paragraph(
        "Pharmacie Apothical Nanterre Université — document généré automatiquement par botRh.",
        petit))

    doc.build(elements)
    return buf.getvalue()


def envoyer(server, destinataire, sujet, corps, pdf):
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = destinataire
    msg["Subject"] = sujet
    msg.attach(MIMEText(corps, "plain", "utf-8"))
    part = MIMEBase("application", "pdf")
    part.set_payload(pdf)
    encoders.encode_base64(part)
    nom_fichier = f"Releve_{prenom}_{MOIS_FR.get(mois, '')}_{annee}.pdf".replace(" ", "_")
    part.add_header("Content-Disposition", f'attachment; filename="{nom_fichier}"')
    msg.attach(part)
    server.sendmail(GMAIL_USER, destinataire, msg.as_string())


def main():
    pdf = generer_pdf()

    corps_admin = (
        f"{prenom} vient de soumettre son relevé d'heures.\n\n"
        f"H+ : {hp:g} h\n"
        f"H− : {hm:g} h\n"
        f"Solde : {signe}{solde:g} h\n"
        f"Commentaire : {commentaire}\n"
        f"Signé le : {date_signature}\n\n"
        f"Le relevé complet est en pièce jointe (PDF).\n"
        f"Voir l'admin : https://pharmacie92000.pythonanywhere.com/admin"
    )
    corps_employe = (
        f"Bonjour {prenom},\n\n"
        f"Nous avons bien reçu votre relevé d'heures de {mois_annee}. Merci !\n\n"
        f"Récapitulatif :\n"
        f"  • Heures en plus (H+) : {hp:g} h\n"
        f"  • Heures en moins (H−) : {hm:g} h\n"
        f"  • Solde du mois : {signe}{solde:g} h\n"
        f"  • Commentaire : {commentaire}\n\n"
        f"Une copie de votre relevé est en pièce jointe (PDF).\n"
        f"En cas d'erreur, vous pouvez le modifier jusqu'au 25 via votre lien.\n\n"
        f"Belle journée,\n\n{SIGNATURE}"
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        # 1) Notification à l'admin
        envoyer(server, ADMIN_EMAIL,
                f"botRh — {prenom} a soumis son relevé {mois_annee}", corps_admin, pdf)
        print(f"Notification + PDF envoyés à l'admin pour {prenom} ({mois_annee}).")
        # 2) Accusé de réception à l'employé (si email connu et ≠ admin)
        if email_emp and email_emp.lower() != ADMIN_EMAIL.lower():
            envoyer(server, email_emp,
                    f"Relevé d'heures {mois_annee} bien reçu ✓", corps_employe, pdf)
            print(f"Accusé de réception envoyé à {email_emp}.")


if __name__ == "__main__":
    main()
