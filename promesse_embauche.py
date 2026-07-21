"""Promesse d'embauche — valeurs par défaut + génération du PDF (reportlab).

Le courrier reproduit le modèle utilisé par la pharmacie (cf. exemple Meriem
2026) : bandeau vert, bloc entreprise, « À l'attention de », objet, corps,
double bloc de signature. Tous les champs sont modifiables depuis le
formulaire /admin/recrutement/promesse ; ce module ne fait que composer.
"""
import io
from datetime import datetime, timedelta

# reportlab est importé PARESSEUSEMENT (dans generer_pdf_promesse) : s'il manque
# côté serveur (piège python3.12 / pip --user), seule la génération du PDF
# échoue — le site, le formulaire et l'aperçu restent fonctionnels.

VERT_HEX = "#1c4532"
GRIS_HEX = "#222222"

CIVILITES = ["Madame", "Monsieur", "Mademoiselle"]
TYPES_CONTRAT = ["CDD", "CDI"]


def reportlab_disponible():
    """Témoin healthcheck : True si la génération PDF est possible."""
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        return False


def valeurs_par_defaut(candidat=None):
    """Pré-remplissage du formulaire : infos pharmacie + candidat s'il existe."""
    c = candidat or {}
    return {
        "pharmacie_nom": "Pharmacie Nanterre Université",
        "pharmacie_adresse": "390 Boulevard des Provinces Françaises",
        "pharmacie_cp_ville": "92000 NANTERRE",
        "pharmacie_tel": "01 47 21 22 28",
        "pharmacie_siret": "895 110 682 00013",
        "gerants": "Messieurs David et Jonathan ILLOUZ",
        "gerants_titre": "Gérants",
        "convention": "Pharmacie d'officine",
        "date_courrier": datetime.now().strftime("%d/%m/%Y"),
        "civilite": "Madame",
        "nom": c.get("nom", ""),
        "prenom": c.get("prenom", ""),
        "poste": c.get("poste_vise", ""),
        "type_contrat": "CDD",
        "duree_contrat": "6 mois",
        "date_debut": "",
        "salaire_brut": "",
        "heures_mensuelles": "151,67",
        "temps_precision": "soit un temps plein à 35 heures par semaine",
        "validite": (datetime.now() + timedelta(days=10)).strftime("%d/%m/%Y"),
    }


CHAMPS_PROMESSE = list(valeurs_par_defaut().keys())


def _phrase_contrat(p):
    if p.get("type_contrat") == "CDI":
        base = "d'un contrat à durée indéterminée"
    else:
        base = f"d'un contrat à durée déterminée de {p.get('duree_contrat', '')}"
    return f"{base} à partir du {p.get('date_debut', '')}"


def paragraphes_courrier(p):
    """Le corps du courrier, phrase par phrase — aussi utilisé pour l'aperçu HTML."""
    civ = p.get("civilite", "Madame")
    return [
        f"Nous avons le plaisir de vous proposer un engagement dans notre entreprise "
        f"{p.get('pharmacie_nom', '')}, en qualité de {p.get('poste', '')}, dans le cadre "
        f"{_phrase_contrat(p)}.",

        f"Conformément aux dispositions de la convention collective {p.get('convention', '')}, "
        "qui vous seront applicables, l'embauche définitive sera précédée d'une période "
        "d'essai. Pendant cette période d'essai, le contrat pourra être rompu par l'une ou "
        "l'autre des parties en respectant le délai de prévenance.",

        f"Vous bénéficierez d'une <b>rémunération mensuelle brute de {p.get('salaire_brut', '')}</b>, "
        f"pour un temps de travail correspondant à <b>{p.get('heures_mensuelles', '')} heures "
        f"mensuelles</b> ({p.get('temps_precision', '')}).",

        f"Vos fonctions seront exercées au sein de la {p.get('pharmacie_nom', '')} au "
        f"{p.get('pharmacie_adresse', '')}, {p.get('pharmacie_cp_ville', '')}. Un contrat de "
        "travail définissant les conditions d'exercice de votre activité vous sera remis dès "
        "la prise effective de vos fonctions.",

        "Pour la bonne règle, nous vous remercions de nous retourner un exemplaire de cette "
        "lettre daté et signé avec la mention « lu et approuvé ».",

        f"La présente promesse d'embauche vaut engagement contractuel et reste valable "
        f"jusqu'au {p.get('validite', '')} inclus.",

        f"Nous vous prions de recevoir, {civ}, l'expression de nos sentiments distingués.",
    ]


def _bandeau(canvas, doc):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    canvas.saveState()
    largeur, hauteur = A4
    canvas.setFillColor(colors.HexColor(VERT_HEX))
    canvas.rect(0, hauteur - 1.6 * cm, largeur, 1.6 * cm, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(2 * cm, hauteur - 1.0 * cm, "APOTHICAL")
    canvas.setFont("Helvetica", 7)
    canvas.drawString(2 * cm, hauteur - 1.35 * cm, "PHARMACIE NANTERRE UNIVERSITÉ")
    canvas.restoreState()


def generer_pdf_promesse(p):
    """Compose le PDF et renvoie un BytesIO prêt pour send_file."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle)
    GRIS_TEXTE = colors.HexColor(GRIS_HEX)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=2.6 * cm, bottomMargin=1.8 * cm,
                            leftMargin=2 * cm, rightMargin=2 * cm)
    styles = getSampleStyleSheet()
    normal = ParagraphStyle("courrier", parent=styles["Normal"], fontSize=10,
                            leading=14, textColor=GRIS_TEXTE, alignment=TA_JUSTIFY,
                            spaceAfter=10)
    entete = ParagraphStyle("entete", parent=normal, alignment=0, spaceAfter=0,
                            leading=13)
    droite = ParagraphStyle("droite", parent=entete, leftIndent=9 * cm)
    objet = ParagraphStyle("objet", parent=normal, fontName="Helvetica-Bold",
                           spaceBefore=8, spaceAfter=14, alignment=0)

    el = [
        Paragraph(f"{p.get('pharmacie_nom', '')}<br/>{p.get('pharmacie_adresse', '')}<br/>"
                  f"{p.get('pharmacie_cp_ville', '')}<br/>{p.get('pharmacie_tel', '')}<br/>"
                  f"Siret : {p.get('pharmacie_siret', '')}", entete),
        Spacer(1, 1.4 * cm),
        Paragraph(f"À l'attention de<br/>{p.get('civilite', '')} "
                  f"{p.get('nom', '').upper()}<br/>{p.get('prenom', '')}", droite),
        Spacer(1, 0.8 * cm),
        Paragraph(f"Le {p.get('date_courrier', '')}",
                  ParagraphStyle("date", parent=entete, leftIndent=12 * cm)),
        Spacer(1, 0.6 * cm),
        Paragraph("Objet : Promesse d'embauche.", objet),
        Paragraph(f"{p.get('prenom', '')},", normal),
    ]
    for texte in paragraphes_courrier(p):
        el.append(Paragraph(texte, normal))

    el.append(Spacer(1, 1.2 * cm))
    sig = ParagraphStyle("sig", parent=entete, spaceAfter=0)
    t = Table([[Paragraph(f"{p.get('gerants', '')}<br/>{p.get('gerants_titre', '')}", sig),
                Paragraph(f"{p.get('civilite', '')} {p.get('prenom', '').upper()} "
                          f"{p.get('nom', '').upper()}<br/>lu et approuvé + signature", sig)]],
              colWidths=[8.5 * cm, 8.5 * cm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    el.append(t)

    doc.build(el, onFirstPage=_bandeau, onLaterPages=_bandeau)
    buf.seek(0)
    return buf
