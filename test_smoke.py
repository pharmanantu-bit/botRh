"""Test de fumée — charge chaque page de l'app avec des données réalistes et
vérifie qu'aucune ne plante (HTTP 200). Lancé en CI à chaque push : aurait
attrapé les bugs dashboard/absences avant le déploiement.

Usage : python test_smoke.py   (code de sortie non nul si une page échoue)
"""
import os
import sys
import json
from datetime import datetime

import app as A
import tokens

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
    ("/export_reponses?cle=mauvaise", 403),
]
routes_admin = [
    "/admin", "/admin/dashboard", "/admin/absences", "/admin/historique",
    f"/admin/historique/{mois}/{annee}", "/admin/employes", "/admin/planning",
    "/admin/export", "/admin/erreurs",
]

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

if cree_temp and os.path.exists(fichier_temp):
    os.remove(fichier_temp)

if echecs:
    print(f"\n[ECHEC] {len(echecs)} route(s) en erreur :")
    for x in echecs:
        print("   -", x)
    sys.exit(1)
print("\n[OK] Toutes les pages repondent correctement.")
