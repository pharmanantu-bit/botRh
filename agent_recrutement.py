"""Agent conversationnel RECRUTEMENT (function calling) sur les candidats.

Même mécanique que l'agent RH (agent_rh.py) — boucle tool-calling Mistral/Claude/
fake + collecte d'actions confirmables — MAIS sans pseudonymisation : on veut que
l'agent raisonne sur les vraies données candidats (déjà accepté, cf. analyse de CV).
À mentionner dans l'UI (transparence RGPD). Module isolé → l'agent RH n'est pas touché.

Les implémentations d'outils vivent dans recrutement.py (accès aux candidats),
injectées via le callback `executer(nom, args)` -> str (lecture) ou
{resultat, action} (action surfacée à l'UI).
"""
import os
import re
import json

from assistant_rh import _post_json

MAX_TOURS = 5

SYSTEM_RECRUTEMENT = (
    "Tu es un assistant de recrutement pour une pharmacie d'officine en France. Tu "
    "aides le ou la titulaire à trier et suivre ses candidats. Pour toute question sur "
    "les candidats (lister, comparer, chercher une compétence, classer, consulter une "
    "fiche), APPELLE l'outil adapté plutôt que de deviner ; n'invente jamais une donnée.\n"
    "- Pour préparer une CONVOCATION à un entretien ou un REFUS, appelle l'outil "
    "correspondant : il PRÉPARE un brouillon d'e-mail que l'utilisateur relira et "
    "enverra lui-même — ne prétends JAMAIS que c'est envoyé.\n"
    "- Pour des QUESTIONS D'ENTRETIEN, appuie-toi sur la fiche du candidat (outil "
    "fiche_candidat) et propose des questions ciblées sur son profil et le poste.\n"
    "- Réponds en français, de façon factuelle, concise et utile ; synthétise les "
    "résultats des outils au lieu de les recopier bruts. Reste neutre et professionnel "
    "(pas de discrimination ; juge sur les compétences)."
)

# Outils LECTURE + ACTION. params : nom -> (type JSON, description). requis : obligatoires.
OUTILS_SPECS = [
    {"nom": "lister_candidats",
     "description": "Liste les candidats, éventuellement filtrés par statut et/ou poste visé.",
     "params": {"statut": ("string", "Statut (Reçu/À contacter/Entretien/Retenu/Refusé/Embauché), optionnel"),
                "poste": ("string", "Poste visé, optionnel")},
     "requis": []},
    {"nom": "fiche_candidat",
     "description": "Détails d'un candidat : coordonnées, poste, statut, score d'analyse "
                    "IA s'il existe, et extrait du CV. Sert aussi à préparer des questions d'entretien.",
     "params": {"candidat": ("string", "Nom ou prénom du candidat")},
     "requis": ["candidat"]},
    {"nom": "rechercher_candidat",
     "description": "Cherche un mot-clé / une compétence dans le texte des CV (ex. "
                    "« vaccination », « LGPI », « CDD »).",
     "params": {"motcle": ("string", "Mot-clé ou compétence à chercher")},
     "requis": ["motcle"]},
    {"nom": "classer_candidats",
     "description": "Classe les candidats par adéquation (score d'analyse IA décroissant), "
                    "éventuellement pour un poste donné. Signale ceux non encore analysés.",
     "params": {"poste": ("string", "Poste visé, optionnel")},
     "requis": []},
    {"nom": "preparer_mail_convocation",
     "description": "Prépare un brouillon d'e-mail de convocation à un entretien pour un "
                    "candidat. Ne l'envoie PAS.",
     "params": {"candidat": ("string", "Nom ou prénom du candidat"),
                "details": ("string", "Date/heure/lieu proposés (optionnel)")},
     "requis": ["candidat"]},
    {"nom": "preparer_mail_refus",
     "description": "Prépare un brouillon d'e-mail de refus poli et bienveillant pour un "
                    "candidat. Ne l'envoie PAS.",
     "params": {"candidat": ("string", "Nom ou prénom du candidat")},
     "requis": ["candidat"]},
]


def _schema_props(spec):
    return {n: {"type": t, "description": d} for n, (t, d) in spec["params"].items()}

def _tools_mistral():
    return [{"type": "function", "function": {
        "name": s["nom"], "description": s["description"],
        "parameters": {"type": "object", "properties": _schema_props(s), "required": s["requis"]}}}
        for s in OUTILS_SPECS]

def _tools_claude():
    return [{"name": s["nom"], "description": s["description"],
             "input_schema": {"type": "object", "properties": _schema_props(s), "required": s["requis"]}}
            for s in OUTILS_SPECS]


def _exec_outil(nom, args, executer, outils, actions):
    """Exécute un outil. Lecture -> texte. Action -> {resultat, action} : le texte va
    au modèle, l'action (vrai contenu) est accumulée pour l'UI (pas de pseudonymisation)."""
    outils.append(nom)
    try:
        brut = executer(nom, args or {})
    except Exception as e:
        return f"(erreur de l'outil {nom} : {type(e).__name__})"
    if isinstance(brut, dict):
        if brut.get("action"):
            actions.append(brut["action"])
        return str(brut.get("resultat", ""))
    return str(brut)


def _boucle_mistral(systeme, msgs, executer, modele):
    cle = os.getenv("MISTRAL_API_KEY")
    if not cle:
        raise RuntimeError("MISTRAL_API_KEY manquante.")
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {cle}", "Content-Type": "application/json"}
    convo = [{"role": "system", "content": systeme}] + msgs
    outils, actions = [], []
    for _ in range(MAX_TOURS):
        rep = _post_json(url, headers, {"model": modele or "mistral-small-latest",
                                        "messages": convo, "tools": _tools_mistral(),
                                        "tool_choice": "auto", "temperature": 0.2, "max_tokens": 1500})
        message = rep["choices"][0]["message"]
        tcs = message.get("tool_calls")
        if not tcs:
            return message.get("content") or "", outils, actions
        convo.append(message)
        for tc in tcs:
            nom = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            res = _exec_outil(nom, args, executer, outils, actions)
            convo.append({"role": "tool", "tool_call_id": tc.get("id"), "name": nom, "content": res})
    rep = _post_json(url, headers, {"model": modele or "mistral-small-latest",
                                    "messages": convo, "temperature": 0.2, "max_tokens": 1500})
    return rep["choices"][0]["message"].get("content") or "(pas de réponse)", outils, actions


def _boucle_claude(systeme, msgs, executer, modele):
    cle = os.getenv("ANTHROPIC_API_KEY")
    if not cle:
        raise RuntimeError("ANTHROPIC_API_KEY manquante.")
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": cle, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    convo = [{"role": m["role"], "content": m["content"]} for m in msgs]
    outils, actions = [], []
    for _ in range(MAX_TOURS):
        rep = _post_json(url, headers, {"model": modele or "claude-haiku-4-5", "max_tokens": 1500,
                                        "system": systeme, "messages": convo, "tools": _tools_claude()})
        blocks = rep.get("content", [])
        if rep.get("stop_reason") == "tool_use":
            convo.append({"role": "assistant", "content": blocks})
            resultats = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    res = _exec_outil(b["name"], b.get("input", {}), executer, outils, actions)
                    resultats.append({"type": "tool_result", "tool_use_id": b["id"], "content": res})
            convo.append({"role": "user", "content": resultats})
            continue
        texte = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return texte, outils, actions
    return "(trop d'étapes — réponse interrompue)", outils, actions


def _boucle_fake(msgs, executer):
    """Hors-ligne : routeur par mots-clés pour valider la chaîne (tests)."""
    dernier = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    d = dernier.lower()
    outils, actions = [], []
    mo = re.search(r"pour\s+([A-Za-zÀ-ÿ'’-]+)", dernier)
    cand = mo.group(1) if mo else None
    if "convoc" in d and cand:
        nom, args = "preparer_mail_convocation", {"candidat": cand}
    elif ("refus" in d or "refuse" in d) and cand:
        nom, args = "preparer_mail_refus", {"candidat": cand}
    elif any(k in d for k in ("classe", "classement", "trie", "meilleur")):
        nom, args = "classer_candidats", {}
    elif any(k in d for k in ("fiche", "profil")) and cand:
        nom, args = "fiche_candidat", {"candidat": cand}
    elif any(k in d for k in ("cherche", "recherche", "compétence", "competence", "qui a")):
        nom, args = "rechercher_candidat", {"motcle": d.split()[-1]}
    else:
        nom, args = "lister_candidats", {}
    res = _exec_outil(nom, args, executer, outils, actions)
    return f"(mode fake) Résultat de l'outil « {nom} » :\n{res}", outils, actions


def run_agent_recrutement(messages, executer, moteur="mistral", modele=None, roster_txt=""):
    """Lance l'agent recrutement. `executer(nom, args)` : callback (recrutement.py).
    Renvoie {reply, outils_utilises, actions}. PAS de pseudonymisation."""
    msgs = [{"role": m["role"], "content": m.get("content", "")} for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")]
    systeme = SYSTEM_RECRUTEMENT + (f"\n\nCandidats :\n{roster_txt}" if roster_txt else "")
    if moteur == "claude":
        texte, outils, actions = _boucle_claude(systeme, msgs, executer, modele)
    elif moteur == "fake":
        texte, outils, actions = _boucle_fake(msgs, executer)
    else:
        texte, outils, actions = _boucle_mistral(systeme, msgs, executer, modele)
    return {"reply": texte, "outils_utilises": outils, "actions": actions}
