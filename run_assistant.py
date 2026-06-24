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
import urllib.request
from datetime import datetime
import config

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
CLE = os.environ.get("API_CLE", "botRh-trigger-2026")


def _liste(val):
    return [x.strip() for x in (val or "").split(",") if x.strip()]


def charger_emails_employes():
    """Adresses des employés : serveur (source de vérité) sinon CSV local."""
    try:
        with urllib.request.urlopen(f"{BASE_URL}/export_employes?cle={CLE}", timeout=30) as r:
            emp = json.loads(r.read().decode("utf-8"))
        return [e["email"].strip().lower() for e in emp if e.get("email")]
    except Exception as e:
        print(f"  (liste serveur indisponible : {e} — repli sur le CSV local)")
        import csv
        for nom in ("employees_live.csv", "employees.csv"):
            p = os.path.join(os.path.dirname(__file__), nom)
            if os.path.exists(p):
                with open(p, newline="", encoding="utf-8") as f:
                    return [row["email"].strip().lower()
                            for row in csv.DictReader(f) if row.get("email")]
        return []


def construire_filtres():
    filtres = {}
    if _liste(config.COMPTA_EMAILS):
        filtres["compta"] = _liste(config.COMPTA_EMAILS)
    if _liste(config.PLANNING_SENDER):
        filtres["planning"] = _liste(config.PLANNING_SENDER)
    if _liste(config.ADMIN_RH_DOMAINS):
        filtres["admin_rh"] = _liste(config.ADMIN_RH_DOMAINS)
    emails_emp = charger_emails_employes()
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
    filtres = construire_filtres()
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
                          max_mails=maxm, max_chars=4000)
    print(f"{len(mails)} mail(s) RH trouvé(s) sur {config.ASSISTANT_JOURS} jour(s) :")
    for m in mails:
        pj = f" [{len(m['pieces_jointes'])} PJ]" if m["pieces_jointes"] else ""
        print(f"  - [{m['categorie']}] {m['from'][:45]} | {m['sujet'][:60]}{pj}")

    if dry:
        print("DRY-RUN : aucun appel IA, aucun push. Terminé.")
        return

    if os.environ.get("ASSISTANT_FAKE") == "1":
        resume = {"resume_texte": f"(FAKE) {len(mails)} mail(s) analysé(s) — test sans IA.",
                  "taches_a_faire": [], "a_mettre_en_place": [], "echeances": [],
                  "alertes": [], "_meta": {"nb_mails": len(mails), "modele": "fake"}}
    else:
        from assistant_rh import analyser
        resume = analyser(mails, modele=config.ASSISTANT_MODELE)

    resume["date"] = datetime.now().strftime("%Y-%m-%d")
    resume["genere_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    pousser(resume)


if __name__ == "__main__":
    main()
