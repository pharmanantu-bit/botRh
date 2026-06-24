"""Lecture IMAP (LECTURE SEULE) de la boîte Gmail pour l'assistant RH.

Le serveur PythonAnywhere gratuit n'ayant pas d'Internet sortant, ce module
s'exécute sur le runner GitHub. Garantie : la boîte est ouverte en `readonly`
=> aucun mail n'est supprimé, déplacé, marqué lu, ni envoyé. On lit, point.

Les pièces jointes ne sont jamais téléchargées : seuls leurs noms sont listés.
"""
import imaplib
import email
import re
from email.header import decode_header
from datetime import datetime, timedelta

IMAP_HOST = "imap.gmail.com"


def _decoder_entete(valeur):
    """Décode un en-tête MIME (=?UTF-8?...?=) en texte lisible."""
    if not valeur:
        return ""
    morceaux = []
    for txt, enc in decode_header(valeur):
        if isinstance(txt, bytes):
            try:
                morceaux.append(txt.decode(enc or "utf-8", "replace"))
            except (LookupError, TypeError):
                morceaux.append(txt.decode("utf-8", "replace"))
        else:
            morceaux.append(txt)
    return "".join(morceaux)


def _payload_texte(part):
    try:
        charset = part.get_content_charset() or "utf-8"
        brut = part.get_payload(decode=True) or b""
        return brut.decode(charset, "replace")
    except Exception:
        return ""


def _html_vers_texte(html):
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser").get_text("\n")
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def _extraire_corps(msg, max_chars=4000):
    """Renvoie (corps_texte_tronqué, [noms_pieces_jointes]). PJ non téléchargées."""
    pieces = []
    corps_txt, corps_html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                nom = _decoder_entete(part.get_filename())
                if nom:
                    pieces.append(nom)
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and not corps_txt:
                corps_txt = _payload_texte(part)
            elif ctype == "text/html" and not corps_html:
                corps_html = _payload_texte(part)
    else:
        if msg.get_content_type() == "text/html":
            corps_html = _payload_texte(msg)
        else:
            corps_txt = _payload_texte(msg)
    corps = corps_txt or _html_vers_texte(corps_html)
    corps = re.sub(r"\n{3,}", "\n\n", corps).strip()
    return corps[:max_chars], pieces


def _adresse_de(from_header):
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", from_header or "")
    return m.group(0).lower() if m else ""


def aplatir_filtres(filtres):
    """{categorie: [adresses ou @domaines]} -> [(categorie, motif)]."""
    return [(cat, m) for cat, motifs in filtres.items() for m in motifs]


def categoriser(adresse, filtres_plats):
    """Catégorie d'une adresse selon les motifs (adresse exacte ou @domaine)."""
    adresse = (adresse or "").lower()
    for cat, motif in filtres_plats:
        motif = (motif or "").lower().strip()
        if not motif:
            continue
        if motif.startswith("@"):
            if adresse.endswith(motif):
                return cat
        elif adresse == motif:
            return cat
    return None


def connecter(user, app_password):
    """Connexion IMAP SSL, boîte ouverte en LECTURE SEULE (readonly=True)."""
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(user, app_password)
    imap.select("INBOX", readonly=True)
    return imap


def lire_mails_rh(user, app_password, filtres, depuis_jours=2, max_mails=25, max_chars=4000):
    """Lit les mails RH des `depuis_jours` derniers jours, restreints aux expéditeurs
    déclarés dans `filtres` ({categorie: [adresses/@domaines]}). Lecture seule.
    Renvoie [{date, from, sujet, corps, pieces_jointes[], categorie}]."""
    filtres_plats = aplatir_filtres(filtres)
    if not filtres_plats:
        return []
    depuis = (datetime.now() - timedelta(days=depuis_jours)).strftime("%d-%b-%Y")
    imap = connecter(user, app_password)
    vus, mails = set(), []
    try:
        for _cat, motif in filtres_plats:
            crit_from = motif[1:] if motif.startswith("@") else motif
            typ, data = imap.search(None, "SINCE", depuis, "FROM", crit_from)
            if typ != "OK" or not data or not data[0]:
                continue
            for uid in data[0].split():
                if uid in vus:
                    continue
                vus.add(uid)
                typ, raw = imap.fetch(uid, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                try:
                    msg = email.message_from_bytes(raw[0][1])
                    frm = _decoder_entete(msg.get("From"))
                    corps, pieces = _extraire_corps(msg, max_chars)
                    mails.append({
                        "date": _decoder_entete(msg.get("Date")),
                        "from": frm,
                        "sujet": _decoder_entete(msg.get("Subject")),
                        "corps": corps,
                        "pieces_jointes": pieces,
                        "categorie": categoriser(_adresse_de(frm), filtres_plats) or _cat,
                    })
                except Exception:
                    continue
                if len(mails) >= max_mails:
                    break
            if len(mails) >= max_mails:
                break
    finally:
        try:
            imap.close()
            imap.logout()
        except Exception:
            pass
    return mails[:max_mails]
