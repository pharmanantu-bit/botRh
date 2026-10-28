"""Runner GitHub Actions : envoie les notifications push en attente sur le serveur.

Le serveur PythonAnywhere gratuit ne joint pas les services push (Google, Apple,
Mozilla) : il met les notifications en file. Ce script, lancé après la ronde
quotidienne (agent_ronde.yml) ou sur repository_dispatch « push_admin »
(push_admin.yml), récupère file + abonnements via GET /export_push, envoie
(pywebpush) et rend compte via POST /push_envoyees.

Variables d'environnement : API_CLE, VAPID_PRIVATE_KEY (secrets GitHub),
VAPID_EMAIL (optionnel), BASE_URL (optionnel).
N'affiche jamais le contenu des notifications (peut contenir des prénoms).
"""
import os
import sys
import json
import urllib.request
import urllib.error

import push_rh

BASE_URL = os.environ.get("BASE_URL") or "https://pharmacie92000.pythonanywhere.com"
CLE = os.environ.get("API_CLE") or "botRh-trigger-2026"


def _get(path):
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path, data):
    req = urllib.request.Request(f"{BASE_URL}{path}", data=json.dumps(data).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    cle_privee = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    if not cle_privee:
        print("VAPID_PRIVATE_KEY absente : rien envoyé.")
        return 0
    try:
        data = _get(f"/export_push?cle={CLE}")
    except urllib.error.HTTPError as e:
        print(f"export_push : HTTP {e.code}")
        return 1
    abonnements = data.get("abonnements") or []
    notifications = data.get("notifications") or []
    print(f"{len(notifications)} notification(s) en attente, {len(abonnements)} abonnement(s)")
    if not notifications or not abonnements:
        return 0
    res = push_rh.envoyer_en_attente(abonnements, notifications, cle=cle_privee,
                                     sub_contact=push_rh.contact(), marquer=False)
    print(f"envoyées : {len(res['envoyees'])} · abonnements périmés : {len(res['perimes'])} "
          f"· erreurs : {res['erreurs']}"
          + (f" · codes : {res['codes']}" if res.get("codes") else "")
          + (f" · {res['indisponible']}" if res.get("indisponible") else ""))
    if res["envoyees"] or res["perimes"]:
        rep = _post(f"/push_envoyees?cle={CLE}", {"ids": res["envoyees"], "perimes": res["perimes"]})
        print(f"serveur : {rep.get('marquees')} marquée(s), {rep.get('abonnements')} abonnement(s) restant(s)")
    return 0 if not res["erreurs"] or res["envoyees"] else 1


if __name__ == "__main__":
    sys.exit(main())
