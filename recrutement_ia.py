"""Analyse IA d'un CV pour le module recrutement.

Contrairement à l'assistant RH (salariés, pseudonymisé), ici on veut analyser le
VRAI CV → pas de pseudonymisation. ⚠️ Le texte du CV est donc envoyé au modèle
(Mistral, hébergé UE, par défaut) : à mentionner dans l'UI (transparence RGPD).

Moteur interchangeable comme assistant_rh : "fake" (hors-ligne, coût nul),
"mistral", "claude". Réutilise le transport `_post_json`/`_extraire_json`.
"""
import os

from assistant_rh import _post_json, _extraire_json

SYSTEM_CV = (
    "Tu es un recruteur RH expérimenté en pharmacie d'officine en France. On te donne "
    "le texte d'un CV et un poste visé. Évalue objectivement l'adéquation du profil au "
    "poste. Sois factuel, concret et bienveillant. N'invente rien qui ne soit pas dans le "
    "CV. Réponds UNIQUEMENT en JSON valide, en français, conforme au schéma demandé."
)

SCHEMA_CV = (
    '{\n'
    '  "resume": "3 à 5 phrases : profil, expérience clé, formation",\n'
    '  "score": 0,            // adéquation au poste sur 100\n'
    '  "adequation": "1 phrase : bien/moyennement/peu adapté et pourquoi",\n'
    '  "points_forts": ["..."],\n'
    '  "points_attention": ["..."]\n'
    '}'
)


def _prompt(texte, poste):
    cv = (texte or "").strip()[:6000] or "(CV illisible / vide)"
    poste = (poste or "non précisé").strip()
    return (f"Poste visé : {poste}\n\n--- Texte du CV ---\n{cv}\n\n"
            f"Analyse l'adéquation au poste et réponds en JSON conforme à ce schéma exact :\n{SCHEMA_CV}")


def _fake(texte, poste):
    return {"resume": "(FAKE) Analyse simulée — aucun appel réseau.",
            "score": 50, "adequation": "(mode test) configure un moteur (Mistral) pour une vraie analyse.",
            "points_forts": [], "points_attention": []}


def _mistral(texte, poste, modele):
    cle = os.getenv("MISTRAL_API_KEY")
    if not cle:
        raise RuntimeError("MISTRAL_API_KEY manquante.")
    rep = _post_json("https://api.mistral.ai/v1/chat/completions",
                     {"Authorization": f"Bearer {cle}", "Content-Type": "application/json"},
                     {"model": modele or "mistral-small-latest",
                      "messages": [{"role": "system", "content": SYSTEM_CV},
                                   {"role": "user", "content": _prompt(texte, poste)}],
                      "response_format": {"type": "json_object"},
                      "temperature": 0.2, "max_tokens": 1200})
    return _extraire_json(rep["choices"][0]["message"]["content"])


def _claude(texte, poste, modele):
    cle = os.getenv("ANTHROPIC_API_KEY")
    if not cle:
        raise RuntimeError("ANTHROPIC_API_KEY manquante.")
    rep = _post_json("https://api.anthropic.com/v1/messages",
                     {"x-api-key": cle, "anthropic-version": "2023-06-01",
                      "Content-Type": "application/json"},
                     {"model": modele or "claude-haiku-4-5", "max_tokens": 1200,
                      "system": SYSTEM_CV,
                      "messages": [{"role": "user", "content": _prompt(texte, poste)}]})
    return _extraire_json(rep["content"][0]["text"])


def analyser_cv(texte, poste, moteur="mistral", modele=None):
    """Analyse un CV face à un poste. Renvoie un dict normalisé
    {resume, score, adequation, points_forts[], points_attention[]}."""
    if moteur == "claude":
        brut = _claude(texte, poste, modele)
    elif moteur == "fake":
        brut = _fake(texte, poste)
    else:
        brut = _mistral(texte, poste, modele)
    try:
        score = int(brut.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    return {
        "resume": str(brut.get("resume", "")).strip(),
        "score": max(0, min(100, score)),
        "adequation": str(brut.get("adequation", "")).strip(),
        "points_forts": [str(x).strip() for x in (brut.get("points_forts") or []) if str(x).strip()],
        "points_attention": [str(x).strip() for x in (brut.get("points_attention") or []) if str(x).strip()],
        # AI Act / RGPD art. 22 : ce résultat est une AIDE à la décision, jamais une
        # décision automatisée. Marqueur tracé et affiché dans l'UI.
        "aide_decision": True,
    }
