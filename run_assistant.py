"""Assistant RH — exécuté sur le runner GitHub (qui a l'accès Internet).

Lit les mails RH pertinents (IMAP, LECTURE SEULE), les fait analyser par Claude,
puis pousse le résumé du jour vers le serveur (POST /assistant_push?cle=).

Usage :
  python run_assistant.py                  # complet : IMAP + IA + push
  python run_assistant.py --dry-run        # lit les mails, AUCUN appel IA, AUCUN push
  python run_assistant.py --max 5          # limite le nombre de mails
  ASSISTANT_FAKE=1 python run_assistant.py # résumé bidon (teste le push sans IA ni coût)
"""
import sys
import os
import json
import hashlib
import urllib.request
from datetime import datetime
import config

EXT_DOCS_OK = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".doc"}

BASE_URL = os.environ.get("ASSISTANT_BASE_URL", "https://pharmacie92000.pythonanywhere.com")
CLE = os.environ.get("API_CLE") or "botRh-trigger-2026"  # vide (secret absent) -> défaut


def _liste(val):
    return [x.strip() for x in (val or "").split(",") if x.strip()]


def deviner_type(nom, sujet):
    """Devine le type de document RH à partir du nom de fichier et de l'objet du mail."""
    t = (nom + " " + sujet).lower()
    paires = [
        (("arret", "arrêt", "maladie", "cpam", "prolongation"), "Arrêt de travail"),
        (("rib", "iban", "bancaire"), "RIB / coordonnées bancaires"),
        (("avenant",), "Avenant"),
        (("contrat",), "Contrat de travail"),
        (("vitale",), "Carte vitale"),
        (("identite", "identité", "cni", "passeport", "sejour", "séjour"), "Pièce d'identité"),
        (("domicile",), "Justificatif de domicile"),
        (("diplome", "diplôme"), "Diplôme"),
        (("attestation", "formation"), "Attestation de formation"),
    ]
    for mots, typ in paires:
        if any(m in t for m in mots):
            return typ
    return "Autre / divers"


def pieces_classables(mails, employes):
    """PJ provenant d'un SALARIÉ connu, classables dans son dossier (types autorisés)."""
    from mail_reader import _adresse_de
    par_email = {e["email"].lower(): e for e in employes if e.get("email")}
    out = []
    for m in mails:
        if m.get("categorie") != "employes":
            continue
        emp = par_email.get(_adresse_de(m.get("from", "")))
        if not emp:
            continue
        for pj in m.get("pj_data", []):
            if os.path.splitext(pj["filename"])[1].lower() not in EXT_DOCS_OK:
                continue
            out.append({
                "email": emp["email"], "prenom": emp.get("prenom", ""),
                "filename": pj["filename"], "content": pj["content"],
                "type": deviner_type(pj["filename"], m.get("sujet", "")),
                "sha": hashlib.sha256(pj["content"]).hexdigest(),
                "message_id": m.get("message_id", ""),
            })
    return out


def classer_pieces(mails, employes):
    """Envoie au serveur les PJ des salariés pour classement auto (anti-doublon côté serveur)."""
    import base64
    candidats = pieces_classables(mails, employes)
    if not candidats:
        print("Aucune pièce jointe de salarié à classer.")
        return
    ajout = doublon = autre = 0
    for c in candidats:
        # OCR + extraction LOCALE (best-effort) : champs utiles -> propositions à
        # valider côté serveur. Aucune donnée de santé (arrêts non lus). Un échec
        # n'empêche jamais le classement de la PJ.
        try:
            from extraction_pj import extraire_texte, extraire_champs
            extraction = extraire_champs(c["type"], extraire_texte(c["filename"], c["content"]))
        except Exception as e:
            extraction = []
            print(f"  (extraction ignorée pour {c['filename']} : {e})")
        payload = {"email": c["email"], "filename": c["filename"], "type": c["type"],
                   "libelle": c["filename"], "sha": c["sha"], "message_id": c["message_id"],
                   "content_b64": base64.b64encode(c["content"]).decode(),
                   "extraction": extraction}
        try:
            req = urllib.request.Request(
                f"{BASE_URL}/document_push?cle={CLE}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "botRh"})
            with urllib.request.urlopen(req, timeout=60) as r:
                st = json.loads(r.read().decode("utf-8")).get("status", "?")
        except Exception as e:
            st = f"erreur ({e})"
        ajout += st == "ajoute"
        doublon += st == "doublon"
        autre += st not in ("ajoute", "doublon")
        print(f"  PJ {c['prenom']} / {c['filename']} -> {st}")
    print(f"Classement PJ : {ajout} ajout(s), {doublon} doublon(s), {autre} autre(s).")


def _parse_nom_expediteur(from_header):
    """« Marie Leroy <x@y> » -> ('Marie', 'Leroy'). Sinon prénom = nom affiché entier."""
    import re
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', from_header or "")
    complet = (m.group(1).strip() if m else "")
    parts = complet.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return complet, ""


def classer_candidatures(label="Recrutement"):
    """Lit le LABEL Gmail « Recrutement » : chaque mail avec un CV en PJ devient un
    candidat (POST /candidat_push, anti-doublon par e-mail côté serveur). OCR/extraction
    du texte faits ICI (le runner a Tesseract)."""
    import base64
    from mail_reader import lire_mails_label, _adresse_de
    user = os.environ.get("GMAIL_USER") or config.GMAIL_USER
    pwd = os.environ.get("GMAIL_APP_PASSWORD") or config.GMAIL_APP_PASSWORD
    if not user or not pwd:
        return
    try:
        mails = lire_mails_label(user, pwd, label, depuis_jours=config.ASSISTANT_JOURS,
                                 max_mails=config.ASSISTANT_MAX_MAILS)
    except Exception as e:
        print(f"Candidatures : lecture du label « {label} » impossible ({e}).")
        return
    if not mails:
        print(f"Aucun mail dans le label « {label} ».")
        return
    try:
        from extraction_pj import extraire_texte, extraire_contact
    except Exception:
        extraire_texte = extraire_contact = None
    ajout = doublon = autre = 0
    for m in mails:
        pj = next((p for p in m.get("pj_data", [])
                   if os.path.splitext(p["filename"])[1].lower() in EXT_DOCS_OK), None)
        if not pj:
            continue
        email_exp = _adresse_de(m.get("from", ""))
        prenom, nom = _parse_nom_expediteur(m.get("from", ""))
        cv_texte, tel = "", ""
        if extraire_texte:
            try:
                cv_texte = extraire_texte(pj["filename"], pj["content"])
                if extraire_contact:
                    ct = extraire_contact(cv_texte)
                    email_exp = email_exp or ct.get("email", "")
                    tel = ct.get("telephone", "")
            except Exception:
                pass
        payload = {"prenom": prenom, "nom": nom, "email": email_exp, "telephone": tel,
                   "sujet": m.get("sujet", ""), "filename": pj["filename"],
                   "content_b64": base64.b64encode(pj["content"]).decode(),
                   "cv_texte": cv_texte, "message_id": m.get("message_id", "")}
        try:
            req = urllib.request.Request(f"{BASE_URL}/candidat_push?cle={CLE}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "botRh"})
            with urllib.request.urlopen(req, timeout=60) as r:
                st = json.loads(r.read().decode("utf-8")).get("status", "?")
        except Exception as e:
            st = f"erreur ({e})"
        ajout += st == "ajoute"
        doublon += st == "doublon"
        autre += st not in ("ajoute", "doublon")
        print(f"  Candidature {prenom} {nom} <{email_exp}> -> {st}")
    print(f"Candidatures : {ajout} ajout(s), {doublon} doublon(s), {autre} autre(s).")


def charger_employes_complet():
    """Employés (prénom, nom, email) : serveur (source de vérité) sinon CSV local.
    Sert au filtrage IMAP ET à la pseudonymisation (option B)."""
    try:
        with urllib.request.urlopen(f"{BASE_URL}/export_employes?cle={CLE}", timeout=30) as r:
            emp = json.loads(r.read().decode("utf-8"))
        return [{"prenom": e.get("prenom", ""), "nom": e.get("nom", ""),
                 "email": (e.get("email", "") or "").strip()} for e in emp if e.get("email")]
    except Exception as e:
        print(f"  (liste serveur indisponible : {e} — repli sur le CSV local)")
        import csv
        for nom in ("employees_live.csv", "employees.csv"):
            p = os.path.join(os.path.dirname(__file__), nom)
            if os.path.exists(p):
                with open(p, newline="", encoding="utf-8") as f:
                    return [{"prenom": row.get("prenom", ""), "nom": row.get("nom", ""),
                             "email": (row.get("email", "") or "").strip()}
                            for row in csv.DictReader(f) if row.get("email")]
        return []


def construire_filtres(employes):
    filtres = {}
    if _liste(config.COMPTA_EMAILS):
        filtres["compta"] = _liste(config.COMPTA_EMAILS)
    if _liste(config.PLANNING_SENDER):
        filtres["planning"] = _liste(config.PLANNING_SENDER)
    if _liste(config.ADMIN_RH_DOMAINS):
        filtres["admin_rh"] = _liste(config.ADMIN_RH_DOMAINS)
    emails_emp = [e["email"].lower() for e in employes if e.get("email")]
    if emails_emp:
        filtres["employes"] = emails_emp
    return filtres


def pousser(resume):
    data = json.dumps(resume, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{BASE_URL}/assistant_push?cle={CLE}", data=data,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "botRh"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("Push:", r.status, r.read().decode("utf-8"))


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    maxm = config.ASSISTANT_MAX_MAILS
    if "--max" in args:
        try:
            maxm = int(args[args.index("--max") + 1])
        except (ValueError, IndexError):
            pass

    user = os.environ.get("GMAIL_USER") or config.GMAIL_USER
    pwd = os.environ.get("GMAIL_APP_PASSWORD") or config.GMAIL_APP_PASSWORD
    employes = charger_employes_complet()
    filtres = construire_filtres(employes)
    print(f"run_assistant — dry_run={dry} max={maxm} jours={config.ASSISTANT_JOURS}")
    print("Filtres:", {k: len(v) for k, v in filtres.items()})
    if not filtres:
        print("Aucun expéditeur configuré (COMPTA_EMAILS / PLANNING_SENDER / ...). Rien à lire.")
        return
    if not user or not pwd:
        print("GMAIL_USER / GMAIL_APP_PASSWORD manquants.")
        return

    from mail_reader import lire_mails_rh
    mails = lire_mails_rh(user, pwd, filtres, depuis_jours=config.ASSISTANT_JOURS,
                          max_mails=maxm, max_chars=4000, avec_pj=True)
    print(f"{len(mails)} mail(s) RH trouvé(s) sur {config.ASSISTANT_JOURS} jour(s) :")
    for m in mails:
        pj = f" [{len(m['pieces_jointes'])} PJ]" if m["pieces_jointes"] else ""
        print(f"  - [{m['categorie']}] {m['from'][:45]} | {m['sujet'][:60]}{pj}")

    if dry:
        candidats = pieces_classables(mails, employes)
        print(f"DRY-RUN : {len(candidats)} pièce(s) jointe(s) de salarié seraient classée(s) :")
        for c in candidats:
            print(f"  -> {c['prenom']} : {c['filename']} (type deviné : {c['type']})")
            try:
                from extraction_pj import extraire_texte, extraire_champs
                champs = extraire_champs(c["type"], extraire_texte(c["filename"], c["content"]))
                for ch in champs:
                    print(f"       · extrait : {ch['libelle']} = {ch['apercu']}")
                if not champs:
                    print("       · (aucun champ extrait / type non concerné)")
            except Exception as e:
                print(f"       · extraction indisponible ({e})")
        print("DRY-RUN : aucun appel IA, aucun push, aucun classement. Terminé.")
        return

    from assistant_rh import analyser
    moteur = "fake" if os.environ.get("ASSISTANT_FAKE") == "1" else config.ASSISTANT_MOTEUR
    print(f"Analyse — moteur={moteur} (identités pseudonymisées avant envoi)")
    resume = analyser(mails, employes=employes, extra_noms=_liste(config.EXTRA_NOMS),
                      moteur=moteur, modele=config.ASSISTANT_MODELE or None)

    resume["date"] = datetime.now().strftime("%Y-%m-%d")
    resume["genere_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    pousser(resume)

    # Classement automatique des pièces jointes des salariés dans leur dossier.
    classer_pieces(mails, employes)

    # Création auto des candidats depuis le label Gmail « Recrutement ».
    try:
        classer_candidatures()
    except Exception:
        print("Candidatures : étape ignorée (erreur).")


if __name__ == "__main__":
    main()
