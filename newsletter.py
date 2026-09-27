"""Module NEWSLETTER « Apothical Inside » (Blueprint Flask).

La newsletter mensuelle de la pharmacie, reprise de l'ancien RH :
  - brouillon par mois (sections titre+texte+photos, sous-titre) ;
  - l'admin jette ses notes en vrac → l'IA rédige les sections dans le ton du
    modèle historique (identités pseudonymisées avant envoi, comme l'agent RH) ;
  - aperçu PDF fidèle (newsletter_pdf) ;
  - RIEN ne part sans validation : « Valider et envoyer » déclenche le workflow
    GitHub `newsletter_envoi` — le runner récupère le PDF via /export_newsletter
    et l'envoie par mail à tous les collaborateurs actifs (SMTP bloqué sur le
    serveur gratuit, comme pour les relevés).

Données : newsletters.json + newsletter_photos/<AAAA-MM>/ (gitignorés).
"""
import os
import json
import re
import uuid
from datetime import date

from flask import (Blueprint, request, render_template, redirect, url_for,
                   session, abort, send_file)
from werkzeug.utils import secure_filename

from app import (_lire_json, _ecrire_json, BASE_DIR, MOIS_FR, API_CLE,
                 charger_employes, charger_profils, collaborateur_actif,
                 declencher_workflow)
import assistant_rh
import newsletter_pdf

bp = Blueprint("newsletter", __name__)

NEWSLETTERS_FILE = os.path.join(BASE_DIR, "newsletters.json")
PHOTOS_NL_DIR = os.path.join(BASE_DIR, "newsletter_photos")
EXT_PHOTOS_OK = {".jpg", ".jpeg", ".png"}

# Trame proposée pour un nouveau numéro (modifiable / supprimable à l'écran).
SECTIONS_DEFAUT = ["Le mot de la direction", "Évènement marquant",
                   "Vie d'équipe", "Le petit mot de la fin"]


def charger_newsletters():
    return _lire_json(NEWSLETTERS_FILE)


def sauvegarder_newsletters(d):
    _ecrire_json(NEWSLETTERS_FILE, d)


def numero_du_mois(annee, mois):
    return f"{annee:04d}-{mois:02d}"


def titre_mois(num):
    annee, mois = int(num[:4]), int(num[5:7])
    return f"{MOIS_FR[mois]} {annee}"


def _num_valide(num):
    return bool(re.fullmatch(r"\d{4}-\d{2}", num or "")) and 1 <= int(num[5:7]) <= 12


def obtenir_ou_creer(data, num):
    """Renvoie le numéro demandé, créé en brouillon avec la trame par défaut."""
    if num not in data:
        data[num] = {
            "statut": "brouillon",
            "sous_titre": "",
            "notes": "",
            "sections": [{"id": uuid.uuid4().hex[:8], "titre": t,
                          "texte": "", "photos": []} for t in SECTIONS_DEFAUT],
        }
    return data[num]


def _dossier_photos(num):
    d = os.path.join(PHOTOS_NL_DIR, num)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- Rédaction IA

PROMPT_REDACTION = """Tu rédiges la newsletter interne mensuelle « Apothical Inside » de la \
Pharmacie Apothical Nanterre Université, signée par la direction (Jonathan & David Illouz).

TON À RESPECTER (calqué sur les anciens numéros) : chaleureux, fédérateur et \
professionnel ; la direction dit « nous » et s'adresse à l'équipe par « vous » \
(« Chères collaboratrices, chers collaborateurs ») ; on remercie, on valorise \
l'implication de l'équipe, on donne de la visibilité sur les projets. Paragraphes \
courts et fluides, listes à puces (« - ») quand on énumère des faits concrets. \
Aucune donnée inventée : uniquement ce que contiennent les notes.

À partir des NOTES EN VRAC ci-dessous, rédige les sections du numéro de {mois}. \
Regroupe les notes par thème ; ne crée une section que si les notes s'y prêtent \
(2 à 6 sections). Commence par un mot de la direction si les notes le permettent, \
termine par un petit mot de conclusion.

Réponds UNIQUEMENT avec un objet JSON (aucun texte autour) :
{{"sous_titre": "devise courte du numéro",
  "sections": [{{"titre": "…", "texte": "paragraphes séparés par une ligne vide, puces avec - "}}]}}

NOTES EN VRAC :
{notes}"""


def rediger_avec_ia(notes, num):
    """Notes en vrac → sections rédigées. Identités pseudonymisées avant envoi
    au LLM (même mécanique que l'agent RH), ré-identifiées au retour.
    Renvoie (dict {sous_titre, sections}, None) ou (None, message d'erreur)."""
    employes = charger_employes()
    table, inverse = assistant_rh.construire_table(employes)
    notes_pseudo = assistant_rh.pseudonymiser_texte(notes, table)
    moteur = os.getenv("ASSISTANT_MOTEUR", "mistral")
    prompt = PROMPT_REDACTION.format(mois=titre_mois(num), notes=notes_pseudo)
    try:
        fonc = assistant_rh.MOTEURS.get(moteur, assistant_rh.MOTEURS["fake"])
        brut = fonc("Tu es le rédacteur de la newsletter interne d'une pharmacie.",
                    prompt, os.getenv("ASSISTANT_MODELE") or None)
    except Exception as e:
        return None, f"IA indisponible : {e}"
    # Le modèle répond parfois avec du texte autour : on isole le JSON.
    m = re.search(r"\{.*\}", brut, re.DOTALL)
    if not m:
        return None, "Réponse IA illisible (pas de JSON)."
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None, "Réponse IA illisible (JSON invalide)."
    obj = assistant_rh.reidentifier(obj, inverse)  # « Employé A » -> prénom réel
    sections = [{"titre": str(s.get("titre", "")).strip(),
                 "texte": str(s.get("texte", "")).strip()}
                for s in obj.get("sections", []) if isinstance(s, dict)]
    if not sections:
        return None, "L'IA n'a proposé aucune section."
    return {"sous_titre": str(obj.get("sous_titre", "")).strip(),
            "sections": sections}, None


# ----------------------------------------------------------------- Page admin

@bp.route("/admin/newsletter")
def admin_newsletter():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    auj = date.today()
    num = request.args.get("num", "")
    if not _num_valide(num):
        num = numero_du_mois(auj.year, auj.month)
    data = charger_newsletters()
    n = obtenir_ou_creer(data, num)
    sauvegarder_newsletters(data)
    anciens = sorted((k for k in data if k != num), reverse=True)
    return render_template("admin_newsletter.html", num=num, n=n,
                           titre_mois=titre_mois(num), anciens=anciens,
                           titres_anciens={k: titre_mois(k) for k in anciens},
                           msg=request.args.get("msg", ""))


@bp.route("/admin/newsletter/enregistrer", methods=["POST"])
def enregistrer():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    num = request.form.get("num", "")
    if not _num_valide(num):
        abort(400)
    data = charger_newsletters()
    n = obtenir_ou_creer(data, num)
    n["sous_titre"] = request.form.get("sous_titre", "").strip()
    n["notes"] = request.form.get("notes", "").strip()
    # Sections existantes : champs titre_<id> / texte_<id> ; absents = supprimées.
    conservees = []
    for s in n["sections"]:
        sid = s["id"]
        if f"titre_{sid}" in request.form or f"texte_{sid}" in request.form:
            s["titre"] = request.form.get(f"titre_{sid}", "").strip()
            s["texte"] = request.form.get(f"texte_{sid}", "").strip()
            conservees.append(s)
    n["sections"] = conservees
    action = request.form.get("action", "")
    msg = "enregistre"
    if action == "ajouter_section":
        n["sections"].append({"id": uuid.uuid4().hex[:8], "titre": "",
                              "texte": "", "photos": []})
    elif action.startswith("supprimer_section:"):
        sid = action.split(":", 1)[1]
        for s in list(n["sections"]):
            if s["id"] == sid:
                for f in s.get("photos", []):
                    try:
                        os.remove(os.path.join(_dossier_photos(num), f))
                    except OSError:
                        pass
                n["sections"].remove(s)
    elif action == "ia":
        if not n["notes"]:
            msg = "notes_vides"
        else:
            obj, err = rediger_avec_ia(n["notes"], num)
            if err:
                msg = "ia_err"
            else:
                # L'IA remplace le CONTENU ; les photos déjà attachées restent
                # sur les sections de même rang.
                anciennes = n["sections"]
                n["sections"] = []
                for i, s in enumerate(obj["sections"]):
                    photos = anciennes[i].get("photos", []) if i < len(anciennes) else []
                    n["sections"].append({"id": uuid.uuid4().hex[:8],
                                          "titre": s["titre"], "texte": s["texte"],
                                          "photos": photos})
                if obj["sous_titre"] and not n["sous_titre"]:
                    n["sous_titre"] = obj["sous_titre"]
                msg = "ia_ok"
    if n.get("statut") == "envoyee":
        n["statut"] = "brouillon"  # re-modifié après envoi → devra être revalidé
    sauvegarder_newsletters(data)
    return redirect(url_for("newsletter.admin_newsletter", num=num, msg=msg))


@bp.route("/admin/newsletter/photo", methods=["POST"])
def ajouter_photo():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    num = request.form.get("num", "")
    sid = request.form.get("section", "")
    if not _num_valide(num):
        abort(400)
    data = charger_newsletters()
    n = data.get(num)
    section = next((s for s in (n or {}).get("sections", []) if s["id"] == sid), None)
    fichier = request.files.get("photo")
    if not section or not fichier or not fichier.filename:
        return redirect(url_for("newsletter.admin_newsletter", num=num, msg="photo_err"))
    ext = os.path.splitext(secure_filename(fichier.filename))[1].lower()
    if ext not in EXT_PHOTOS_OK:
        return redirect(url_for("newsletter.admin_newsletter", num=num, msg="photo_type"))
    nom = f"{uuid.uuid4().hex[:10]}{ext}"
    fichier.save(os.path.join(_dossier_photos(num), nom))
    section.setdefault("photos", []).append(nom)
    sauvegarder_newsletters(data)
    return redirect(url_for("newsletter.admin_newsletter", num=num, msg="photo_ok"))


@bp.route("/admin/newsletter/photo/supprimer", methods=["POST"])
def supprimer_photo():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    num = request.form.get("num", "")
    sid = request.form.get("section", "")
    nom = os.path.basename(request.form.get("fichier", ""))
    if not _num_valide(num):
        abort(400)
    data = charger_newsletters()
    n = data.get(num)
    section = next((s for s in (n or {}).get("sections", []) if s["id"] == sid), None)
    if section and nom in section.get("photos", []):
        section["photos"].remove(nom)
        try:
            os.remove(os.path.join(_dossier_photos(num), nom))
        except OSError:
            pass
        sauvegarder_newsletters(data)
    return redirect(url_for("newsletter.admin_newsletter", num=num, msg="photo_supprimee"))


@bp.route("/admin/newsletter/photo/<num>/<nom>")
def voir_photo(num, nom):
    if not session.get("admin"):
        return redirect(url_for("admin"))
    if not _num_valide(num):
        abort(404)
    chemin = os.path.join(_dossier_photos(num), os.path.basename(nom))
    if not os.path.isfile(chemin):
        abort(404)
    return send_file(chemin)


@bp.route("/admin/newsletter/pdf")
def apercu_pdf():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    num = request.args.get("num", "")
    if not _num_valide(num):
        abort(400)
    n = charger_newsletters().get(num)
    if not n:
        abort(404)
    buf = newsletter_pdf.generer_pdf_newsletter(
        {**n, "titre_mois": titre_mois(num)}, _dossier_photos(num))
    return send_file(buf, mimetype="application/pdf",
                     download_name=f"Apothical_Inside_{num}.pdf")


@bp.route("/admin/newsletter/valider", methods=["POST"])
def valider():
    """Validation par l'admin : marque le numéro validé et délègue l'envoi au
    runner GitHub (le serveur gratuit ne peut pas envoyer d'e-mail)."""
    if not session.get("admin"):
        return redirect(url_for("admin"))
    num = request.form.get("num", "")
    if not _num_valide(num):
        abort(400)
    data = charger_newsletters()
    n = data.get(num)
    if not n or not any((s.get("texte") or "").strip() for s in n.get("sections", [])):
        return redirect(url_for("newsletter.admin_newsletter", num=num, msg="vide"))
    n["statut"] = "validee"
    n["validee_le"] = date.today().strftime("%d/%m/%Y")
    sauvegarder_newsletters(data)
    try:
        declencher_workflow("newsletter_envoi", {"num": num})
    except Exception:
        return redirect(url_for("newsletter.admin_newsletter", num=num, msg="dispatch_err"))
    n["statut"] = "envoyee"
    sauvegarder_newsletters(data)
    return redirect(url_for("newsletter.admin_newsletter", num=num, msg="envoyee"))


# ------------------------------------------------------- Export pour le runner

@bp.route("/export_newsletter")
def export_newsletter():
    """PDF d'un numéro VALIDÉ, pour le runner GitHub (clé API requise)."""
    if request.args.get("cle") != API_CLE:
        abort(403)
    num = request.args.get("num", "")
    if not _num_valide(num):
        abort(400)
    n = charger_newsletters().get(num)
    if not n or n.get("statut") not in ("validee", "envoyee"):
        abort(404)
    buf = newsletter_pdf.generer_pdf_newsletter(
        {**n, "titre_mois": titre_mois(num)}, _dossier_photos(num))
    return send_file(buf, mimetype="application/pdf",
                     download_name=f"Apothical_Inside_{num}.pdf")
