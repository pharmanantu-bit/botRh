"""Rappel de validation (le 25 au soir) : mail à l'admin listant les relevés
reçus non validés et les relevés manquants, avant le départ automatique du
récap paie le 26 au matin. Exécuté côté RUNNER GitHub (run_send.py, mode
rappel_validation) — le constructeur de mail est pur pour être testable sans
SMTP (même pattern que conges_sender.construire_mail)."""
from tokens import reponse_de

BASE_URL = "https://pharmacie92000.pythonanywhere.com"


def construire_mail_rappel(employes, reponses, mois_annee):
    """employes : liste {nom, prenom, email} (export_employes) ;
    reponses : dict token -> relevé (export_reponses, brut).
    Renvoie (sujet, corps)."""
    valides, non_valides, manquants = [], [], []
    for e in employes:
        r = reponse_de(reponses, e["prenom"], e["email"])
        if r is None:
            manquants.append(e)
        elif r.get("valide"):
            valides.append((e, r))
        else:
            non_valides.append((e, r))

    total = len(employes)
    tout_ok = not non_valides and not manquants

    if tout_ok:
        sujet = f"botRh — Relevés {mois_annee} : tout est validé ✓"
        corps = (f"Bonjour,\n\n"
                 f"Les {total} relevés de {mois_annee} sont tous reçus et validés.\n"
                 f"Le récap Excel pour la paie partira automatiquement demain matin (le 26).\n\n"
                 f"Rien à faire.\n\nbotRh")
        return sujet, corps

    sujet = (f"botRh — À valider : {len(non_valides)} relevé(s) non validé(s), "
             f"{len(manquants)} manquant(s) — {mois_annee}")

    lignes = [
        "Bonjour,",
        "",
        f"Point de validation des relevés de {mois_annee} — la saisie est clôturée ce soir (le 25).",
        "Le récap Excel pour la paie partira automatiquement demain matin (le 26).",
        "",
        f"✅ Validés : {len(valides)}/{total}",
    ]
    if non_valides:
        lignes += ["", f"⏳ Reçus mais NON validés ({len(non_valides)}) :"]
        lignes += [f"  - {e['prenom']} {e['nom']} (reçu le {r.get('date', '?')})"
                   for e, r in non_valides]
    if manquants:
        lignes += ["", f"❌ Relevés manquants ({len(manquants)}) :"]
        lignes += [f"  - {e['prenom']} {e['nom']}" for e in manquants]
    lignes += ["", f"Valider les relevés : {BASE_URL}/admin/mois", "", "botRh"]
    return sujet, "\n".join(lignes)
