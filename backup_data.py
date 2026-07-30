"""Sauvegarde automatique des données botRh — exécuté par GitHub Actions.

Récupère toutes les données du serveur et les envoie par mail à l'admin en
PLUSIEURS messages : Gmail refuse les messages > 25 Mo (vécu : échecs SMTP 552
du 26 au 30/07/2026 quand la sauvegarde unique a grossi au-delà, à cause des
fichiers de documents inclus en base64).

 - mail « données (1/N) » : employés, relevés, planning, dossiers RH, index des
   documents, candidats — le critique, envoyé EN PREMIER (part même si l'envoi
   des documents échoue ensuite) ;
 - mails « documents (i/N) » : les fichiers (documents RH, photos, docs
   candidats) répartis en volumes d'environ 12 Mo de binaire.

Chaque pièce jointe est un JSON zippé restaurable INDÉPENDAMMENT et dans
n'importe quel ordre via /admin/sauvegarde (la restauration n'écrase que les
clés présentes dans le fichier).
"""
import io
import json
import os
import smtplib
import urllib.request
import zipfile
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
CLE = os.environ.get("API_CLE") or "botRh-trigger-2026"  # vide (secret absent) -> défaut
ADMIN_EMAIL = "pharmanantu@gmail.com"

# Catégories de fichiers binaires (base64) déportées dans les volumes « documents ».
CATEGORIES_FICHIERS = ("documents_fichiers", "photos_fichiers", "candidats_fichiers")

# ~12 Mo de binaire par volume : le ZIP re-compresse le base64 à peu près à la
# taille binaire, et la pièce jointe encodée reste alors ≈ 16 Mo < 25 Mo Gmail.
VOLUME_MAX = 12 * 1024 * 1024


def decouper_volumes(donnees, volume_max=VOLUME_MAX):
    """Sépare les fichiers binaires du reste. Modifie `donnees` (retire les
    catégories de fichiers) et renvoie une liste de volumes {catégorie: {nom: b64}}
    dont le poids binaire estimé (3/4 du base64) reste sous volume_max."""
    volumes, courant, taille = [], {}, 0
    for cat in CATEGORIES_FICHIERS:
        for nom, b64 in (donnees.pop(cat, None) or {}).items():
            poids = len(b64) * 3 // 4
            if courant and taille + poids > volume_max:
                volumes.append(courant)
                courant, taille = {}, 0
            courant.setdefault(cat, {})[nom] = b64
            taille += poids
    if courant:
        volumes.append(courant)
    return volumes


def preparer_mail(gmail_user, sujet, corps, nom_zip, contenu_json):
    """Construit un message avec le JSON zippé en pièce jointe."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(nom_zip.replace(".zip", ".json"), contenu_json)
    zip_bytes = buf.getvalue()
    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = ADMIN_EMAIL
    msg["Subject"] = sujet
    msg.attach(MIMEText(corps, "plain", "utf-8"))
    part = MIMEBase("application", "zip")
    part.set_payload(zip_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{nom_zip}"')
    msg.attach(part)
    return msg, len(zip_bytes)


def main():
    horodatage = datetime.now().strftime("%Y-%m-%d")

    # 1) Récupérer toutes les données du serveur
    with urllib.request.urlopen(f"{BASE_URL}/export_backup?cle={CLE}", timeout=120) as r:
        donnees = json.loads(r.read().decode("utf-8"))

    # 2) Découper : données critiques d'un côté, fichiers en volumes de l'autre
    volumes = decouper_volumes(donnees)
    total = 1 + len(volumes)

    corps_commun = (
        "Sauvegarde automatique botRh, envoyée en plusieurs e-mails (limite de "
        "taille Gmail).\n\nChaque fichier ZIP est restaurable indépendamment et "
        "dans n'importe quel ordre via Admin > Sauvegarde : restaurez le mail "
        "« données » puis chaque mail « documents ».\n\n"
        "(Sauvegarde quotidienne automatique — botRh)")

    envois = [(f"[Sauvegarde botRh] {horodatage} — données (1/{total})",
               f"botRh_sauvegarde_{horodatage}_donnees.zip",
               json.dumps(donnees, ensure_ascii=False, indent=2))]
    for i, vol in enumerate(volumes, start=2):
        # "employes": [] = compatibilité avec le contrôle de la page de
        # restauration (liste vide -> rien d'écrasé à la restauration).
        charge = {"genere_le": donnees.get("genere_le", horodatage),
                  "volume": f"{i}/{total}", "employes": [], **vol}
        envois.append((f"[Sauvegarde botRh] {horodatage} — documents ({i}/{total})",
                       f"botRh_sauvegarde_{horodatage}_documents_{i - 1}.zip",
                       json.dumps(charge, ensure_ascii=False)))

    # 3) Envoyer (les données critiques partent en premier)
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pwd = os.environ["GMAIL_APP_PASSWORD"]
    echecs = []
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pwd)
        for sujet, nom_zip, contenu in envois:
            msg, poids = preparer_mail(gmail_user, sujet, corps_commun, nom_zip, contenu)
            try:
                server.sendmail(gmail_user, ADMIN_EMAIL, msg.as_string())
                print(f"Envoyé : {sujet} ({poids} octets zippés)")
            except Exception as e:
                print(f"ÉCHEC : {sujet} — {type(e).__name__}: {e}")
                echecs.append(sujet)

    if echecs:
        raise SystemExit(f"{len(echecs)}/{len(envois)} envoi(s) en échec : {', '.join(echecs)}")
    print(f"Sauvegarde complète envoyée à {ADMIN_EMAIL} en {len(envois)} e-mail(s).")


if __name__ == "__main__":
    main()
