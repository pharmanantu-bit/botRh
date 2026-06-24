"""Analyse IA des mails RH — avec PSEUDONYMISATION (protection RGPD, option B).

Principe : AVANT tout appel au modèle, les identités des salariés (prénom, nom,
email) sont remplacées par des étiquettes neutres « Employé A », « Employé B »...
et tous les emails restants sont masqués. Le modèle ne reçoit donc jamais
l'identité réelle. APRÈS la réponse, on ré-identifie localement (les étiquettes
redeviennent les prénoms) uniquement pour l'affichage sur la page admin.

Le moteur d'analyse est interchangeable via le paramètre `moteur` :
  - "fake"    : aucune requête réseau (test de la chaîne, coût nul)
  - "mistral" : API Mistral (hébergée en UE) — MISTRAL_API_KEY
  - "claude"  : API Anthropic — ANTHROPIC_API_KEY
"""
import os
import re
import json
import urllib.request

SYSTEM_PROMPT = (
    "Tu es l'assistant RH d'une pharmacie d'officine en France. Tu reçois les e-mails "
    "RH du jour, classés par catégorie (cabinet comptable, employés, administratif). "
    "Produis une synthèse actionnable « ce qui doit être fait / mis en place ». "
    "Priorise, repère les dates limites et obligations légales (déclaration d'arrêt de "
    "travail sous 48 h, DSN, visite médicale, solde de tout compte en cas de démission...). "
    "N'invente jamais une échéance absente des mails. Les identités sont anonymisées "
    "(« Employé A/B... ») : conserve ces étiquettes telles quelles. Réponds UNIQUEMENT "
    "en JSON valide, en français, conforme au schéma demandé."
)

SCHEMA_HINT = (
    '{\n'
    '  "resume_texte": "3 à 5 phrases de synthèse",\n'
    '  "taches_a_faire": [{"titre": "", "detail": "", "source_mail": "", "priorite": "haute|moyenne|basse"}],\n'
    '  "a_mettre_en_place": [{"titre": "", "detail": "", "source_mail": ""}],\n'
    '  "echeances": [{"libelle": "", "date_limite": "", "source": ""}],\n'
    '  "alertes": ["..."]\n'
    '}'
)


def _alpha(n):
    """0->A, 1->B, ... 25->Z, 26->AA."""
    s, n = "", n + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def construire_table(employes):
    """employes: [{prenom, nom?, email}] -> (table[(regex, étiquette)], inverse{étiquette: prénom})."""
    table, inverse = [], {}
    for i, e in enumerate(employes or []):
        etq = f"Employé {_alpha(i)}"
        inverse[etq] = (e.get("prenom") or etq).strip() or etq
        motifs = []
        if e.get("email"):
            motifs.append(re.escape(e["email"].strip()))
        if e.get("prenom"):
            motifs.append(r"\b" + re.escape(e["prenom"].strip()) + r"\b")
        if e.get("nom"):
            motifs.append(r"\b" + re.escape(e["nom"].strip()) + r"\b")
        if motifs:
            table.append((re.compile("|".join(motifs), re.IGNORECASE), etq))
    return table, inverse


def pseudonymiser_texte(txt, table):
    if not txt:
        return txt
    for rgx, etq in table:
        txt = rgx.sub(etq, txt)
    # masque tout email résiduel (expéditeurs hors liste, signatures...)
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", txt)


def pseudonymiser_mails(mails, table):
    return [{
        "categorie": m.get("categorie", ""),
        "date": m.get("date", ""),
        "from": pseudonymiser_texte(m.get("from", ""), table),
        "sujet": pseudonymiser_texte(m.get("sujet", ""), table),
        "corps": pseudonymiser_texte(m.get("corps", ""), table),
        "pieces_jointes": [pseudonymiser_texte(p, table) for p in m.get("pieces_jointes", [])],
    } for m in mails]


def reidentifier(obj, inverse):
    """Remplace les étiquettes 'Employé X' par le prénom réel (affichage local uniquement)."""
    s = json.dumps(obj, ensure_ascii=False)
    for etq, prenom in inverse.items():
        if prenom and prenom != etq:
            s = s.replace(etq, prenom)
    return json.loads(s)


def construire_prompt(mails_anon):
    blocs = []
    for i, m in enumerate(mails_anon, 1):
        pj = f"\nPJ: {', '.join(m['pieces_jointes'])}" if m["pieces_jointes"] else ""
        blocs.append(f"--- Mail {i} [{m['categorie']}] ---\nDe: {m['from']}\nDate: {m['date']}\n"
                     f"Objet: {m['sujet']}\n{m['corps']}{pj}")
    corpus = "\n\n".join(blocs) if blocs else "(aucun mail aujourd'hui)"
    return (f"Voici les e-mails RH à analyser (identités anonymisées).\n\n{corpus}\n\n"
            f"Réponds en JSON conforme à ce schéma exact :\n{SCHEMA_HINT}")


def _extraire_json(texte):
    m = re.search(r"\{.*\}", texte or "", re.S)
    if not m:
        raise ValueError("Réponse du modèle sans JSON exploitable.")
    return json.loads(m.group(0))


def _post_json(url, headers, charge, timeout=60):
    data = json.dumps(charge).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# --- Moteurs interchangeables ---

def _moteur_fake(system, prompt, modele):
    return {"resume_texte": "(FAKE) analyse simulée — aucun appel réseau, coût nul.",
            "taches_a_faire": [], "a_mettre_en_place": [], "echeances": [], "alertes": []}


def _moteur_mistral(system, prompt, modele):
    cle = os.getenv("MISTRAL_API_KEY")
    if not cle:
        raise RuntimeError("MISTRAL_API_KEY manquante.")
    rep = _post_json("https://api.mistral.ai/v1/chat/completions",
                     {"Authorization": f"Bearer {cle}", "Content-Type": "application/json"},
                     {"model": modele or "mistral-small-latest",
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": prompt}],
                      "response_format": {"type": "json_object"},
                      "temperature": 0.2, "max_tokens": 2000})
    return _extraire_json(rep["choices"][0]["message"]["content"])


def _moteur_claude(system, prompt, modele):
    cle = os.getenv("ANTHROPIC_API_KEY")
    if not cle:
        raise RuntimeError("ANTHROPIC_API_KEY manquante.")
    rep = _post_json("https://api.anthropic.com/v1/messages",
                     {"x-api-key": cle, "anthropic-version": "2023-06-01",
                      "Content-Type": "application/json"},
                     {"model": modele or "claude-haiku-4-5", "max_tokens": 2000,
                      "system": system,
                      "messages": [{"role": "user", "content": prompt}]})
    return _extraire_json(rep["content"][0]["text"])


MOTEURS = {"fake": _moteur_fake, "mistral": _moteur_mistral, "claude": _moteur_claude}


def analyser(mails, employes=None, moteur="fake", modele=None):
    """Pseudonymise -> analyse (moteur choisi) -> normalise -> ré-identifie pour l'affichage."""
    table, inverse = construire_table(employes or [])
    mails_anon = pseudonymiser_mails(mails, table)
    prompt = construire_prompt(mails_anon)
    fn = MOTEURS.get(moteur, _moteur_fake)
    brut = fn(SYSTEM_PROMPT, prompt, modele)
    resume = {
        "resume_texte": brut.get("resume_texte", ""),
        "taches_a_faire": brut.get("taches_a_faire", []),
        "a_mettre_en_place": brut.get("a_mettre_en_place", []),
        "echeances": brut.get("echeances", []),
        "alertes": brut.get("alertes", []),
        "_meta": {"nb_mails": len(mails), "moteur": moteur, "modele": modele or ""},
    }
    return reidentifier(resume, inverse)
