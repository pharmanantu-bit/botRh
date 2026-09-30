"""Envoi du dossier paie mensuel à l'expert-comptable — exécuté par GitHub
Actions (repository_dispatch `envoi_comptable`, déclenché par le bouton
« Envoyer au comptable » de l'admin, une fois tous les relevés validés).

Le serveur PythonAnywhere gratuit ne peut pas envoyer d'e-mail : ce runner
récupère le résumé paie (/export_resume_paie : ventilation des heures sup
25 % / 50 % par semaine civile + heures de sujétion des gardes dimanche/férié,
régime CCN pharmacie d'officine) et le récap Excel (/export_recap), puis
envoie le tout à l'expert-comptable (destinataires dans le payload), copie à
l'admin. Corps HTML (tables par collaborateur, une ligne par semaine) avec
repli texte. `construire_mail_comptable` est pur pour être testable sans SMTP
(pattern conges_sender)."""
import os
import html as html_mod
import json
import smtplib
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from signature_mail import SIGNATURE

BASE_URL = "https://pharmacie92000.pythonanywhere.com"
CLE = os.environ.get("API_CLE") or "botRh-trigger-2026"
ADMIN_EMAIL = "pharmanantu@gmail.com"

MOIS_FR = {1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril", 5: "Mai", 6: "Juin",
           7: "Juillet", 8: "Août", 9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"}


def _fmt_h(v):
    """7.75 -> « 7h45 » ; 7.0 -> « 7h » (plus lisible pour le comptable)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    h = int(v)
    m = round((v - h) * 60)
    return f"{h}h{m:02d}" if m else f"{h}h"


# Styles inline (Gmail ignore <style>) partagés par toutes les cellules.
_TD = "border:1px solid #444;padding:6px 10px;text-align:center;font-size:14px;"
_TH = _TD + "background:#1c4e2e;color:#fff;font-weight:bold;"
_TH_HP = _TD + "background:#2e6da4;color:#fff;font-weight:bold;"
_TH_SUJ = _TD + "background:#b56a1e;color:#fff;font-weight:bold;"
_TD_HP = _TD + "background:#e9f1f8;font-weight:bold;"
_TD_SUJ = _TD + "background:#fdf1e3;"
_TD_TOT = _TD + "background:#eaf3ec;font-weight:bold;"
_ZERO = '<span style="color:#999;font-weight:normal">—</span>'


def _h(v):
    """Cellule heures : « 7h45 » ou tiret grisé si zéro."""
    return _fmt_h(v) if v else _ZERO


def _conges_txt(cg):
    """{"plages": [{debut, fin, jours}], "total": n} -> « du 13/07 au 25/07
    (11 j) · le 28/07 (1 j) — total 12 j ouvrables »."""
    plages = [(f"du {p['debut']} au {p['fin']} ({p['jours']} j)"
               if p["debut"] != p["fin"] else f"le {p['debut']} (1 j)")
              for p in cg.get("plages") or []]
    txt = " · ".join(plages)
    if len(plages) > 1:
        txt += f" — total {cg['total']} j ouvrables"
    return txt


def _table(entetes, lignes_html):
    tr = "".join(f'<th style="{st}">{txt}</th>' for txt, st in entetes)
    return ('<table style="border-collapse:collapse;width:100%;margin:10px 0 4px">'
            f"<tr>{tr}</tr>{''.join(lignes_html)}</table>")


_TD_COMM = _TD + "text-align:left;font-size:12.5px;"


def _cell_commentaires(it):
    """Cellule « Commentaires » d'un collaborateur : commentaire du salarié +
    marqueurs pharmacie (saisi/corrigé/ajusté) + jours de garde (sujétion) +
    congés payés pris sur la période."""
    esc = html_mod.escape
    parts = []
    if it.get("commentaire"):
        parts.append("💬 " + esc(str(it["commentaire"])))
    flags = []
    if it.get("saisi_par_admin"):
        flags.append("saisi par la pharmacie")
    if it.get("corrige"):
        flags.append("corrigé par la pharmacie")
    if it.get("ajuste"):
        flags.append("chiffres ajustés par la pharmacie")
    if flags:
        parts.append('<span style="color:#8a6d3b">' + esc(" ; ".join(flags)) + "</span>")
    jours = [j for s in (it.get("semaines") or []) for j in (s.get("sujetion_jours") or [])]
    if jours:
        parts.append('<span style="color:#555">garde dim./férié : '
                     + esc(", ".join(jours)) + "</span>")
    if it.get("conges"):
        parts.append('<span style="color:#1a7a6e">🏖 Congés payés pris sur la période : '
                     + esc(_conges_txt(it["conges"])) + "</span>")
    return "<br>".join(parts) or _ZERO


def construire_mail_comptable(resume, mois_annee):
    """resume : liste par collaborateur produite par /export_resume_paie.
    Renvoie (sujet, texte, html) — texte = repli lisible, html = résumé
    collaborateur par collaborateur (une table par personne : une ligne par
    semaine civile avec dates, colonnes Total H+ / sup 25 % / sup 50 % /
    heures d'indemnité de sujétion = H+ des dimanches et jours fériés) +
    table « Total équipe ». Tous les relevés transmis sont réputés VALIDÉS
    (le serveur ne déclenche l'envoi qu'à cette condition)."""
    esc = html_mod.escape
    lignes_txt, lignes_equipe, sans_releve, a_faire = [], [], [], []
    tot = {"plus": 0.0, "sup25": 0.0, "sup50": 0.0, "comp": 0.0, "suj": 0.0}

    for it in resume:
        nom = f"{it['prenom']} {it['nom'].upper()}"
        if it.get("statut") == "manquant":
            if it.get("conges"):
                nom += f" (congés payés : {_conges_txt(it['conges'])})"
            sans_releve.append(nom)
            continue
        contrat = it.get("contrat_hebdo") or 0
        extras = []
        if it.get("saisi_par_admin"):
            extras.append("saisi par la pharmacie")
        if it.get("corrige"):
            extras.append("corrigé par la pharmacie")
        if it.get("ajuste"):
            extras.append("chiffres ajustés par la pharmacie")
        if it.get("commentaire"):
            extras.append(f"commentaire : {it['commentaire']}")
        note_extras = f" [{' ; '.join(extras)}]" if extras else ""

        # --- repli texte (une ligne par collaborateur, comme avant) ---
        ligne = f"  - {nom}" + (f" ({_fmt_h(contrat)}/sem)" if contrat else "")
        ligne += f" : +{_fmt_h(it['plus'])} / -{_fmt_h(it['moins'])}"
        if it["statut"] == "ok":
            if contrat >= 35:
                ligne += (f" → heures sup : {_fmt_h(it['sup25'])} à +25 %"
                          f", {_fmt_h(it['sup50'])} à +50 %")
            else:
                ligne += (f" → {_fmt_h(it['complementaires'])} complémentaires"
                          " (majoration 10 % / 25 %)")
            if it.get("sujetion"):
                ligne += f" · sujétion {_fmt_h(it['sujetion'])} (garde dim./férié)"
        elif it["statut"] == "sans_contrat":
            ligne += " → heures contractuelles non renseignées : ventilation à faire"
        else:  # sans_detail
            ligne += (f" → solde net {'+' if it['solde'] >= 0 else ''}{it['solde']} h"
                      " (pas de détail par jour : ventilation 25/50 à faire)")
        if it.get("conges"):
            ligne += f" · congés payés : {_conges_txt(it['conges'])}"
        lignes_txt.append(ligne + note_extras)

        # --- ligne HTML du tableau « Total équipe » (+ colonne Commentaires) ---
        col_collab = (f'<td style="{_TD};text-align:left"><b>{esc(nom)}</b>'
                      + (f'<br><span style="font-size:12px;color:#555;font-weight:normal">'
                         f"contrat {_fmt_h(contrat)}/sem</span>" if contrat else "")
                      + "</td>")
        if it["statut"] != "ok":
            motif = ("heures contractuelles non renseignées sur la fiche salarié"
                     if it["statut"] == "sans_contrat"
                     else "pas de détail jour par jour dans le relevé")
            a_faire.append(nom)
            tot["plus"] += it["plus"]
            comm = _cell_commentaires(it)
            comm = (f'<span style="color:#9C0006">ventilation à faire — {esc(motif)}</span>'
                    + ("<br>" + comm if comm != _ZERO else ""))
            lignes_equipe.append(
                "<tr>" + col_collab
                + f'<td style="{_TD_HP}">{_h(it["plus"])}</td>'
                f'<td style="{_TD}" colspan="2">ventilation à faire</td>'
                f'<td style="{_TD_SUJ}">{_ZERO}</td>'
                f'<td style="{_TD_COMM}">{comm}</td></tr>')
            continue

        temps_plein = contrat >= 35
        comp = it.get("complementaires") or 0
        tot["plus"] += it["plus"]
        tot["sup25"] += it["sup25"]
        tot["sup50"] += it["sup50"]
        tot["comp"] += comp
        tot["suj"] += it["sujetion"]
        lignes_equipe.append(
            "<tr>" + col_collab
            + f'<td style="{_TD_HP}">{_h(it["plus"])}</td>'
            + (f'<td style="{_TD}">{_h(it["sup25"])}</td>'
               f'<td style="{_TD}">{_h(it["sup50"])}</td>' if temps_plein
               else f'<td style="{_TD}" colspan="2">{_h(comp)}'
                    + (' <span style="font-size:12px;color:#555">compl.</span>' if comp else "")
                    + "</td>")
            + f'<td style="{_TD_SUJ}">{_h(it["sujetion"])}</td>'
            + f'<td style="{_TD_COMM}">{_cell_commentaires(it)}</td></tr>')

    sujet = f"Relevés d'heures validés — {mois_annee} — Pharmacie Apothical Nanterre Université"

    # ----- corps texte (repli) -----
    lignes = [
        "Bonjour,",
        "",
        f"Veuillez trouver ci-joint le récapitulatif Excel des relevés d'heures de {mois_annee},",
        "vérifiés et validés, pour l'établissement des bulletins de paie.",
        "",
        f"Résumé par collaborateur ({len(lignes_txt)} relevé(s) validé(s)) :",
        *lignes_txt,
        "",
        f"Total équipe : {_fmt_h(tot['plus'])} H+ · {_fmt_h(tot['sup25'])} à +25 % · "
        f"{_fmt_h(tot['sup50'])} à +50 %"
        + (f" · {_fmt_h(tot['comp'])} complémentaires" if tot["comp"] else "")
        + (f" · {_fmt_h(tot['suj'])} de sujétion" if tot["suj"] else ""),
    ]
    if sans_releve:
        lignes += ["", f"Sans relevé ce mois ({len(sans_releve)}) : " + ", ".join(sans_releve)]
    lignes += ["", "Nous restons à votre disposition,", "", SIGNATURE]
    texte = "\n".join(lignes)

    # ----- corps HTML -----
    lignes_equipe.append(
        f'<tr><td style="{_TD_TOT};text-align:left">Total équipe</td>'
        f'<td style="{_TD_TOT}">{_h(tot["plus"])}</td>'
        f'<td style="{_TD_TOT}">{_h(tot["sup25"])}'
        + (f" + {_h(tot['comp'])} compl." if tot["comp"] else "") + "</td>"
        f'<td style="{_TD_TOT}">{_h(tot["sup50"])}</td>'
        f'<td style="{_TD_TOT.replace("#eaf3ec", "#fdf1e3")}">{_h(tot["suj"])}</td>'
        f'<td style="{_TD_TOT}"></td></tr>')
    equipe = (f'<h2 style="font-size:16px;color:#1c4e2e;border-bottom:2px solid #1c4e2e;'
              f'padding-bottom:4px;margin:28px 0 0">Total équipe — {esc(mois_annee)}</h2>'
              + _table([("Collaborateur", _TH + "width:18%"), ("Total H+<br>du mois", _TH_HP),
                        ("Total h. sup<br>25&nbsp;%", _TH), ("Total h. sup<br>50&nbsp;%", _TH),
                        ('Total h. sujétion<br><span style="font-weight:normal">'
                         "(garde dim./férié)</span>", _TH_SUJ),
                        ("Commentaires", _TH + "width:28%")], lignes_equipe))
    notes = []
    if sans_releve:
        notes.append(f"Sans relevé ce mois ({len(sans_releve)}) : " + esc(", ".join(sans_releve)))
    if a_faire:
        notes.append("Ventilation à faire par vos soins pour : " + esc(", ".join(a_faire)))
    notes.append(
        "« Total H+ du mois » = ensemble des heures déclarées en plus sur le mois, dont la "
        "part au-delà de 35&nbsp;h hebdomadaires est ventilée par semaine civile en heures "
        "supplémentaires : +25&nbsp;% de la 36<sup>e</sup> à la 43<sup>e</sup> heure "
        "hebdomadaire, +50&nbsp;% au-delà (convention collective de la pharmacie d'officine ; "
        "heures complémentaires des temps partiels majorées 10&nbsp;% / 25&nbsp;%). Les heures "
        "d'indemnité de sujétion correspondent aux heures effectuées un dimanche ou un jour "
        "férié (base : 1,5 × valeur du point conventionnel × nombre d'heures, calcul effectué "
        "par vos soins) — les jours concernés sont listés dans la colonne Commentaires. Les "
        "congés payés indiqués sont ceux pris sur la période du relevé, comptés en jours "
        "ouvrables (lundi-samedi, hors jours fériés). Le détail par semaine et jour par jour "
        "figure dans le classeur Excel joint.")
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:900px;'
        'line-height:1.5">'
        "<p>Bonjour,</p>"
        f"<p>Veuillez trouver ci-dessous le total du mois par collaborateur — heures "
        f"supplémentaires et heures de garde (indemnité de sujétion) — de "
        f"<b>{esc(mois_annee)}</b>, avec les commentaires des salariés, ainsi que le "
        f"récapitulatif Excel des relevés en pièce jointe, pour l'établissement des "
        f"bulletins de paie.</p>"
        + equipe
        + "".join(f'<p style="font-size:12.5px;color:#555">{n}</p>' for n in notes)
        + "<p>Nous restons à votre disposition,</p>"
        + '<div style="font-size:13px;color:#333;border-top:1px solid #ccc;padding-top:12px">'
        + esc(SIGNATURE).replace("\n", "<br>") + "</div></div>")
    return sujet, texte, html


def main():
    p = json.loads(os.environ.get("PAYLOAD") or "{}") or {}
    mois = int(p.get("mois") or datetime.now().month)
    annee = int(p.get("annee") or datetime.now().year)
    destinataires = [d.strip() for d in (p.get("destinataires") or "").split(",") if d.strip()]
    if not destinataires:
        print("Aucun destinataire comptable dans le payload — rien à envoyer.")
        return
    mois_annee = f"{MOIS_FR[mois]} {annee}"

    with urllib.request.urlopen(
            f"{BASE_URL}/export_resume_paie?cle={CLE}&mois={mois}&annee={annee}", timeout=30) as r:
        resume = json.loads(r.read().decode("utf-8"))
    with urllib.request.urlopen(
            f"{BASE_URL}/export_recap?cle={CLE}&mois={mois}&annee={annee}", timeout=60) as r:
        xlsx = r.read()

    sujet, texte, corps_html = construire_mail_comptable(resume, mois_annee)

    gmail_user = os.environ["GMAIL_USER"]
    gmail_pwd = os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEMultipart("mixed")
    msg["From"] = gmail_user
    msg["To"] = ", ".join(destinataires)
    msg["Cc"] = ADMIN_EMAIL
    msg["Subject"] = sujet
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(texte, "plain", "utf-8"))
    alt.attach(MIMEText(corps_html, "html", "utf-8"))
    msg.attach(alt)
    part = MIMEBase("application", "octet-stream")
    part.set_payload(xlsx)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition",
                    f'attachment; filename="Releves_{MOIS_FR[mois]}_{annee}.xlsx"')
    msg.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pwd)
        server.sendmail(gmail_user, destinataires + [ADMIN_EMAIL], msg.as_string())
    print(f"Dossier paie {mois_annee} envoyé à {', '.join(destinataires)} (copie admin).")


if __name__ == "__main__":
    main()
