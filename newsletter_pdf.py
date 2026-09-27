"""Newsletter « Apothical Inside » — composition du PDF (reportlab).

Reproduit la mise en page du modèle historique (Décembre 2025) : bandeau vert
Apothical en tête de chaque page, page de garde avec titre + sous-titre +
table des matières, une entrée par section (titre vert olive, paragraphes
justifiés, photos en rangées), pied de page « Jonathan & David Illouz /
Direction — Pharmacie Apothical Nanterre Université » + numéro de page.

reportlab et les images sont importés PARESSEUSEMENT : si la lib manque côté
serveur, seule la génération du PDF échoue, le site reste fonctionnel.
"""
import io
import os

VERT_BANDEAU = "#1c4532"   # vert foncé du bandeau (identique promesse d'embauche)
VERT_TITRE = "#77955d"     # vert olive des titres de sections (modèle Word)
GRIS_TEXTE = "#222222"

PIED_1 = "Jonathan & David Illouz"
PIED_2 = "Direction – Pharmacie Apothical Nanterre Université"


def reportlab_disponible():
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        return False


def _entete_pied(canvas, doc):
    """Bandeau vert en tête + pied de page signé, sur chaque page."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    canvas.saveState()
    largeur, hauteur = A4
    canvas.setFillColor(colors.HexColor(VERT_BANDEAU))
    canvas.rect(0, hauteur - 1.8 * cm, largeur, 1.8 * cm, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(2 * cm, hauteur - 1.05 * cm, "APOTHICAL")
    canvas.setFont("Helvetica", 7)
    canvas.drawString(2 * cm, hauteur - 1.45 * cm, "PHARMACIE NANTERRE UNIVERSITÉ")
    # Pied de page : signature direction + pagination
    canvas.setFillColor(colors.HexColor("#555555"))
    canvas.setFont("Helvetica", 8)
    canvas.drawCentredString(largeur / 2, 1.35 * cm, PIED_1)
    canvas.drawCentredString(largeur / 2, 0.95 * cm, PIED_2)
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(largeur - 2 * cm, 1.35 * cm, f"Page {doc.page}")
    canvas.restoreState()


def _rangee_photos(chemins, largeur_dispo):
    """Table reportlab d'une rangée de photos (jusqu'à 3), hauteurs alignées.
    Les images illisibles sont ignorées silencieusement (best-effort)."""
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image, Table, TableStyle
    imgs = []
    for ch in chemins:
        try:
            lecteur = ImageReader(ch)
            l, h = lecteur.getSize()
            lecteur.getRGBData()   # décode entièrement : une image corrompue est
            imgs.append((ch, l / float(h)))  # écartée ICI, pas au dessin du PDF
        except Exception:
            continue
    if not imgs:
        return None
    espace = 0.35 * cm
    dispo = largeur_dispo - espace * (len(imgs) - 1)
    # Hauteur commune : la rangée remplit la largeur, plafonnée à 7,5 cm.
    hauteur = min(dispo / sum(r for _, r in imgs), 7.5 * cm)
    cellules = [Image(ch, width=hauteur * r, height=hauteur) for ch, r in imgs]
    t = Table([cellules], colWidths=[hauteur * r + espace for _, r in imgs])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), espace),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _echapper(txt):
    return (txt or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generer_pdf_newsletter(n, photos_dir):
    """Compose le PDF depuis le numéro n = {titre_mois, sous_titre, sections:[
    {titre, texte, photos:[fichiers]}]} et renvoie un BytesIO prêt pour send_file."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    PageBreak, KeepTogether)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=2.9 * cm, bottomMargin=2.2 * cm,
                            leftMargin=2 * cm, rightMargin=2 * cm)
    largeur_dispo = A4[0] - 4 * cm
    styles = getSampleStyleSheet()
    normal = ParagraphStyle("nl", parent=styles["Normal"], fontSize=10.5,
                            leading=15.5, textColor=colors.HexColor(GRIS_TEXTE),
                            alignment=TA_JUSTIFY, spaceAfter=10)
    puce = ParagraphStyle("puce", parent=normal, leftIndent=0.8 * cm,
                          bulletIndent=0.3 * cm, spaceAfter=6)
    titre_section = ParagraphStyle("titre_sec", parent=styles["Heading1"],
                                   fontName="Helvetica-Bold", fontSize=15,
                                   textColor=colors.HexColor(VERT_TITRE),
                                   spaceBefore=6, spaceAfter=12)
    grand_titre = ParagraphStyle("grand", parent=styles["Title"], fontSize=27,
                                 leading=34, textColor=colors.HexColor(VERT_TITRE),
                                 alignment=TA_CENTER, spaceBefore=40)
    sous_titre = ParagraphStyle("sous", parent=normal, fontName="Helvetica-Bold",
                                fontSize=14, alignment=TA_CENTER, spaceBefore=16)
    som_titre = ParagraphStyle("somt", parent=normal, fontSize=17,
                               textColor=colors.HexColor("#3d5a45"),
                               alignment=TA_CENTER, spaceBefore=54, spaceAfter=16)
    som_ligne = ParagraphStyle("soml", parent=normal, fontName="Helvetica-Bold",
                               fontSize=10.5, alignment=0, spaceAfter=6,
                               leftIndent=2.2 * cm)

    sections = [s for s in n.get("sections", [])
                if (s.get("titre") or "").strip() or (s.get("texte") or "").strip()]

    # --- Page de garde : titre + sous-titre + table des matières ---
    el = [Paragraph(f"Apothical Inside – {_echapper(n.get('titre_mois', ''))}", grand_titre)]
    if (n.get("sous_titre") or "").strip():
        el.append(Paragraph(_echapper(n["sous_titre"]), sous_titre))
    if sections:
        el.append(Paragraph("Table des matières", som_titre))
        for s in sections:
            el.append(Paragraph(_echapper(s.get("titre") or "(sans titre)"), som_ligne))
    el.append(PageBreak())

    # --- Sections ---
    for i, s in enumerate(sections):
        bloc = [Paragraph(_echapper(s.get("titre") or ""), titre_section)]
        for para in (s.get("texte") or "").split("\n"):
            para = para.strip()
            if not para:
                continue
            if para.startswith(("- ", "• ", "* ")):
                bloc.append(Paragraph(_echapper(para[2:].strip()), puce, bulletText="•"))
            else:
                bloc.append(Paragraph(_echapper(para), normal))
        el.append(KeepTogether(bloc[:2]) if len(bloc) > 1 else bloc[0])
        el.extend(bloc[2:] if len(bloc) > 1 else [])
        # Photos de la section, par rangées de 3 max.
        chemins = [os.path.join(photos_dir, f) for f in s.get("photos", [])]
        chemins = [c for c in chemins if os.path.isfile(c)]
        for deb in range(0, len(chemins), 3):
            rangee = _rangee_photos(chemins[deb:deb + 3], largeur_dispo)
            if rangee is not None:
                el.append(Spacer(1, 0.3 * cm))
                el.append(rangee)
        if i < len(sections) - 1:
            el.append(PageBreak())

    if len(el) == 1:  # aucune section : éviter un PDF vide invalide
        el.append(Paragraph("(Numéro en préparation)", normal))

    doc.build(el, onFirstPage=_entete_pied, onLaterPages=_entete_pied)
    buf.seek(0)
    return buf
