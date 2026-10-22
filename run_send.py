"""Orchestrateur d'envoi exécuté par GitHub Actions.

Le serveur PythonAnywhere gratuit bloque le SMTP sortant ; l'envoi des e-mails
est donc fait ici, sur le runner GitHub (qui a un accès Internet complet).
Le serveur ne sert plus qu'à héberger les pages web et à fournir les réponses.

Modes :
- auto              : décidé chaque matin du 20 au 26 d'après les MARQUEURS
                      d'envoi tenus par le serveur (/export_envois) :
                        · 20→24 : relevés, s'ils n'ont pas encore été envoyés
                                  ce mois-ci (rattrapage si le site était éteint
                                  le 20 — il suffit de le réactiver, l'envoi part
                                  le lendemain matin) ;
                        · 22→24 : relances, si les relevés sont partis depuis
                                  au moins 2 jours et qu'aucune relance n'a été
                                  faite ;
                        · 25    : rappel de validation à l'admin ;
                        · 26    : récap paie.
                      Chaque envoi réussi pose son marqueur sur le serveur :
                      jamais deux envois du même type dans le mois.
- test              : envoie un seul mail de test à l'admin (validation du pipeline)
- releves           : force l'envoi des relevés à tous (pose le marqueur)
- relances          : force l'envoi des relances (pose le marqueur)
- rappel_validation : force le mail à l'admin listant les relevés non validés/manquants
- recap             : force l'envoi du récap Excel paie à l'admin
"""
import sys
import os
import json
import urllib.request
import urllib.error
from datetime import datetime

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
CLE = os.environ.get("API_CLE") or "botRh-trigger-2026"  # vide (secret absent) -> défaut
ADMIN_EMAIL = "pharmanantu@gmail.com"

# Fenêtres du mode auto (jours du mois)
JOUR_RELEVES_DEBUT, JOUR_RELEVES_FIN = 20, 24
JOUR_RELANCES_DEBUT, JOUR_RELANCES_FIN = 22, 24
ECART_MIN_RELANCE = 2   # jours entre l'envoi des relevés et la relance
JOUR_RAPPEL_VALIDATION = 25
JOUR_RECAP = 26

MOIS_FR = {1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril", 5: "Mai", 6: "Juin",
           7: "Juillet", 8: "Août", 9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"}

# Texte d'annonce inséré en haut du mail, activé seulement si INTRO=oui
# (utilisé pour le 1er envoi qui présente le nouveau système en ligne).
INTRO_JUIN = (
    "🆕 Nouveauté : votre feuille d'heures se remplit désormais directement EN LIGNE. "
    "Plus besoin de document à imprimer ou à renvoyer — cliquez simplement sur le lien "
    "ci-dessous et remplissez-la en ligne, exactement comme le relevé que vous remplissiez "
    "auparavant. C'est rapide et tout est enregistré automatiquement."
)


def sync_employes():
    """Récupère la liste des employés gérée sur le serveur (page admin) et
    l'écrit dans le fichier local, pour que l'envoi utilise toujours la liste
    à jour. Le serveur est la source unique de vérité (il exclut déjà les
    collaborateurs inactifs et archivés)."""
    import csv
    import config
    url = f"{BASE_URL}/export_employes?cle={CLE}"
    with urllib.request.urlopen(url, timeout=30) as r:
        employes = json.loads(r.read().decode("utf-8"))
    with open(config.EMPLOYEES_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["nom", "prenom", "email"])
        w.writeheader()
        for e in employes:
            w.writerow({"nom": e.get("nom", ""), "prenom": e.get("prenom", ""),
                        "email": e.get("email", "")})
    print(f"Liste employés synchronisée depuis le serveur : {len(employes)} employé(s).")
    return len(employes)


def etat_envois(mois, annee):
    """Marqueurs d'envoi du mois côté serveur : {"releves": "AAAA-MM-JJ HH:MM",
    "relances": ...} (clé absente = pas encore envoyé). Renvoie None si le
    serveur est injoignable ou éteint (site PythonAnywhere non réactivé)."""
    url = f"{BASE_URL}/export_envois?cle={CLE}&mois={mois}&annee={annee}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # HTTPError 404 (site éteint), URLError, timeout…
        print(f"Serveur injoignable ({type(e).__name__}: {e}).")
        return None


def marquer_envoi(type_envoi, mois, annee):
    """Pose le marqueur « envoyé » sur le serveur. Un échec ici n'annule pas
    l'envoi déjà fait : on le signale seulement (risque = doublon le lendemain)."""
    url = f"{BASE_URL}/marquer_envoi?cle={CLE}&mois={mois}&annee={annee}&type={type_envoi}"
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            etat = json.loads(r.read().decode("utf-8"))
        print(f"Marqueur '{type_envoi}' posé sur le serveur : {etat.get(type_envoi)}")
        return True
    except Exception as e:
        print(f"AVERTISSEMENT : marqueur '{type_envoi}' non enregistré ({e}). "
              f"Vérifier avant tout nouvel envoi ce mois-ci.")
        return False


def jour_du_marqueur(valeur):
    """Jour du mois contenu dans un marqueur 'AAAA-MM-JJ HH:MM' (None si illisible)."""
    try:
        return int(str(valeur)[8:10])
    except (TypeError, ValueError):
        return None


def choisir_mode_auto(jour, etat):
    """Décide quoi faire aujourd'hui en mode auto.
    etat : marqueurs du mois (dict), ou None si le serveur est injoignable."""
    if JOUR_RELEVES_DEBUT <= jour <= JOUR_RELANCES_FIN:
        if etat is None:
            return "serveur_injoignable"
        if not etat.get("releves"):
            return "releves" if jour <= JOUR_RELEVES_FIN else "rien"
        if (JOUR_RELANCES_DEBUT <= jour <= JOUR_RELANCES_FIN and not etat.get("relances")):
            j_releves = jour_du_marqueur(etat["releves"])
            if j_releves is None or jour - j_releves >= ECART_MIN_RELANCE:
                return "relances"
        return "rien"
    if jour == JOUR_RAPPEL_VALIDATION:
        return "rappel_validation"
    if jour == JOUR_RECAP:
        return "recap"
    return "rien"


def _smtp_envoyer(destinataire, msg):
    import smtplib
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pwd = os.environ["GMAIL_APP_PASSWORD"]
    msg["From"] = gmail_user
    msg["To"] = destinataire
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pwd)
        server.sendmail(gmail_user, destinataire, msg.as_string())


def main(argv):
    intro = INTRO_JUIN if os.environ.get("INTRO") == "oui" else None
    mode = argv[1] if len(argv) > 1 and argv[1] else "auto"
    now = datetime.now()
    jour, mois, annee = now.day, now.month, now.year
    mois_annee = f"{MOIS_FR[mois]} {annee}"

    etat = None
    if mode == "auto":
        etat = etat_envois(mois, annee)
        mode = choisir_mode_auto(jour, etat)
        if etat:
            print(f"Marqueurs du mois : {etat}")

    print(f"run_send.py — mode={mode} (jour du mois={jour}) intro={'oui' if intro else 'non'}")

    if mode == "serveur_injoignable":
        print(f"⚠️ Le site {BASE_URL} ne répond pas : impossible d'envoyer les relevés "
              f"(les employés ne pourraient pas les remplir).\n"
              f"ACTION : réactiver le site sur pythonanywhere.com (onglet Web → « Run until… »). "
              f"L'envoi repartira automatiquement demain matin, jusqu'au {JOUR_RELEVES_FIN}.")
        return 1

    if mode == "test":
        from email_sender import send_emails
        send_emails(only_to=ADMIN_EMAIL, intro=intro)
        print(f"Mail de test envoyé à {ADMIN_EMAIL}.")

    elif mode == "releves":
        if etat is None:
            etat = etat_envois(mois, annee) or {}
        if etat.get("releves"):
            print(f"NB : les relevés de {mois_annee} ont déjà été envoyés le {etat['releves']} "
                  f"(envoi forcé → nouvel envoi).")
        sync_employes()
        from email_sender import send_emails
        send_emails(intro=intro)
        marquer_envoi("releves", mois, annee)
        print("Relevés envoyés.")

    elif mode == "relances":
        if etat is None:
            etat = etat_envois(mois, annee) or {}
        if etat.get("relances"):
            print(f"NB : des relances ont déjà été envoyées le {etat['relances']} "
                  f"(envoi forcé → nouvel envoi).")
        sync_employes()
        # Récupérer auprès du serveur la liste de ceux qui ont déjà répondu,
        # puis l'écrire localement pour que relance_sender ne relance que les autres.
        import relance_sender
        url = f"{BASE_URL}/export_reponses?cle={CLE}&mois={mois}&annee={annee}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read().decode("utf-8")
            with open(relance_sender.reponses_file(), "w", encoding="utf-8") as f:
                f.write(data)
            print(f"Réponses récupérées du serveur : {len(json.loads(data))} déjà répondu.")
        except Exception as e:
            print(f"Avertissement : réponses non récupérées ({e}). Tout le monde sera relancé.")
        relance_sender.send_relances()
        marquer_envoi("relances", mois, annee)
        print("Relances envoyées.")

    elif mode == "rappel_validation":
        # Le 25 au soir (saisie clôturée) : mail à l'admin listant les relevés
        # reçus non validés et les manquants, pour tout valider avant le départ
        # automatique du récap paie le 26 au matin. Consomme les exports JSON du
        # serveur en mémoire — pas besoin d'écrire le CSV local.
        from email.mime.text import MIMEText
        import validation_sender
        with urllib.request.urlopen(f"{BASE_URL}/export_employes?cle={CLE}", timeout=30) as r:
            employes = json.loads(r.read().decode("utf-8"))
        with urllib.request.urlopen(
                f"{BASE_URL}/export_reponses?cle={CLE}&mois={mois}&annee={annee}", timeout=30) as r:
            reponses = json.loads(r.read().decode("utf-8"))
        sujet, corps = validation_sender.construire_mail_rappel(employes, reponses, mois_annee)
        msg = MIMEText(corps, "plain", "utf-8")
        msg["Subject"] = sujet
        _smtp_envoyer(ADMIN_EMAIL, msg)
        print(f"Rappel de validation envoyé à l'admin : {sujet}")

    elif mode == "recap":
        # Récap paie : récupère le classeur Excel du mois auprès du serveur et
        # l'envoie à l'admin (le serveur ne pouvant pas envoyer d'e-mail).
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders
        try:
            with urllib.request.urlopen(
                    f"{BASE_URL}/export_reponses?cle={CLE}&mois={mois}&annee={annee}", timeout=30) as r:
                nb = len(json.loads(r.read().decode("utf-8")))
        except Exception:
            nb = "?"
        with urllib.request.urlopen(
                f"{BASE_URL}/export_recap?cle={CLE}&mois={mois}&annee={annee}", timeout=60) as r:
            xlsx = r.read()
        msg = MIMEMultipart()
        msg["Subject"] = f"botRh — Récap des relevés {mois_annee}"
        msg.attach(MIMEText(
            f"Bonjour,\n\nVoici le récapitulatif des relevés d'heures de {mois_annee} "
            f"({nb} reçus). Le fichier Excel est en pièce jointe, prêt pour la paie.\n\nbotRh",
            "plain", "utf-8"))
        part = MIMEBase("application", "octet-stream")
        part.set_payload(xlsx)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f'attachment; filename="Recap_{MOIS_FR[mois]}_{annee}.xlsx"')
        msg.attach(part)
        _smtp_envoyer(ADMIN_EMAIL, msg)
        print(f"Récap paie envoyé à l'admin ({nb} reçus).")

    else:
        print("Rien à faire aujourd'hui.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
