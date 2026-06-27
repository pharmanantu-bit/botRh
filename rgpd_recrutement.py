"""Conformité RGPD / AI Act du module recrutement — texte et règles partagés.

Centralisé ici pour que le SERVEUR (mention d'information en mailto, anonymisation)
et le RUNNER GitHub (envoi automatique de l'accusé de réception) utilisent
exactement la même information légale et la même durée de conservation.

Aucune dépendance lourde : importable côté serveur comme côté runner.
"""
import os

# --- Durée de conservation (CNIL : 2 ans max pour un candidat non retenu) ---
DUREE_CONSERVATION_JOURS = 730
DUREE_CONSERVATION_LIBELLE = "2 ans"

# Statuts dont les dossiers NE sont PAS purgés par l'ancienneté :
#  - « Embauché » : devenu salarié, conservation régie par la relation de travail.
STATUTS_HORS_PURGE = {"Embauché"}

# Champs de données personnelles effacés lors de l'anonymisation d'un candidat.
CHAMPS_PII_CANDIDAT = ("nom", "prenom", "email", "telephone", "notes_libres",
                       "cv_texte", "embauche_email")

# --- AI Act : recrutement = système d'IA à haut risque (annexe III) ---
# L'analyse IA est une AIDE à la décision, jamais une décision automatisée.
DISCLAIMER_IA = (
    "Aide à la décision — sans valeur décisionnelle. Le score et le résumé sont "
    "générés par une IA à titre purement indicatif. La présélection et la décision "
    "de recrutement sont prises par une personne (supervision humaine, AI Act art. 14 ; "
    "RGPD art. 22 : pas de décision fondée exclusivement sur un traitement automatisé)."
)


def responsable_contact():
    """Adresse de contact du responsable de traitement (pour exercer ses droits)."""
    return (os.getenv("RGPD_CONTACT") or os.getenv("GMAIL_USER") or "").strip()


def nom_pharmacie():
    return (os.getenv("PHARMACIE_NOM") or "notre pharmacie").strip()


def objet_information(poste=""):
    p = (poste or "").strip()
    return f"Bien reçu — votre candidature{f' ({p})' if p else ''}"


def texte_information(prenom="", poste=""):
    """Corps de l'accusé de réception + mention d'information RGPD (art. 13/14).

    Couvre : finalité, base légale, durée de conservation, droits et réclamation CNIL.
    Utilisé tel quel par l'e-mail auto du runner ET par le mailto manuel de la fiche.
    """
    prenom = (prenom or "").strip()
    poste = (poste or "").strip()
    contact = responsable_contact()
    pharma = nom_pharmacie()
    bonjour = f"Bonjour {prenom}," if prenom else "Bonjour,"
    ref_poste = f" au poste de {poste}" if poste else ""
    lignes = [
        bonjour,
        "",
        f"Nous accusons réception de votre candidature{ref_poste} et vous remercions "
        f"de l'intérêt porté à {pharma}. Votre dossier va être étudié ; nous reviendrons "
        "vers vous quelle que soit notre décision.",
        "",
        "— Information sur vos données personnelles (RGPD) —",
        "",
        f"• Finalité : étude de votre candidature et gestion du recrutement de {pharma}.",
        "• Base légale : mesures précontractuelles prises à votre demande et intérêt "
        "légitime du recruteur.",
        "• Données traitées : celles que vous nous transmettez (identité, coordonnées, "
        "CV et son contenu, échanges).",
        "• Aide à la décision par IA : un outil d'intelligence artificielle peut produire "
        "un résumé et un score indicatifs de votre CV. Ils n'ont aucune valeur "
        "décisionnelle : la sélection est effectuée par une personne. Vous ne faites "
        "l'objet d'aucune décision entièrement automatisée.",
        f"• Durée de conservation : {DUREE_CONSERVATION_LIBELLE} maximum à compter de "
        "notre dernier contact, sauf embauche. Au-delà, vos données sont supprimées ou "
        "anonymisées.",
        "• Vos droits : accès, rectification, effacement, limitation, opposition et "
        "portabilité de vos données.",
        f"  Pour les exercer : {contact or '(contact à préciser)'}.",
        "• Vous pouvez également introduire une réclamation auprès de la CNIL "
        "(www.cnil.fr).",
        "",
        "Cordialement,",
        pharma,
    ]
    return "\n".join(lignes)
