"""Extraction de champs depuis les pièces jointes RH — exécuté sur le RUNNER GitHub.

Lit le contenu d'une PJ déjà classée et en extrait des champs UTILES pour
PROPOSER (jamais écrire d'office) un pré-remplissage du profil salarié.

MINIMISATION / RGPD : AUCUNE donnée de santé n'est extraite — le contenu des
ARRÊTS DE TRAVAIL n'est PAS lu (ils restent de simples documents classés).
Cible volontairement limitée à :
  - RIB                             -> IBAN (masqué : 4 derniers chiffres)
  - Contrat / Avenant / Promesse    -> type (CDI/CDD), emploi, durée du travail
    (hebdo, ou mensuelle 151,67 h -> 35 h/sem), dates (entrée/ancienneté, fin
    de CDD, fin d'essai), date de naissance, adresse — champs du profil salarié
    uniquement (le n° de sécurité sociale et le salaire ne sont PAS extraits)

OCR 100 % LOCAL (Tesseract) : aucun document n'est envoyé à une IA tierce.
Module PUR : pas de réseau, pas de chiffrement (le serveur chiffrera l'IBAN au
repos). Toutes les dépendances (pdfplumber, pytesseract, pdf2image, Pillow,
python-docx) sont importées paresseusement et tout échoue PROPREMENT (renvoie
"" / []) si une lib ou Tesseract n'est pas installé.
"""
import os
import re

# IBAN : 2 lettres pays + 2 chiffres clé + 11 à 30 caractères alphanum (groupés ou non).
_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?)\b")
_DATE_RE = re.compile(r"\b(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{2,4})\b")
_MOIS = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
         "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10,
         "novembre": 11, "décembre": 12, "decembre": 12}
_DATE_TXT_RE = re.compile(r"\b(\d{1,2})\s*(?:er)?\s+(" + "|".join(_MOIS) + r")\s+(\d{4})\b", re.I)


# --- Lecture du texte (best-effort, dégradation propre) ---

def _texte_pdf(contenu):
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(contenu)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages[:5])

def _ocr_image(contenu):
    import io
    from PIL import Image
    import pytesseract
    return pytesseract.image_to_string(Image.open(io.BytesIO(contenu)), lang="fra")

def _ocr_pdf(contenu):
    from pdf2image import convert_from_bytes
    import pytesseract
    pages = convert_from_bytes(contenu, dpi=200, first_page=1, last_page=3)
    return "\n".join(pytesseract.image_to_string(p, lang="fra") for p in pages)

def _texte_docx(contenu):
    import io
    import docx
    return "\n".join(p.text for p in docx.Document(io.BytesIO(contenu)).paragraphs)

def extraire_texte(filename, contenu):
    """Renvoie le texte d'une PJ. PDF texte -> pdfplumber ; PDF scanné / image ->
    Tesseract ; .docx -> python-docx. Renvoie "" si illisible ou libs absentes."""
    ext = os.path.splitext(filename or "")[1].lower()
    try:
        if ext == ".pdf":
            txt = _texte_pdf(contenu)
            if len((txt or "").strip()) < 25:  # PDF probablement scanné -> OCR
                try:
                    txt = _ocr_pdf(contenu) or txt
                except Exception:
                    pass
            return txt or ""
        if ext in (".png", ".jpg", ".jpeg"):
            return _ocr_image(contenu)
        if ext in (".docx", ".doc"):
            return _texte_docx(contenu)
    except Exception:
        return ""
    return ""


# --- Extraction de champs ---

def _normaliser_date(j, m, a):
    a = int(a)
    if a < 100:
        a += 2000
    j, m = int(j), int(m)
    if not (1 <= j <= 31 and 1 <= m <= 12 and 1900 <= a <= 2100):
        return None
    return f"{j:02d}/{m:02d}/{a:04d}"

def _dates_positions(texte):
    """[(position, 'jj/mm/aaaa')] pour les dates numériques ET textuelles."""
    res = []
    for mo in _DATE_RE.finditer(texte):
        d = _normaliser_date(*mo.groups())
        if d:
            res.append((mo.start(), d))
    for mo in _DATE_TXT_RE.finditer(texte):
        d = _normaliser_date(mo.group(1), _MOIS[mo.group(2).lower()], mo.group(3))
        if d:
            res.append((mo.start(), d))
    return res

def _date_apres(texte, mots):
    """Première date qui suit (≤ 80 caractères) l'un des mots-clés."""
    bas = texte.lower()
    dates = _dates_positions(texte)
    for mot in mots:
        i = bas.find(mot)
        while i != -1:
            apres = sorted((pos - i, d) for pos, d in dates if 0 <= pos - i <= 80)
            if apres:
                return apres[0][1]
            i = bas.find(mot, i + 1)
    return None

def _trouver_iban(texte):
    for mo in _IBAN_RE.finditer(texte or ""):
        brut = re.sub(r"\s", "", mo.group(1))
        if 15 <= len(brut) <= 34:
            return brut
    return None

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_TEL_RE = re.compile(r"(?:(?:\+33|0033)\s?|0)[1-9](?:[ .\-]?\d{2}){4}")

def extraire_contact(texte):
    """Extrait e-mail + téléphone (FR) d'un texte de CV -> {email, telephone}.
    Sert à pré-remplir la fiche candidat. Vide si rien trouvé."""
    texte = texte or ""
    em = _EMAIL_RE.search(texte)
    tel = _TEL_RE.search(texte)
    return {
        "email": em.group(0) if em else "",
        "telephone": re.sub(r"\s+", " ", tel.group(0)).strip() if tel else "",
    }


# Contrats (modèles de l'expert-comptable) : durée du travail, emploi, adresse.
_HEBDO_RE = re.compile(
    r"dur[ée]e\s+hebdomadaire.{0,90}?(\d{1,2}(?:[.,]\d{1,2})?)\s*heures", re.I | re.S)
_MENSUEL_RE = re.compile(r"(\d{2,3}(?:[.,]\d{1,2})?)\s*heures\s+par\s+mois", re.I)
_EMPLOI_RE = re.compile(
    r"un\s+emploi\s+d[e'’]\s*([A-Za-zÀ-ÿ'’\- ]{3,60}?)\s*(?:statut|[.,\n])", re.I)
_ADRESSE_RE = re.compile(r"demeurant\s+(.{10,90}?)\s*(?:\n|$)", re.I)


_SALAIRE_RE = re.compile(
    r"r[ée]mun[ée]ration\s+mensuelle\s+brute\s+de\s*([\d\s]+(?:[.,]\d{1,2})?)\s*(?:euros|€)",
    re.I)


def extraire_salaire_mensuel(texte):
    """Salaire mensuel brut (float) mentionné dans un contrat, ou None. Utilisé
    UNIQUEMENT pour le contrôle contrat/promesse d'embauche — jamais proposé ni
    stocké dans les champs du profil (minimisation)."""
    mo = _SALAIRE_RE.search(texte or "")
    if not mo:
        return None
    try:
        return float(mo.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _prop(champ, valeur, apercu, libelle):
    return {"cible": f"profil:{champ}", "valeur": valeur, "apercu": apercu,
            "libelle": libelle, "chiffre": False}


def extraire_champs(type_doc, texte):
    """Renvoie une liste de propositions {cible, valeur, apercu, libelle, chiffre}.
    cible : 'iban' (sensible -> chiffré côté serveur) ou 'profil:<champ>' (en clair).
    Les arrêts de travail et autres types -> [] (aucune donnée de santé)."""
    texte = texte or ""
    t = (type_doc or "").strip()
    props = []

    if "RIB" in t or "bancaire" in t.lower():
        iban = _trouver_iban(texte)
        if iban:
            props.append({"cible": "iban", "valeur": f"…{iban[-4:]}",
                          "apercu": "IBAN détecté sur le RIB", "libelle": "IBAN (RIB)",
                          "chiffre": True})

    elif t in ("Contrat de travail", "Avenant", "Promesse d'embauche"):
        bas = texte.lower()
        # Type : un contrat de conversion CDD -> CDI mentionne les deux, mais
        # seul un CDI parle de « durée indéterminée » -> priorité au CDI.
        cdi = "indéterminée" in bas or "indeterminee" in bas
        cdd = not cdi and ("déterminée" in bas or "determinee" in bas)
        if cdi or cdd:
            tc = "CDI" if cdi else "CDD"
            props.append(_prop("type_contrat", tc, f"Type de contrat : {tc}",
                               "Type de contrat"))
        emploi = _EMPLOI_RE.search(texte)
        if emploi:
            poste = emploi.group(1).strip(" '’-\t")
            props.append(_prop("poste", poste, f"Emploi : {poste}", "Poste / fonction"))
        heures = None
        mh = _HEBDO_RE.search(texte)
        if mh:
            heures = float(mh.group(1).replace(",", "."))
        else:
            mm = _MENSUEL_RE.search(texte)
            if mm:                                   # 151,67 h/mois -> 35 h/sem
                heures = float(mm.group(1).replace(",", ".")) * 12 / 52
        if heures and 2 <= heures <= 48:
            val = f"{round(heures, 2):g}".replace(".", ",")
            props.append(_prop("heures_contractuelles_hebdo", val,
                               f"Durée du travail : {val} h/semaine",
                               "Heures contractuelles / semaine"))
        if cdd:                                      # jamais de date_fin sur un CDI
            fin = _date_apres(texte, ["jusqu'au", "jusqu au", "terme du contrat",
                                      "au terme", "date de fin", "échéance",
                                      "prend fin le"])
            if fin:
                props.append(_prop("date_fin", fin, f"Fin de contrat (CDD) : {fin}",
                                   "Fin de contrat"))
        essai = _date_apres(texte, ["période d'essai", "periode d'essai",
                                    "fin de la période d'essai", "fin d'essai"])
        if essai:
            props.append(_prop("fin_essai", essai, f"Fin de période d'essai : {essai}",
                               "Fin de période d'essai"))
        entree = _date_apres(texte, ["à compter du", "a compter du", "embauché le",
                                     "embauche le", "date d'embauche", "prend effet le",
                                     "débute le", "ancienneté acquise",
                                     "anciennete acquise", "début du contrat",
                                     "debut du contrat"])
        if entree:
            props.append(_prop("date_entree", entree, f"Date d'entrée : {entree}",
                               "Date d'entrée"))
        naissance = _date_apres(texte, ["née le", "né le", "né(e) le",
                                        "date de naissance"])
        if naissance:
            props.append(_prop("naissance", naissance,
                               f"Date de naissance : {naissance}", "Date de naissance"))
        adresse = _ADRESSE_RE.search(texte)
        if adresse:
            adr = re.sub(r"\s+", " ", adresse.group(1)).strip(" .,")
            props.append(_prop("adresse", adr, f"Adresse : {adr}", "Adresse"))

    # Arrêt de travail / Carte vitale / autres : aucune extraction (minimisation).
    return props
