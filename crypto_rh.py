"""Chiffrement au repos (Fernet) des champs sensibles du dossier RH (ex. IBAN).

Clé via la variable d'environnement BOTRH_CRYPTO_KEY (définie dans le .env du
serveur uniquement). Conçu pour une DÉGRADATION PROPRE — ne jamais planter :
  - pas de clé configurée  -> stockage EN CLAIR (utile en dev/local) ;
  - jeton « enc: » illisible (clé absente/incorrecte au moment de lire) ->
    renvoie un marqueur « 🔒 » au lieu de lever une exception.

Générer une clé :
  python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"

⚠️ Perdre la clé = perdre la lecture des champs chiffrés (récupérables depuis le
document d'origine, ex. le RIB). Garder une copie de la clé dans un gestionnaire
de mots de passe, jamais uniquement sur un poste.
"""
import os

PREFIXE = "enc:"


def _fernet():
    """Renvoie une instance Fernet si une clé valide est configurée, sinon None."""
    cle = (os.getenv("BOTRH_CRYPTO_KEY") or "").strip()
    if not cle:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(cle.encode())
    except Exception:
        return None


def crypto_disponible():
    """True si une clé de chiffrement valide est configurée (pour diagnostic)."""
    return _fernet() is not None


def est_chiffre(valeur):
    return isinstance(valeur, str) and valeur.startswith(PREFIXE)


def chiffrer(txt):
    """Chiffre une chaîne -> « enc:... ». Sans clé : renvoie le texte en clair (dev).
    Idempotent : une valeur déjà chiffrée est renvoyée telle quelle."""
    if txt is None or txt == "" or est_chiffre(txt):
        return txt
    f = _fernet()
    if not f:
        return txt  # pas de clé -> clair (dégradation propre)
    return PREFIXE + f.encrypt(str(txt).encode()).decode()


def dechiffrer(valeur):
    """Déchiffre « enc:... » -> texte. Une valeur en clair est renvoyée telle quelle.
    Jeton non déchiffrable (clé manquante/incorrecte) -> « 🔒 »."""
    if not est_chiffre(valeur):
        return valeur
    f = _fernet()
    if not f:
        return "🔒 (clé manquante)"
    try:
        return f.decrypt(valeur[len(PREFIXE):].encode()).decode()
    except Exception:
        return "🔒 (illisible)"
