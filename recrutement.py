"""Module RECRUTEMENT (Blueprint Flask) — rangé à part pour ne pas alourdir app.py.

Gère les candidats (pipeline), le dépôt de CV/documents, les notes d'entretien et
l'analyse IA des CV. Réutilise les helpers de app.py (stockage JSON, détection de
type), l'OCR (extraction_pj) et l'analyse (recrutement_ia). Données 100 % séparées
des salariés (candidats.json), aucun impact sur Équipe & RH.
"""
import os
import uuid
from datetime import datetime

from flask import (Blueprint, request, render_template, redirect, url_for,
                   session, abort, send_file, current_app)
from werkzeug.utils import secure_filename

from app import (_lire_json, _ecrire_json, BASE_DIR, EXT_DOCS_OK, humaniser_taille,
                 deviner_type_doc)
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
    return render_template("admin_candidat.html", c=c, cid=cid,
                           docs=candidat_docs_de(cid), statuts=STATUTS_RECRUTEMENT,
                           types_doc=TYPES_DOC_CANDIDAT, types_note=TYPES_NOTE_ENTRETIEN,
                           journal=journal, msg=request.args.get("msg", ""))


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
