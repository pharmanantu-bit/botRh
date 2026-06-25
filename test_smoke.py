"""Test de fumée — charge chaque page de l'app avec des données réalistes et
vérifie qu'aucune ne plante (HTTP 200). Lancé en CI à chaque push : aurait
attrapé les bugs dashboard/absences avant le déploiement.

Usage : python test_smoke.py   (code de sortie non nul si une page échoue)
"""
import os
import re
import sys
import json
import urllib.parse
from datetime import datetime

import app as A
import tokens
import agent_rh
import crypto_rh
import extraction_pj

A.app.config["TESTING"] = True
client = A.app.test_client()

mois = datetime.now().month
annee = datetime.now().year
employes = A.charger_employes()

# Donnée de test : une réponse pour le 1er employé, pour exercer les recherches
fichier_temp = A.reponses_file(mois, annee)
cree_temp = not os.path.exists(fichier_temp)
if employes and cree_temp:
    e = employes[0]
    tok = tokens.generer_token(e["prenom"], e["email"])
    with open(fichier_temp, "w", encoding="utf-8") as f:
        json.dump({tok: {"prenom": e["prenom"], "heures_plus": 6.0, "heures_moins": 1.5,
                         "commentaire": "smoke", "date_signature": "10/06/2026",
                         "signature": "Test", "date": "10/06/2026 10:00",
                         "mois": mois, "annee": annee}}, f, ensure_ascii=False)

cle = A.API_CLE
prenom0 = employes[0]["prenom"] if employes else "Test"
tok0 = tokens.generer_token(employes[0]["prenom"], employes[0]["email"]) if employes else "x"

routes_publiques = [
    (f"/releve?token={tok0}&prenom={prenom0}", 200),
    (f"/healthcheck?cle={cle}", 200),
    (f"/export_reponses?cle={cle}&mois={mois}&annee={annee}", 200),
    (f"/export_employes?cle={cle}", 200),
    (f"/export_recap?cle={cle}&mois={mois}&annee={annee}", 200),
    (f"/export_backup?cle={cle}", 200),
    ("/export_reponses?cle=mauvaise", 403),
]
routes_admin = [
    "/admin/dashboard", "/admin/mois", f"/admin/mois?mois={mois}&annee={annee}",
    "/admin/historique", f"/admin/historique/{mois}/{annee}",
    "/admin/employes", "/admin/planning", "/admin/export", "/admin/erreurs",
    "/admin/sauvegarde", "/admin/sauvegarde/telecharger", "/admin/assistant",
]
if employes:
    routes_admin.append(f"/admin/employe?email={urllib.parse.quote(employes[0]['email'])}")

# Routes qui redirigent (302) : /admin -> dashboard, absences/synthese fusionnées
routes_admin_redirect = ["/admin", "/admin/absences", "/admin/synthese"]

echecs = []

for url, attendu in routes_publiques:
    code = client.get(url).status_code
    ok = code == attendu
    print(f"{'OK ' if ok else 'KO '}[{code}] {url}")
    if not ok:
        echecs.append(f"{url} (attendu {attendu}, reçu {code})")

with client.session_transaction() as s:
    s["admin"] = True
for url in routes_admin:
    code = client.get(url).status_code
    ok = code == 200
    print(f"{'OK ' if ok else 'KO '}[{code}] {url}")
    if not ok:
        echecs.append(f"{url} (attendu 200, reçu {code})")

for url in routes_admin_redirect:
    code = client.get(url).status_code
    ok = code in (301, 302)
    print(f"{'OK ' if ok else 'KO '}[{code}] {url} (redirection attendue)")
    if not ok:
        echecs.append(f"{url} (attendu 302, reçu {code})")

# --- Agent RH outillé : chaîne complète hors-ligne (moteur fake, coût nul) ---
# Vérifie : qu'un outil est déclenché, que le prénom tapé par l'utilisateur est
# pseudonymisé (étiquette « Employé X ») AVANT d'atteindre l'outil — donc le modèle
# ne verrait jamais le vrai nom — et que la réponse finale est ré-identifiée.
if employes:
    captures = []
    def _executer_spy(nom, args, annuaire):
        captures.append((nom, dict(args or {})))
        return A.executer_outil_agent(nom, args, annuaire)

    res = agent_rh.run_agent(
        [{"role": "user", "content": f"la fiche de {prenom0}"}],
        employes, _executer_spy, moteur="fake")

    if not res.get("outils_utilises"):
        echecs.append("agent fake : aucun outil déclenché")
        print("KO [--] agent fake : aucun outil déclenché")
    else:
        ok_label = True
        for nom, args in captures:
            if nom == "profil_salarie":
                emp = args.get("employe", "")
                if not re.match(r"Employé [A-Z]+$", emp) or prenom0.lower() in emp.lower():
                    ok_label = False
                    echecs.append(f"agent fake : nom non pseudonymisé dans l'outil ({emp!r})")
        # La réponse doit être ré-identifiée (le prénom réel réapparaît à l'affichage).
        ok_reid = prenom0 in res.get("reply", "")
        if not ok_reid:
            echecs.append("agent fake : réponse non ré-identifiée (prénom absent)")
        statut = "OK " if (ok_label and ok_reid) else "KO "
        print(f"{statut}[--] agent fake (pseudonymisation + ré-identification) "
              f"· outils={res['outils_utilises']}")

# --- Phase 1 : crypto + extraction OCR + propositions (hors-ligne, données isolées) ---
# Chiffrement au repos : round-trip (sans clé -> clair ; avec clé -> enc:).
ok_crypto = crypto_rh.dechiffrer(crypto_rh.chiffrer("FR7630004")) == "FR7630004"
print(("OK " if ok_crypto else "KO ") + "[--] crypto_rh round-trip")
if not ok_crypto:
    echecs.append("crypto_rh : round-trip KO")

# Extraction : RIB -> IBAN, contrat -> dates, ARRÊT -> rien (aucune donnée de santé).
champs_rib = extraction_pj.extraire_champs("RIB / coordonnées bancaires",
                                           "IBAN FR76 3000 4000 0500 0012 3456 789 BIC X")
champs_contrat = extraction_pj.extraire_champs("Contrat de travail",
                                               "prend effet le 01/09/2026 jusqu'au 31/12/2026")
champs_arret = extraction_pj.extraire_champs("Arrêt de travail", "repos jusqu'au 20/06/2026 maladie")
ok_ext = (any(c["cible"] == "iban" for c in champs_rib)
          and any(c["cible"] == "profil:date_fin" for c in champs_contrat)
          and champs_arret == [])
print(("OK " if ok_ext else "KO ") + "[--] extraction (RIB+contrat extraits, arrêt ignoré)")
if not ok_ext:
    echecs.append("extraction_pj : champs inattendus")

# Propositions : ajout + appliquer, sur un fichier profils TEMPORAIRE (vraies données intactes).
if employes:
    email0 = employes[0]["email"]
    _orig_pf = A.PROFILS_FILE
    _tmp_pf = os.path.join(A.BASE_DIR, "_smoke_profils_tmp.json")
    A.PROFILS_FILE = _tmp_pf
    try:
        A.sauvegarder_profils({email0: {}})
        A._ajouter_propositions(email0, [
            {"cible": "profil:date_fin", "valeur": "31/12/2026",
             "apercu": "Fin : 31/12/2026", "libelle": "Fin de contrat", "chiffre": False},
            {"cible": "iban", "valeur": "…6789", "apercu": "IBAN", "libelle": "IBAN", "chiffre": True},
        ], "docSMOKE")
        prof = A.profil_de(email0)
        pid = next((p["id"] for p in prof.get("propositions", []) if p["cible"] == "profil:date_fin"), None)
        with client.session_transaction() as s:
            s["admin"] = True
            s["_csrf_token"] = "tok"
        client.post("/admin/employe/proposition/appliquer",
                    data={"email": email0, "prop_id": pid, "csrf_token": "tok"})
        prof2 = A.profil_de(email0)
        ok_prop = (len(prof.get("propositions", [])) == 2
                   and prof2.get("date_fin") == "31/12/2026"
                   and not any(p["id"] == pid for p in prof2.get("propositions", [])))
        print(("OK " if ok_prop else "KO ") + "[--] propositions (ajout + appliquer date_fin)")
        if not ok_prop:
            echecs.append("propositions : ajout/appliquer KO")
    finally:
        A.PROFILS_FILE = _orig_pf
        A._JSON_CACHE.pop(_tmp_pf, None)
        if os.path.exists(_tmp_pf):
            os.remove(_tmp_pf)

if cree_temp and os.path.exists(fichier_temp):
    os.remove(fichier_temp)

if echecs:
    print(f"\n[ECHEC] {len(echecs)} route(s) en erreur :")
    for x in echecs:
        print("   -", x)
    sys.exit(1)
print("\n[OK] Toutes les pages repondent correctement.")
