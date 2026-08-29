"""Attestation de travail en PDF (reportlab) — même contenu que la page
imprimable templates/attestation.html, même charte que la promesse d'embauche.
Utilisée par l'agent RH (outil generer_attestation / envoyer_attestation).
Importe reportlab paresseusement : le module se charge même sans la lib."""
import io
from datetime import datetime

from promesse_embauche import _bandeau, VERT_HEX, GRIS_HEX, reportlab_disponible  # noqa: F401


def _date_fr(s):
    """« 2025-09-01 » / « 01/09/2025 » -> « 01/09/2025 » (inchangé si illisible)."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return s


def texte_attestation(emp, profil):
    """Paragraphes (texte brut) de l'attestation — partagés PDF / corps de mail."""
    nom = f"{emp.get('prenom', '')} {(emp.get('nom') or '').upper()}".strip()
    p1 = (f"Je soussigné(e), responsable de la Pharmacie Apothical Nanterre Université, "
          f"atteste que {nom} ")
    p1 += (f"occupe le poste de {profil['poste']}" if profil.get("poste")
           else "fait partie de notre personnel")
    p1 += " au sein de notre établissement"
    if profil.get("type_contrat"):
        p1 += f" dans le cadre d'un contrat de type {profil['type_contrat']}"
    if profil.get("date_entree"):
        p1 += f", depuis le {_date_fr(profil['date_entree'])}"
    p1 += "."
    p2 = "La présente attestation est délivrée à l'intéressé(e) pour servir et valoir ce que de droit."
    return [p1, p2]


def generer_pdf_attestation(emp, profil):
    """Renvoie les octets du PDF."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_RIGHT
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=3 * cm, bottomMargin=2 * cm,
                            leftMargin=2.2 * cm, rightMargin=2.2 * cm,
                            title="Attestation de travail")
    styles = getSampleStyleSheet()
    gris = colors.HexColor(GRIS_HEX)
    normal = ParagraphStyle("corps", parent=styles["Normal"], fontSize=11, leading=18,
                            textColor=gris, alignment=TA_JUSTIFY, spaceAfter=14)
    titre = ParagraphStyle("titre", parent=styles["Title"], fontSize=16, leading=22,
                           textColor=colors.HexColor(VERT_HEX), alignment=TA_CENTER,
                           spaceBefore=18, spaceAfter=28)
    entete = ParagraphStyle("entete", parent=normal, spaceAfter=2, alignment=0)
    droite = ParagraphStyle("droite", parent=normal, alignment=TA_RIGHT, spaceAfter=4)
    mention = ParagraphStyle("mention", parent=normal, fontSize=8, leading=11,
                             textColor=colors.HexColor("#777777"))

    elems = [Paragraph("<b>Pharmacie Apothical</b>", entete),
             Paragraph("Nanterre Université", entete),
             Spacer(1, 0.6 * cm),
             Paragraph("ATTESTATION DE TRAVAIL", titre)]
    for p in texte_attestation(emp, profil):
        elems.append(Paragraph(p, normal))
    elems += [Spacer(1, 1.6 * cm),
              Paragraph(f"Fait à Nanterre, le {datetime.now().strftime('%d/%m/%Y')}.", droite),
              Spacer(1, 1.2 * cm),
              Paragraph("Signature et cachet de l'employeur", droite),
              Spacer(1, 2 * cm),
              Paragraph("Document généré par botRh — à vérifier et signer avant remise.", mention)]
    doc.build(elems, onFirstPage=_bandeau, onLaterPages=_bandeau)
    return buf.getvalue()
