"""Agent RH outillé (function calling) — le chaînon « conseil → action ».

L'assistant peut désormais LIRE les données de l'officine via des outils :
relevés manquants, fiche salarié, échéances, écart heures/planning, annuaire.

Principe RGPD (identique au reste de botRh, « option B ») : la boucle agent
tourne ENTIÈREMENT côté serveur, là où vivent les données. Seul le RAISONNEMENT
passe par le LLM. Avant tout envoi, les identités sont pseudonymisées
(« Employé A/B... ») — dans les messages de l'utilisateur ET dans les résultats
d'outils. Le modèle ne voit donc jamais un vrai nom. À l'affichage, on
ré-identifie en local (les étiquettes redeviennent les prénoms).

Périmètre : LECTURE SEULE. Aucune écriture/suppression. Les implémentations
d'outils vivent dans app.py (accès aux données) et sont injectées ici via le
callback `executer(nom, args, annuaire)` — pas d'import circulaire : ce module
n'importe que assistant_rh.
"""
import os
import re
import json

from assistant_rh import (
    construire_table, annuaire_pseudo, pseudonymiser_texte, reidentifier, _post_json,
)

MAX_TOURS = 5  # borne le nombre d'allers-retours d'outils (coût/latence)

SYSTEM_AGENT = (
    "Tu es l'assistant RH d'une pharmacie d'officine en France : à la fois expert "
    "RH / droit du travail (Code du travail, CCN pharmacie d'officine IDCC 1996) ET "
    "agent capable d'AGIR sur les données de l'officine grâce à des outils.\n"
    "RÈGLES :\n"
    "- Pour toute question portant sur les salariés, les relevés d'heures, les "
    "échéances ou le planning, APPELLE l'outil adapté plutôt que de deviner. "
    "N'invente jamais une donnée : si un outil ne renvoie rien, dis-le.\n"
    "- Les salariés sont anonymisés en « Employé A », « Employé B »... Utilise ces "
    "étiquettes telles quelles (dans tes appels d'outils comme dans ta réponse) ; "
    "n'écris JAMAIS de nom de famille. L'affichage ré-identifiera localement.\n"
    "- Pour les questions purement juridiques/RH (sans donnée nominative), réponds "
    "directement, en français, de façon concrète et actionnable, et signale quand "
    "un point délicat relève de l'avocat ou de l'expert-comptable. Information "
    "générale, pas un conseil juridique engageant. Aucun conseil médical.\n"
    "- Pour PRÉPARER une action (relance d'un retardataire, attestation de travail, "
    "note au journal, mail à un salarié), APPELLE l'outil action correspondant "
    "(preparer_relance / preparer_attestation / proposer_note_journal / preparer_mail). "
    "Ces outils ne font que PRÉPARER une proposition que l'utilisateur confirmera d'un "
    "clic : ne prétends JAMAIS que c'est envoyé, ajouté ou fait. Annonce simplement que "
    "le brouillon/le document est prêt à valider.\n"
    "- Réponds toujours en français, clairement, et synthétise le résultat des "
    "outils au lieu de le recracher brut."
)

# --- Catalogue d'outils (format neutre, converti par moteur) ---
# Lecture seule + ACTION (préparent un livrable à confirmer). `params` : nom ->
# (type JSON, description). `requis` : args obligatoires.
OUTILS_SPECS = [
    {
        "nom": "releves_manquants",
        "description": "Liste les salariés actifs qui n'ont PAS encore rendu leur "
                       "relevé d'heures pour un mois. Sans argument : mois en cours.",
        "params": {
            "mois": ("integer", "Mois 1-12 (optionnel, défaut = mois courant)"),
            "annee": ("integer", "Année (optionnel, défaut = année courante)"),
        },
        "requis": [],
    },
    {
        "nom": "profil_salarie",
        "description": "Fiche d'un salarié : poste, type de contrat, dates clés "
                       "(entrée, fin de CDD, fin de période d'essai, prochaine visite "
                       "médicale), alertes en cours et documents obligatoires manquants.",
        "params": {
            "employe": ("string", "Étiquette du salarié, ex. « Employé A »"),
        },
        "requis": ["employe"],
    },
    {
        "nom": "echeances_a_venir",
        "description": "Échéances RH à venir sur tous les salariés actifs : fins de "
                       "CDD, fins de période d'essai, visites médicales, documents qui "
                       "expirent, anniversaires d'ancienneté.",
        "params": {
            "jours": ("integer", "Horizon en jours (optionnel ; informatif, les "
                                 "alertes ont déjà leur propre seuil)"),
        },
        "requis": [],
    },
    {
        "nom": "lister_employes",
        "description": "Annuaire des salariés actifs (étiquette, poste, type de contrat).",
        "params": {},
        "requis": [],
    },
    # --- Outils ACTION : préparent un livrable à CONFIRMER (jamais d'envoi/écriture auto) ---
    {
        "nom": "preparer_relance",
        "description": "Prépare un brouillon d'e-mail de relance (rappel du relevé "
                       "d'heures) pour un salarié retardataire. Ne l'envoie PAS : produit "
                       "un brouillon que l'utilisateur relira et enverra lui-même.",
        "params": {"employe": ("string", "Étiquette du salarié, ex. « Employé A »")},
        "requis": ["employe"],
    },
    {
        "nom": "preparer_attestation",
        "description": "Prépare l'attestation de travail (page imprimable pré-remplie) "
                       "d'un salarié. Produit un lien à ouvrir, n'imprime ni n'envoie rien.",
        "params": {"employe": ("string", "Étiquette du salarié")},
        "requis": ["employe"],
    },
    {
        "nom": "proposer_note_journal",
        "description": "Propose l'ajout d'une note datée au journal RH d'un salarié "
                       "(ex. entretien, augmentation, avertissement). N'écrit RIEN : "
                       "l'utilisateur confirmera l'ajout d'un clic.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "note": ("string", "Texte de la note"),
                   "type_evenement": ("string", "Type : Entretien, Augmentation, "
                                                 "Avertissement, Formation, Congés, Autre (optionnel)")},
        "requis": ["employe", "note"],
    },
    {
        "nom": "preparer_mail",
        "description": "Rédige un brouillon d'e-mail RH libre à un salarié (convocation, "
                       "information…). Ne l'envoie PAS : brouillon à relire et envoyer soi-même.",
        "params": {"employe": ("string", "Étiquette du salarié"),
                   "sujet": ("string", "Objet du mail"),
                   "corps": ("string", "Corps du mail")},
        "requis": ["employe", "corps"],
    },
]


def _schema_props(spec):
    return {nom: {"type": t, "description": desc} for nom, (t, desc) in spec["params"].items()}


def _tools_mistral():
    return [{
        "type": "function",
        "function": {
            "name": s["nom"], "description": s["description"],
            "parameters": {"type": "object", "properties": _schema_props(s),
                           "required": s["requis"]},
        },
    } for s in OUTILS_SPECS]


def _tools_claude():
    return [{
        "name": s["nom"], "description": s["description"],
        "input_schema": {"type": "object", "properties": _schema_props(s),
                         "required": s["requis"]},
    } for s in OUTILS_SPECS]


def _exec_outil(nom, args, annuaire, table, executer, outils, actions):
    """Exécute un outil en local. Outil LECTURE -> texte (re-pseudonymisé pour le
    modèle). Outil ACTION -> dict {resultat, action} : le texte va au modèle, et
    l'`action` (vrai contenu, surfacée à l'UI uniquement, JAMAIS au LLM) est
    accumulée à part."""
    outils.append(nom)
    try:
        brut = executer(nom, args or {}, annuaire)
    except Exception as e:
        brut = f"(erreur de l'outil {nom} : {type(e).__name__})"
    if isinstance(brut, dict):
        act = brut.get("action")
        if act:
            actions.append(act)
        brut = brut.get("resultat", "")
    return pseudonymiser_texte(str(brut), table)


# --- Transports par moteur ---

def _boucle_mistral(systeme, msgs, annuaire, table, executer, modele):
    cle = os.getenv("MISTRAL_API_KEY")
    if not cle:
        raise RuntimeError("MISTRAL_API_KEY manquante.")
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {cle}", "Content-Type": "application/json"}
    convo = [{"role": "system", "content": systeme}] + msgs
    outils, actions = [], []
    for _ in range(MAX_TOURS):
        rep = _post_json(url, headers, {
            "model": modele or "mistral-small-latest", "messages": convo,
            "tools": _tools_mistral(), "tool_choice": "auto",
            "temperature": 0.2, "max_tokens": 1500})
        message = rep["choices"][0]["message"]
        tcs = message.get("tool_calls")
        if not tcs:
            return message.get("content") or "", outils, actions
        convo.append(message)  # message assistant porteur des tool_calls
        for tc in tcs:
            nom = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            res = _exec_outil(nom, args, annuaire, table, executer, outils, actions)
            convo.append({"role": "tool", "tool_call_id": tc.get("id"),
                          "name": nom, "content": res})
    # Garde-fou : un dernier appel SANS outils pour forcer une réponse rédigée.
    rep = _post_json(url, headers, {"model": modele or "mistral-small-latest",
                                    "messages": convo, "temperature": 0.2, "max_tokens": 1500})
    return rep["choices"][0]["message"].get("content") or "(pas de réponse)", outils, actions


def _boucle_claude(systeme, msgs, annuaire, table, executer, modele):
    cle = os.getenv("ANTHROPIC_API_KEY")
    if not cle:
        raise RuntimeError("ANTHROPIC_API_KEY manquante.")
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": cle, "anthropic-version": "2023-06-01",
               "Content-Type": "application/json"}
    convo = [{"role": m["role"], "content": m["content"]} for m in msgs]
    outils, actions = [], []
    for _ in range(MAX_TOURS):
        rep = _post_json(url, headers, {
            "model": modele or "claude-haiku-4-5", "max_tokens": 1500,
            "system": systeme, "messages": convo, "tools": _tools_claude()})
        blocks = rep.get("content", [])
        if rep.get("stop_reason") == "tool_use":
            convo.append({"role": "assistant", "content": blocks})
            resultats = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    res = _exec_outil(b["name"], b.get("input", {}), annuaire, table, executer, outils, actions)
                    resultats.append({"type": "tool_result", "tool_use_id": b["id"], "content": res})
            convo.append({"role": "user", "content": resultats})
            continue
        texte = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return texte, outils, actions
    return "(trop d'étapes — réponse interrompue)", outils, actions


def _boucle_fake(msgs, annuaire, table, executer):
    """Hors-ligne, coût nul : routeur par mots-clés qui déclenche UN outil puis
    rédige une réponse. Sert à valider toute la chaîne (pseudonymisation, exécution
    locale, ré-identification) sans aucun appel réseau."""
    dernier = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    d = dernier.lower()
    outils, actions = [], []
    mo = re.search(r"Employé [A-Z]+", dernier)
    label = mo.group(0) if mo else None
    # Outils ACTION (mots-clés explicites, salarié ciblé requis)
    if "relance" in d and label:
        nom, args = "preparer_relance", {"employe": label}
    elif ("attestation" in d or "certificat" in d) and label:
        nom, args = "preparer_attestation", {"employe": label}
    elif ("journal" in d or "note" in d) and label:
        nom, args = "proposer_note_journal", {"employe": label, "note": "Entretien réalisé."}
    elif any(k in d for k in ("mail", "écris", "ecris", "rédige", "redige")) and label:
        nom, args = "preparer_mail", {"employe": label, "sujet": "Information", "corps": "Bonjour,\n..."}
    # Outils LECTURE
    elif any(k in d for k in ("relev", "rendu", "manqu")):
        nom, args = "releves_manquants", {}
    elif any(k in d for k in ("visite", "échéan", "echean", "cdd", "essai", "expir")):
        nom, args = "echeances_a_venir", {}
    elif any(k in d for k in ("heure", "écart", "ecart")):
        nom, args = "releves_manquants", {}
    elif any(k in d for k in ("fiche", "profil")):
        nom, args = ("profil_salarie", {"employe": label}) if label else ("lister_employes", {})
    else:
        nom, args = "lister_employes", {}
    res = _exec_outil(nom, args, annuaire, table, executer, outils, actions)
    return f"(mode fake) Résultat de l'outil « {nom} » :\n{res}", outils, actions


def run_agent(messages, employes, executer, moteur="mistral", modele=None, roster_txt=""):
    """Lance l'agent outillé. `messages` : [{role:'user'|'assistant', content}].
    `executer(nom, args, annuaire)` : callback fourni par app.py qui exécute l'outil
    en local et renvoie un texte. `roster_txt` : roster pseudonymisé (labels + poste)
    déjà construit par app.py, injecté dans le system pour cibler « le pharmacien ».
    Renvoie {"reply": <texte ré-identifié>, "outils_utilises": [...], "actions": [...]}.
    Les `actions` (boutons à confirmer) ne sont jamais passées au LLM ; leurs libellés
    et brouillons en « Employé X » sont ré-identifiés en local pour l'affichage."""
    table, inverse = construire_table(employes or [])
    annuaire = annuaire_pseudo(employes or [])
    # Pseudonymise les messages de l'utilisateur AVANT tout envoi au modèle.
    msgs = [{"role": m["role"], "content": pseudonymiser_texte(m.get("content", ""), table)}
            for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    systeme = SYSTEM_AGENT + (f"\n\nSalariés (anonymisés) :\n{roster_txt}" if roster_txt else "")

    if moteur == "claude":
        texte, outils, actions = _boucle_claude(systeme, msgs, annuaire, table, executer, modele)
    elif moteur == "fake":
        texte, outils, actions = _boucle_fake(msgs, annuaire, table, executer)
    else:
        texte, outils, actions = _boucle_mistral(systeme, msgs, annuaire, table, executer, modele)

    # Ré-identifie en local pour l'affichage (étiquettes -> prénoms réels).
    return {"reply": reidentifier(texte, inverse), "outils_utilises": outils,
            "actions": [reidentifier(a, inverse) for a in actions]}
