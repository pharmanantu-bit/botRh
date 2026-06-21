"""Génération et validation des jetons employés.

Jeton actuel : HMAC-SHA256(prénom+email, TOKEN_SECRET) — infalsifiable sans
connaître le secret. Jeton « legacy » : ancien MD5, encore accepté pour que les
liens déjà envoyés (ex. juin) continuent de fonctionner pendant la transition.

Importé par app.py (serveur) ET email_sender / relance_sender (runner) : doit
donner exactement le même résultat des deux côtés (même TOKEN_SECRET requis).
"""
import os
import hmac
import hashlib


def _secret():
    return os.getenv("TOKEN_SECRET", "pharmacie-nanterre-2026")


def generer_token(prenom, email):
    """Jeton signé (nouveau) — utilisé pour tous les nouveaux liens."""
    msg = f"{prenom}{email}".encode("utf-8")
    return hmac.new(_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def generer_token_legacy(prenom, email):
    """Ancien jeton MD5 — accepté en lecture pour compat avec les liens envoyés."""
    chaine = f"{prenom}{email}{_secret()}"
    return hashlib.md5(chaine.encode("utf-8")).hexdigest()[:10]


def tokens_valides(prenom, email):
    """Liste des jetons acceptés pour cet employé : le nouveau et l'ancien."""
    return [generer_token(prenom, email), generer_token_legacy(prenom, email)]


def reponse_de(reponses, prenom, email):
    """Retourne la réponse de l'employé, qu'elle soit indexée par le nouveau
    jeton (HMAC) ou l'ancien (MD5). Indispensable pour lire les réponses
    enregistrées avant la bascule des jetons signés."""
    for t in tokens_valides(prenom, email):
        r = reponses.get(t)
        if r is not None:
            return r
    return None


def resoudre_employe(token, employes):
    """Retourne l'employé dont un jeton valide correspond au jeton fourni, sinon
    None. Sert à la fois à valider le jeton (anti-falsification) et à identifier
    l'employé de façon non ambiguë."""
    for emp in employes:
        if token in tokens_valides(emp["prenom"], emp["email"]):
            return emp
    return None
