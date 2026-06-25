"""Module RECRUTEMENT (Blueprint Flask) — rangé à part pour ne pas alourdir app.py.

Gère les candidats (pipeline), le dépôt de CV/documents, les notes d'entretien et
l'analyse IA des CV. Réutilise les helpers de app.py (stockage JSON, détection de
type), l'OCR (extraction_pj) et l'analyse (recrutement_ia). Données 100 % séparées
des salariés (candidats.json), aucun impact sur Équipe & RH.
"""
import os
import json
import uuid
from datetime import datetime

from flask import (Blueprint, request, render_template, redirect, url_for,
                   session, abort, send_file, current_app)
from werkzeug.utils import secure_filename

from app import (_lire_json, _ecrire_json, BASE_DIR, EXT_DOCS_OK, humaniser_taille,
                 deviner_type_doc, charger_employes, sauvegarder_employes,
                 charger_profils, sauvegarder_profils, DOCS_DIR,
                 charger_docs_index, sauvegarder_docs_index, API_CLE)
import extraction_pj
import recrutement_ia

bp = Blueprint("recrutement", __name__)

CANDIDATS_FILE = os.path.join(BASE_DIR, "candidats.json")
CANDIDATS_DOCS_DIR = os.path.join(BASE_DIR, "candidats_docs")
CANDIDATS_DOCS_INDEX = os.path.join(CANDIDATS_DOCS_DIR, "index.json")

STATUTS_RECRUTEMENT = ["Reçu", "À contacter", "Entretien", "Retenu", "Refusé", "Embauché"]
TYPES_DOC_CANDIDAT = ["CV", "Lettre de motivation", "Diplôme", "Pièce d'identité", "Autre"]
TYPES_NOTE_ENTRETIEN = ["Entretien téléphonique", "Entretien physique", "Test/essai",
                        "Échange e-mail", "Autre"]


def charger_candidats():
    return _lire_json(CANDIDATS_FILE)

def sauvegarder_candidats(c):
    _ecrire_json(CANDIDATS_FILE, c)

def charger_candidats_docs_index():
    return _lire_json(CANDIDATS_DOCS_INDEX)

def sauvegarder_candidats_docs_index(idx):
    os.makedirs(CANDIDATS_DOCS_DIR, exist_ok=True)
    _ecrire_json(CANDIDATS_DOCS_INDEX, idx)

def candidat_docs_de(cid):
    return charger_candidats_docs_index().get(cid, [])

def _admin():
    return session.get("admin")

def _deviner_type_candidat(filename, texte):
    """Type d'un document de recrutement (CV par défaut)."""
    base = ((filename or "") + " " + (texte or "")).lower()
    if "lettre de motivation" in base or "motivation" in base:
        return "Lettre de motivation"
    if "curriculum" in base or "vitae" in base or " cv" in f" {base}":
        return "CV"
    t = deviner_type_doc(filename, texte)
    if t in ("Diplôme", "Pièce d'identité"):
        return t
    return "CV"


@bp.route("/admin/recrutement")
def liste():
    if not _admin():
        return redirect(url_for("admin"))
    candidats = charger_candidats()
    idx = charger_candidats_docs_index()
    parstatut = {s: [] for s in STATUTS_RECRUTEMENT}
    for cid, c in candidats.items():
        info = {**c, "id": cid, "nb_docs": len(idx.get(cid, [])),
                "a_analyse": bool(c.get("analyse_ia"))}
        parstatut.setdefault(c.get("statut", "Reçu"), []).append(info)
    for s in parstatut:
        parstatut[s].sort(key=lambda x: x.get("date_ajout", ""), reverse=True)
    return render_template("admin_recrutement.html", parstatut=parstatut,
                           statuts=STATUTS_RECRUTEMENT, total=len(candidats))


@bp.route("/admin/recrutement/ajouter", methods=["POST"])
def ajouter():
    if not _admin():
        return redirect(url_for("admin"))
    candidats = charger_candidats()
    cid = uuid.uuid4().hex[:10]
    candidats[cid] = {
        "nom": request.form.get("nom", "").strip(),
        "prenom": request.form.get("prenom", "").strip(),
        "email": request.form.get("email", "").strip(),
        "telephone": request.form.get("telephone", "").strip(),
        "poste_vise": request.form.get("poste_vise", "").strip(),
        "source": request.form.get("source", "").strip(),
        "statut": "Reçu",
        "date_ajout": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "notes_libres": "",
        "journal": [],
        "cv_texte": "",
        "analyse_ia": None,
    }
    sauvegarder_candidats(candidats)
    return redirect(url_for(".candidat", id=cid))


@bp.route("/admin/recrutement/candidat")
def candidat():
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.args.get("id", "")
    c = charger_candidats().get(cid)
    if not c:
        abort(404)
    journal = sorted(c.get("journal", []), key=lambda e: e.get("date", ""), reverse=True)
    # Lien Gmail pré-rempli vers l'expert-comptable (pour le dossier du nouvel embauché).
    import urllib.parse
    comptable = os.getenv("COMPTA_EMAILS", "").split(",")[0].strip()
    mail_comptable_url = ""
    if comptable:
        su = f"Dossier nouvel embauché — {c.get('prenom', '')} {c.get('nom', '')}".strip()
        body = (f"Bonjour,\n\nVeuillez trouver le dossier de {c.get('prenom', '')} {c.get('nom', '')} "
                f"({c.get('poste_vise') or 'poste'}), recruté(e) dans notre officine.\n\n"
                "Documents en pièce jointe (ZIP à joindre depuis le téléchargement).\n\nCordialement")
        mail_comptable_url = ("https://mail.google.com/mail/?view=cm&fs=1&to="
                              + urllib.parse.quote(comptable) + "&su=" + urllib.parse.quote(su)
                              + "&body=" + urllib.parse.quote(body))
    return render_template("admin_candidat.html", c=c, cid=cid,
                           docs=candidat_docs_de(cid), statuts=STATUTS_RECRUTEMENT,
                           types_doc=TYPES_DOC_CANDIDAT, types_note=TYPES_NOTE_ENTRETIEN,
                           journal=journal, msg=request.args.get("msg", ""),
                           mail_comptable_url=mail_comptable_url)


@bp.route("/admin/recrutement/fiche", methods=["POST"])
def fiche():
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.form.get("id", "")
    candidats = charger_candidats()
    c = candidats.get(cid)
    if not c:
        abort(404)
    for champ in ("nom", "prenom", "email", "telephone", "poste_vise", "source", "notes_libres"):
        c[champ] = request.form.get(champ, c.get(champ, "")).strip()
    candidats[cid] = c
    sauvegarder_candidats(candidats)
    return redirect(url_for(".candidat", id=cid))


@bp.route("/admin/recrutement/statut", methods=["POST"])
def statut():
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.form.get("id", "")
    nouveau = request.form.get("statut", "")
    candidats = charger_candidats()
    if cid in candidats and nouveau in STATUTS_RECRUTEMENT:
        candidats[cid]["statut"] = nouveau
        sauvegarder_candidats(candidats)
    # depuis la liste (kanban) on revient à la liste ; depuis la fiche, à la fiche
    if request.form.get("origine") == "liste":
        return redirect(url_for(".liste"))
    return redirect(url_for(".candidat", id=cid))


@bp.route("/admin/recrutement/document", methods=["POST"])
def document():
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.form.get("id", "")
    candidats = charger_candidats()
    c = candidats.get(cid)
    if not c:
        abort(404)
    f = request.files.get("fichier")
    if not f or not f.filename:
        return redirect(url_for(".candidat", id=cid, msg="doc_vide"))
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in EXT_DOCS_OK:
        return redirect(url_for(".candidat", id=cid, msg="doc_type"))
    os.makedirs(CANDIDATS_DOCS_DIR, exist_ok=True)
    doc_id = uuid.uuid4().hex[:12]
    stored = f"{doc_id}_{secure_filename(f.filename)}"
    chemin = os.path.join(CANDIDATS_DOCS_DIR, stored)
    f.save(chemin)
    contenu = b""
    try:
        with open(chemin, "rb") as fp:
            contenu = fp.read()
    except Exception:
        pass
    texte = extraction_pj.extraire_texte(f.filename, contenu)
    type_choisi = request.form.get("type", "")
    type_final = _deviner_type_candidat(f.filename, texte) if type_choisi in ("", "__auto__") else type_choisi
    idx = charger_candidats_docs_index()
    idx.setdefault(cid, []).append({
        "id": doc_id, "fichier": stored, "nom_original": f.filename, "type": type_final,
        "libelle": request.form.get("libelle", "").strip() or f.filename,
        "taille": humaniser_taille(os.path.getsize(chemin)),
        "date_ajout": datetime.now().strftime("%d/%m/%Y %H:%M"),
    })
    sauvegarder_candidats_docs_index(idx)
    # Si c'est un CV : mémorise le texte (pour l'analyse) + pré-remplit le contact si vide.
    if type_final == "CV" and texte.strip():
        c["cv_texte"] = texte
        contact = extraction_pj.extraire_contact(texte)
        if contact.get("email") and not c.get("email"):
            c["email"] = contact["email"]
        if contact.get("telephone") and not c.get("telephone"):
            c["telephone"] = contact["telephone"]
        candidats[cid] = c
        sauvegarder_candidats(candidats)
    return redirect(url_for(".candidat", id=cid, msg="doc_ok"))


@bp.route("/admin/recrutement/document/<doc_id>")
def document_voir(doc_id):
    if not _admin():
        return redirect(url_for("admin"))
    voir = request.args.get("voir") == "1"
    for docs in charger_candidats_docs_index().values():
        for d in docs:
            if d["id"] == doc_id:
                chemin = os.path.join(CANDIDATS_DOCS_DIR, d["fichier"])
                if os.path.exists(chemin):
                    return send_file(chemin, as_attachment=not voir, download_name=d["nom_original"])
    abort(404)


@bp.route("/admin/recrutement/document/supprimer", methods=["POST"])
def document_supprimer():
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.form.get("id", "")
    doc_id = request.form.get("doc_id", "")
    idx = charger_candidats_docs_index()
    docs = idx.get(cid, [])
    cible = next((d for d in docs if d["id"] == doc_id), None)
    if cible:
        try:
            p = os.path.join(CANDIDATS_DOCS_DIR, cible["fichier"])
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            current_app.logger.exception("Échec suppression document candidat")
        idx[cid] = [d for d in docs if d["id"] != doc_id]
        sauvegarder_candidats_docs_index(idx)
    return redirect(url_for(".candidat", id=cid))


@bp.route("/admin/recrutement/journal", methods=["POST"])
def journal_ajout():
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.form.get("id", "")
    candidats = charger_candidats()
    c = candidats.get(cid)
    if not c:
        abort(404)
    note = request.form.get("note", "").strip()
    if note:
        journal = c.get("journal", [])
        journal.append({
            "id": uuid.uuid4().hex[:8],
            "date": request.form.get("date", "").strip() or datetime.now().strftime("%d/%m/%Y"),
            "type": request.form.get("type", "Autre"),
            "note": note,
        })
        c["journal"] = journal
        candidats[cid] = c
        sauvegarder_candidats(candidats)
    return redirect(url_for(".candidat", id=cid))


@bp.route("/admin/recrutement/journal/supprimer", methods=["POST"])
def journal_suppr():
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.form.get("id", "")
    ev_id = request.form.get("ev_id", "")
    candidats = charger_candidats()
    c = candidats.get(cid)
    if c:
        c["journal"] = [e for e in c.get("journal", []) if e.get("id") != ev_id]
        candidats[cid] = c
        sauvegarder_candidats(candidats)
    return redirect(url_for(".candidat", id=cid))


@bp.route("/admin/recrutement/analyser", methods=["POST"])
def analyser():
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.form.get("id", "")
    candidats = charger_candidats()
    c = candidats.get(cid)
    if not c:
        abort(404)
    texte = (c.get("cv_texte") or "").strip()
    if not texte:
        return redirect(url_for(".candidat", id=cid, msg="pas_de_cv"))
    moteur = os.getenv("ASSISTANT_MOTEUR", "mistral")
    try:
        analyse = recrutement_ia.analyser_cv(texte, c.get("poste_vise", ""), moteur=moteur,
                                             modele=os.getenv("ASSISTANT_MODELE") or None)
        analyse["genere_le"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        c["analyse_ia"] = analyse
        candidats[cid] = c
        sauvegarder_candidats(candidats)
        return redirect(url_for(".candidat", id=cid, msg="analyse_ok"))
    except Exception:
        current_app.logger.exception("Analyse CV échouée")
        return redirect(url_for(".candidat", id=cid, msg="analyse_err"))


# --- Agent conversationnel recrutement : outils + route chat ---
# Les outils accèdent aux candidats et construisent les actions (mailto). Surfacés à
# agent_recrutement.run_agent_recrutement (sans pseudonymisation : données candidats).

def _resoudre_candidat(nom):
    """Trouve un candidat par nom/prénom (insensible casse, partiel). Renvoie (cid, c)
    ou (None, raison) : 'preciser' / 'introuvable' / 'ambigu:<liste>'."""
    q = (nom or "").strip().lower()
    if not q:
        return None, "preciser"
    matches = []
    for cid, c in charger_candidats().items():
        full = f"{c.get('prenom', '')} {c.get('nom', '')}".lower()
        if q in full or q == c.get("prenom", "").lower() or q == c.get("nom", "").lower():
            matches.append((cid, c))
    if not matches:
        return None, "introuvable"
    if len(matches) > 1:
        exact = [(i, c) for i, c in matches
                 if q in (c.get("prenom", "").lower(), c.get("nom", "").lower())]
        if len(exact) == 1:
            return exact[0]
        return None, "ambigu:" + ", ".join(f"{c.get('prenom')} {c.get('nom')}" for _, c in matches)
    return matches[0]

def _msg_resolution(raison):
    if raison == "preciser":
        return "Précise le nom du candidat."
    if isinstance(raison, str) and raison.startswith("ambigu:"):
        return "Plusieurs candidats correspondent : " + raison[7:] + ". Précise lequel."
    return "Candidat introuvable."

def _o_lister(args):
    statut = (args.get("statut") or "").strip().lower()
    poste = (args.get("poste") or "").strip().lower()
    lignes = []
    for c in charger_candidats().values():
        if statut and c.get("statut", "").lower() != statut:
            continue
        if poste and poste not in c.get("poste_vise", "").lower():
            continue
        a = c.get("analyse_ia") or {}
        sc = f" — score IA {a['score']}/100" if a.get("score") is not None else ""
        lignes.append(f"- {c.get('prenom')} {c.get('nom')} — {c.get('poste_vise') or '—'} — {c.get('statut')}{sc}")
    return f"{len(lignes)} candidat(s) :\n" + "\n".join(lignes) if lignes else "Aucun candidat correspondant."

def _o_fiche(args):
    cid, c = _resoudre_candidat(args.get("candidat"))
    if not cid:
        return _msg_resolution(c)
    a = c.get("analyse_ia") or {}
    p = [f"{c.get('prenom')} {c.get('nom')} — {c.get('poste_vise') or 'poste non précisé'} — statut « {c.get('statut')} »"]
    if c.get("email") or c.get("telephone"):
        p.append(f"Contact : {c.get('email', '')} {c.get('telephone', '')}".strip())
    if c.get("source"):
        p.append(f"Source : {c.get('source')}")
    if a:
        p.append(f"Analyse IA : {a.get('score')}/100 — {a.get('adequation', '')}")
        if a.get("resume"):
            p.append("Résumé : " + a["resume"])
    if c.get("notes_libres"):
        p.append("Notes : " + c["notes_libres"])
    if c.get("cv_texte"):
        p.append("Extrait CV : " + c["cv_texte"][:1500])
    return "\n".join(p)

def _o_rechercher(args):
    mot = (args.get("motcle") or "").strip().lower()
    if not mot:
        return "Précise un mot-clé."
    res = [f"- {c.get('prenom')} {c.get('nom')} ({c.get('poste_vise') or '—'}, {c.get('statut')})"
           for c in charger_candidats().values()
           if mot in (c.get("cv_texte", "") + " " + c.get("notes_libres", "")).lower()]
    return (f"Candidats mentionnant « {args.get('motcle')} » :\n" + "\n".join(res)
            if res else f"Aucun candidat ne mentionne « {args.get('motcle')} ».")

def _o_classer(args):
    poste = (args.get("poste") or "").strip().lower()
    avec, sans = [], []
    for c in charger_candidats().values():
        if poste and poste not in c.get("poste_vise", "").lower():
            continue
        a = c.get("analyse_ia") or {}
        (avec if a.get("score") is not None else sans).append((a.get("score", 0), c))
    avec.sort(key=lambda x: x[0], reverse=True)
    if not avec and not sans:
        return "Aucun candidat à classer."
    out = []
    if avec:
        out.append("Classement par score d'analyse IA :")
        out += [f"{i}. {c.get('prenom')} {c.get('nom')} — {sc}/100 ({c.get('poste_vise') or '—'})"
                for i, (sc, c) in enumerate(avec, 1)]
    if sans:
        out.append("Non encore analysés (bouton « Analyser le CV » sur leur fiche) : "
                   + ", ".join(f"{c.get('prenom')} {c.get('nom')}" for _, c in sans))
    return "\n".join(out)

def _mail_action(args, refus=False):
    cid, c = _resoudre_candidat(args.get("candidat"))
    if not cid:
        return {"resultat": _msg_resolution(c), "action": None}
    if not c.get("email"):
        return {"resultat": f"{c.get('prenom')} {c.get('nom')} n'a pas d'e-mail enregistré (ajoute-le sur sa fiche).",
                "action": None}
    poste = c.get("poste_vise") or "le poste"
    if refus:
        sujet = f"Votre candidature — {poste}"
        corps = (f"Bonjour {c.get('prenom')},\n\nNous vous remercions pour l'intérêt porté à notre officine et "
                 f"pour votre candidature au poste de {poste}.\n\nAprès étude attentive, nous ne donnerons pas "
                 "suite cette fois-ci. Nous conservons votre profil et reviendrons vers vous si une opportunité "
                 "y correspond.\n\nNous vous souhaitons une pleine réussite dans vos démarches.\n\nCordialement,\nLa pharmacie")
        label = f"✉️ Réponse de refus à {c.get('prenom')} {c.get('nom')}"
        quoi = "refus"
    else:
        details = (args.get("details") or "").strip()
        sujet = f"Entretien de recrutement — {poste}"
        corps = (f"Bonjour {c.get('prenom')},\n\nNous avons étudié votre candidature pour {poste} et souhaitons "
                 "vous rencontrer lors d'un entretien.\n\n"
                 + (f"Proposition : {details}\n\n" if details else "Pourriez-vous nous indiquer vos disponibilités ?\n\n")
                 + "Dans l'attente de votre retour,\n\nCordialement,\nLa pharmacie")
        label = f"✉️ Convoquer {c.get('prenom')} {c.get('nom')}"
        quoi = "convocation"
    return {"resultat": f"Brouillon de {quoi} préparé pour {c.get('prenom')} {c.get('nom')} (à relire et envoyer).",
            "action": {"type": "mailto", "label": label, "to": c["email"], "subject": sujet, "body": corps}}

_OUTILS_RECRUTEMENT = {
    "lister_candidats": _o_lister,
    "fiche_candidat": _o_fiche,
    "rechercher_candidat": _o_rechercher,
    "classer_candidats": _o_classer,
    "preparer_mail_convocation": lambda a: _mail_action(a, refus=False),
    "preparer_mail_refus": lambda a: _mail_action(a, refus=True),
}

def executer_outil_recrutement(nom, args):
    fn = _OUTILS_RECRUTEMENT.get(nom)
    return fn(args or {}) if fn else f"(outil inconnu : {nom})"

def roster_recrutement():
    return "\n".join(
        f"- {c.get('prenom')} {c.get('nom')} — {c.get('poste_vise') or '—'} — {c.get('statut')}"
        for c in list(charger_candidats().values())[:40])


@bp.route("/admin/recrutement/chat", methods=["POST"])
def chat():
    """Agent conversationnel recrutement (function calling). IA en temps réel →
    local / PA payant. Données candidats envoyées au modèle (transparence UI)."""
    if not _admin():
        return current_app.response_class(json.dumps({"error": "non autorisé"}),
                                          status=403, mimetype="application/json")
    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return current_app.response_class(json.dumps({"error": "message vide"}),
                                          status=400, mimetype="application/json")
    messages = messages[-20:]
    try:
        import agent_recrutement
        res = agent_recrutement.run_agent_recrutement(
            messages, executer_outil_recrutement,
            moteur=os.getenv("ASSISTANT_MOTEUR", "mistral"),
            modele=os.getenv("ASSISTANT_MODELE") or None,
            roster_txt=roster_recrutement())
        return current_app.response_class(json.dumps(res, ensure_ascii=False), mimetype="application/json")
    except Exception as e:
        current_app.logger.exception("Agent recrutement indisponible")
        return current_app.response_class(
            json.dumps({"error": f"Service IA indisponible ({type(e).__name__}). "
                                 "En ligne, nécessite un PythonAnywhere payant."}, ensure_ascii=False),
            status=502, mimetype="application/json")


@bp.route("/admin/recrutement/supprimer", methods=["POST"])
def supprimer():
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.form.get("id", "")
    candidats = charger_candidats()
    if cid in candidats:
        idx = charger_candidats_docs_index()
        for d in idx.get(cid, []):
            try:
                p = os.path.join(CANDIDATS_DOCS_DIR, d["fichier"])
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                current_app.logger.exception("Échec suppression fichier candidat")
        idx.pop(cid, None)
        sauvegarder_candidats_docs_index(idx)
        candidats.pop(cid, None)
        sauvegarder_candidats(candidats)
    return redirect(url_for(".liste"))


@bp.route("/candidat_push", methods=["POST"])
def candidat_push():
    """Reçoit une candidature (CV) du runner GitHub (label Gmail « Recrutement ») et
    crée un candidat. Protégé par la clé API. Anti-doublon par e-mail."""
    if request.args.get("cle", "") != API_CLE:
        abort(403)
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip()
    candidats = charger_candidats()
    if email and any((c.get("email") or "").lower() == email.lower() for c in candidats.values()):
        return current_app.response_class(json.dumps({"status": "doublon"}), mimetype="application/json")
    cid = uuid.uuid4().hex[:10]
    sujet = (data.get("sujet") or "").strip()
    candidats[cid] = {
        "prenom": (data.get("prenom") or "").strip(), "nom": (data.get("nom") or "").strip(),
        "email": email, "telephone": (data.get("telephone") or "").strip(),
        "poste_vise": "", "source": "Candidature e-mail", "statut": "Reçu",
        "date_ajout": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "notes_libres": (f"Objet du mail : {sujet}" if sujet else ""),
        "journal": [], "cv_texte": (data.get("cv_texte") or "")[:20000], "analyse_ia": None,
    }
    sauvegarder_candidats(candidats)
    # CV en pièce jointe
    import base64
    nom_orig = data.get("filename", "cv")
    if os.path.splitext(nom_orig)[1].lower() in EXT_DOCS_OK:
        try:
            contenu = base64.b64decode(data.get("content_b64", ""))
        except Exception:
            contenu = b""
        if contenu:
            os.makedirs(CANDIDATS_DOCS_DIR, exist_ok=True)
            doc_id = uuid.uuid4().hex[:12]
            stored = f"{doc_id}_{secure_filename(nom_orig)}"
            with open(os.path.join(CANDIDATS_DOCS_DIR, stored), "wb") as f:
                f.write(contenu)
            idx = charger_candidats_docs_index()
            idx.setdefault(cid, []).append({
                "id": doc_id, "fichier": stored, "nom_original": nom_orig, "type": "CV",
                "libelle": nom_orig, "taille": humaniser_taille(len(contenu)),
                "date_ajout": datetime.now().strftime("%d/%m/%Y %H:%M")})
            sauvegarder_candidats_docs_index(idx)
    return current_app.response_class(json.dumps({"status": "ajoute", "id": cid}), mimetype="application/json")


@bp.route("/admin/recrutement/embaucher", methods=["POST"])
def embaucher():
    """Crée la fiche salarié (Équipe & RH) pré-remplie depuis le candidat + copie ses
    documents. Anti-doublon. Marque le candidat « Embauché »."""
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.form.get("id", "")
    candidats = charger_candidats()
    c = candidats.get(cid)
    if not c:
        abort(404)
    email = (c.get("email") or "").strip()
    if not email:
        return redirect(url_for(".candidat", id=cid, msg="embauche_email"))
    employes = charger_employes()
    if any((e.get("email") or "").lower() == email.lower() for e in employes):
        return redirect(url_for(".candidat", id=cid, msg="deja_salarie"))
    # 1) Salarié (employees_live.csv)
    employes.append({"nom": c.get("nom", ""), "prenom": c.get("prenom", ""), "email": email})
    sauvegarder_employes(employes)
    # 2) Profil pré-rempli
    profils = charger_profils()
    prof = profils.get(email, {})
    prof.setdefault("statut", "actif")
    if c.get("poste_vise"):
        prof.setdefault("poste", c["poste_vise"])
    if c.get("telephone"):
        prof.setdefault("telephone", c["telephone"])
    prof.setdefault("date_entree", datetime.now().strftime("%d/%m/%Y"))
    if not prof.get("notes"):
        prof["notes"] = "Ancien candidat (issu du recrutement)."
    profils[email] = prof
    sauvegarder_profils(profils)
    # 3) Copie des documents candidat -> salarié
    import shutil
    idx = charger_docs_index()
    for d in candidat_docs_de(cid):
        src = os.path.join(CANDIDATS_DOCS_DIR, d["fichier"])
        if not os.path.exists(src):
            continue
        new_id = uuid.uuid4().hex[:12]
        nom_orig = d.get("nom_original", d["fichier"])
        stored = f"{new_id}_{secure_filename(nom_orig)}"
        os.makedirs(DOCS_DIR, exist_ok=True)
        try:
            shutil.copyfile(src, os.path.join(DOCS_DIR, stored))
        except OSError:
            continue
        idx.setdefault(email, []).append({
            "id": new_id, "fichier": stored, "nom_original": nom_orig,
            "type": d.get("type", "Autre / divers"), "libelle": d.get("libelle", nom_orig),
            "taille": d.get("taille", ""), "date_ajout": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "source": "recrutement",
        })
    sauvegarder_docs_index(idx)
    # 4) Marque le candidat
    c["statut"] = "Embauché"
    c["embauche_email"] = email
    candidats[cid] = c
    sauvegarder_candidats(candidats)
    return redirect(url_for("admin_employe", email=email, msg="embauche"))


@bp.route("/admin/recrutement/dossier-zip")
def dossier_zip():
    """ZIP des documents du candidat + un récap — pour l'expert-comptable."""
    if not _admin():
        return redirect(url_for("admin"))
    cid = request.args.get("id", "")
    c = charger_candidats().get(cid)
    if not c:
        abort(404)
    import io
    import zipfile
    recap = "\r\n".join([
        f"DOSSIER CANDIDAT — {c.get('prenom')} {c.get('nom')}",
        f"Poste visé : {c.get('poste_vise') or '—'}",
        f"E-mail : {c.get('email') or '—'}",
        f"Téléphone : {c.get('telephone') or '—'}", ""])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dossier.txt", recap)
        for d in candidat_docs_de(cid):
            chemin = os.path.join(CANDIDATS_DOCS_DIR, d["fichier"])
            if os.path.exists(chemin):
                zf.write(chemin, d.get("nom_original", d["fichier"]))
    buf.seek(0)
    nom = secure_filename(f"dossier_{c.get('prenom', '')}_{c.get('nom', '')}") or "dossier"
    return send_file(buf, as_attachment=True, download_name=f"{nom}.zip", mimetype="application/zip")
