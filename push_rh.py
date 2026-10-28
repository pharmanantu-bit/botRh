"""Notifications push (Web Push / VAPID) vers le téléphone ou l'ordinateur de
l'admin — « l'agent a une proposition à confirmer », « la ronde a trouvé un
problème »…

Module SANS dépendance Flask : importé par le serveur (agent_outils.py) ET par
le runner GitHub Actions (run_push.py).

Fonctionnement :
  1. La page /admin/agent enregistre un ABONNEMENT (navigateur) -> push_subscriptions.json
  2. Un événement appelle notifier_admin(titre, corps) -> la notification est mise
     en FILE (push_queue.json) puis on tente l'envoi immédiat.
  3. L'envoi immédiat réussit si pywebpush est installé ET que le serveur joint les
     services push (local, PythonAnywhere payant). Sinon la notification reste en
     file et c'est le runner GitHub Actions (run_push.py, lancé après la ronde ou
     par repository_dispatch) qui la récupère via GET /export_push et l'envoie.

Une seule clé à configurer : VAPID_PRIVATE_KEY (la clé publique en dérive).
Contact VAPID : VAPID_EMAIL (défaut : GMAIL_USER).
"""
import os
import json
import uuid
import base64
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUBS_FILE = os.path.join(BASE_DIR, "push_subscriptions.json")
QUEUE_FILE = os.path.join(BASE_DIR, "push_queue.json")
MAX_FILE = 200              # notifications conservées (les plus récentes)
JOURS_PEREMPTION = 3        # au-delà, une notification non envoyée n'est plus envoyée
JOURS_HISTORIQUE = 7        # notifications envoyées gardées pour consultation
URL_DEFAUT = "/admin/agent"


# --- Stockage -------------------------------------------------------------------

def _lire(f, defaut):
    try:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, type(defaut)) else defaut
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut


def _ecrire(f, data):
    tmp = f + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, f)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _age_jours(ts):
    try:
        return (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M")).days
    except (TypeError, ValueError):
        return 0


# --- Clés VAPID -----------------------------------------------------------------

def cle_privee():
    return (os.getenv("VAPID_PRIVATE_KEY") or "").strip()


def contact():
    c = (os.getenv("VAPID_EMAIL") or os.getenv("GMAIL_USER") or "").strip()
    if not c:
        return ""
    return c if c.startswith("mailto:") else "mailto:" + c


def actif():
    """Vrai si les notifications sont configurées (clé privée présente)."""
    return bool(cle_privee())


def cle_publique():
    """Clé publique (applicationServerKey) au format base64url, dérivée de la
    clé privée si VAPID_PUBLIC_KEY n'est pas fournie. '' si indisponible."""
    pub = (os.getenv("VAPID_PUBLIC_KEY") or "").strip()
    if pub:
        return pub
    priv = cle_privee()
    if not priv:
        return ""
    try:
        from py_vapid import Vapid
        from cryptography.hazmat.primitives import serialization
        v = Vapid.from_string(priv)
        raw = v.public_key.public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    except Exception:
        return ""


def generer_cles():
    """Génère une paire VAPID. Renvoie (privee_b64url, publique_b64url)."""
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization
    v = Vapid()
    v.generate_keys()
    priv_raw = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    pub_raw = v.public_key.public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return b64(priv_raw), b64(pub_raw)


# --- Abonnements ----------------------------------------------------------------

def charger_abonnements():
    return _lire(SUBS_FILE, [])


def ajouter_abonnement(subscription, label=""):
    """Enregistre (ou rafraîchit) l'abonnement d'un navigateur. Renvoie le nombre
    total d'abonnements. subscription = objet PushSubscription.toJSON()."""
    if not isinstance(subscription, dict) or not subscription.get("endpoint"):
        raise ValueError("abonnement invalide")
    keys = subscription.get("keys") or {}
    if not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("abonnement sans clés")
    subs = [s for s in charger_abonnements() if s.get("endpoint") != subscription["endpoint"]]
    subs.append({"endpoint": subscription["endpoint"],
                 "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                 "expirationTime": subscription.get("expirationTime"),
                 "label": (label or "")[:120], "ts": _now()})
    _ecrire(SUBS_FILE, subs)
    return len(subs)


def retirer_abonnement(endpoint):
    subs = charger_abonnements()
    reste = [s for s in subs if s.get("endpoint") != endpoint]
    if len(reste) != len(subs):
        _ecrire(SUBS_FILE, reste)
    return len(reste)


# --- File de notifications ------------------------------------------------------

def charger_file():
    return _lire(QUEUE_FILE, [])


def notifications_en_attente(items=None):
    items = charger_file() if items is None else items
    return [n for n in items if not n.get("envoyee") and _age_jours(n.get("ts")) <= JOURS_PEREMPTION]


def _purger(items):
    garde = []
    for n in items:
        age = _age_jours(n.get("ts"))
        if n.get("envoyee") and age > JOURS_HISTORIQUE:
            continue
        if not n.get("envoyee") and age > JOURS_PEREMPTION:
            continue
        garde.append(n)
    return garde[-MAX_FILE:]


def notifier_admin(titre, corps, url=URL_DEFAUT, tag="botrh-agent", envoyer=True):
    """Met une notification en file puis tente de l'envoyer tout de suite.
    Renvoie {"notification": n, "envoi": résultat de envoyer_en_attente() ou None}."""
    n = {"id": uuid.uuid4().hex[:10], "titre": (titre or "botRh")[:80],
         "corps": (corps or "")[:240], "url": url or URL_DEFAUT, "tag": tag,
         "ts": _now(), "envoyee": False}
    items = _purger(charger_file())
    items.append(n)
    _ecrire(QUEUE_FILE, items)
    envoi = None
    if envoyer:
        try:
            envoi = envoyer_en_attente()
        except Exception as e:      # jamais bloquant pour l'appelant
            envoi = {"envoyees": [], "perimes": [], "erreurs": 1, "detail": type(e).__name__}
    return {"notification": n, "envoi": envoi}


def marquer_envoyees(ids, perimes=()):
    """Marque des notifications comme envoyées et retire les abonnements
    périmés (endpoints répondant 404/410). Renvoie (nb marquées, nb abonnements)."""
    ids = set(ids or [])
    items = charger_file()
    nb = 0
    for n in items:
        if n.get("id") in ids and not n.get("envoyee"):
            n["envoyee"] = _now()
            nb += 1
    _ecrire(QUEUE_FILE, _purger(items))
    subs = charger_abonnements()
    if perimes:
        subs = [s for s in subs if s.get("endpoint") not in set(perimes)]
        _ecrire(SUBS_FILE, subs)
    return nb, len(subs)


# --- Envoi ----------------------------------------------------------------------

def envoyer_en_attente(abonnements=None, notifications=None, cle=None, sub_contact=None,
                       marquer=True):
    """Envoie les notifications en attente à tous les abonnements via pywebpush.
    Renvoie {"envoyees": [ids], "perimes": [endpoints], "erreurs": n, "indisponible": raison?}.
    Sans pywebpush, sans clé, sans abonné ou sans notification : ne fait rien
    (les notifications restent en file pour le runner)."""
    res = {"envoyees": [], "perimes": [], "erreurs": 0}
    notifs = notifications_en_attente() if notifications is None else notifications
    if not notifs:
        return res
    subs = charger_abonnements() if abonnements is None else abonnements
    if not subs:
        res["indisponible"] = "aucun abonnement"
        return res
    cle = cle or cle_privee()
    if not cle:
        res["indisponible"] = "VAPID_PRIVATE_KEY absente"
        return res
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        res["indisponible"] = "pywebpush non installé"
        return res
    claims_sub = sub_contact or contact() or "mailto:admin@example.com"
    perimes = set()
    for n in notifs:
        payload = json.dumps({"titre": n.get("titre"), "corps": n.get("corps"),
                              "url": n.get("url") or URL_DEFAUT, "tag": n.get("tag") or "botrh-agent",
                              "id": n.get("id")}, ensure_ascii=False)
        reussi = 0
        for s in subs:
            if s.get("endpoint") in perimes:
                continue
            try:
                webpush(subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]},
                        data=payload, vapid_private_key=cle,
                        vapid_claims={"sub": claims_sub}, ttl=24 * 3600, timeout=15)
                reussi += 1
            except WebPushException as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code in (404, 410):
                    perimes.add(s["endpoint"])
                else:
                    res["erreurs"] += 1
                    # Code conservé pour le diagnostic : 401/403 sur tous les
                    # envois = clés VAPID divergentes serveur/runner.
                    res.setdefault("codes", []).append(code or "WebPushException")
            except Exception as e:
                res["erreurs"] += 1
                res.setdefault("codes", []).append(type(e).__name__)
        if reussi:
            res["envoyees"].append(n["id"])
    res["perimes"] = sorted(perimes)
    if res.get("codes") and all(c in (401, 403) for c in res["codes"]):
        res["indisponible"] = ("envois refusés (401/403) : VAPID_PRIVATE_KEY probablement "
                               "différente entre le serveur et le secret GitHub — les "
                               "abonnements sont liés à la clé publique dérivée")
    if marquer and (res["envoyees"] or res["perimes"]):
        marquer_envoyees(res["envoyees"], res["perimes"])
    return res
