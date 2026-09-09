"""Banc d'essai de COMPRÉHENSION de l'agent RH.

Mesure, sur des phrases telles que l'utilisateur les tape réellement (accents,
fautes de frappe, tournures orales), que la chaîne comprend la demande :

  1. HORS-LIGNE (défaut, gratuit, aucune requête réseau) :
     - pseudonymisation : les prénoms/noms cités sont bien remplacés par
       « Employé X » (sauf cas marqués typo=True, gérés par chercher_salarie) ;
     - routage : le sous-catalogue envoyé au modèle (selectionner_domaines +
       specs_pour) contient bien le ou les outils attendus.
  2. RÉEL (--reel) : appelle le vrai moteur (Mistral par défaut) phrase par
     phrase et vérifie que le modèle choisit un outil attendu. L'exécuteur est
     un ESPION : il journalise l'appel et renvoie une réponse simulée — AUCUNE
     écriture réelle, aucun mail, la conversation de l'agent n'est pas touchée.
     Une pause entre phrases (--pause, défaut 8 s) respecte le palier Mistral
     gratuit (25 000 tokens/minute).

Usage :  python test_comprehension.py            # hors-ligne
         python test_comprehension.py --reel     # + choix d'outil par le modèle
         python test_comprehension.py --reel --pause 12
Code retour : 1 si un contrôle HORS-LIGNE échoue (régression sûre) ; les échecs
du mode réel sont informatifs (le modèle peut varier) mais listés en clair.

À relancer après toute modification de l'agent (prompt, outils, routage) et à
enrichir avec les vraies phrases qui ont déjà été mal comprises en production.
"""
import argparse
import sys
import time

import app as A  # noqa: F401 — à importer AVANT agent_outils (import circulaire)
import agent_rh
from agent_outils import _moteur
from assistant_rh import annuaire_pseudo, construire_table, pseudonymiser_texte

# ── Cas d'essai ──────────────────────────────────────────────────────────────
# phrase   : telle que tapée (accents volontaires : le CSV est SANS accents).
# attendus : outils acceptés (le premier suffit) — plusieurs choix légitimes.
# typo     : True = le nom est volontairement estropié ; la pseudonymisation ne
#            peut pas le rattraper (c'est chercher_salarie qui doit le faire),
#            on ne compte donc pas la « fuite » comme un échec.
CAS = [
    # Planning / absences / congés
    {"phrase": "Mélanie est malade demain",
     "attendus": {"ajouter_absence", "modifier_horaires_jour"}},
    {"phrase": "Maëlys sera en congés du 15 au 20 décembre",
     "attendus": {"ajouter_absence"}},
    {"phrase": "qui travaille demain ?", "attendus": {"planning_jour"}},
    {"phrase": "quels sont les horaires de Stéphanie cette semaine ?",
     "attendus": {"planning_collaborateur"}},
    {"phrase": "combien de jours de congés reste-t-il à Sophie ?",
     "attendus": {"solde_conges"}},
    {"phrase": "il y a des demandes de congés en attente ?",
     "attendus": {"demandes_conges_en_attente"}},
    {"phrase": "accepte la demande de congés d'Amandine",
     "attendus": {"traiter_demande_conges", "demandes_conges_en_attente"}},
    {"phrase": "Sammy arrive à 10h demain au lieu de 9h",
     "attendus": {"modifier_horaires_jour"}},
    {"phrase": "supprime l'absence de Lise de jeudi",
     "attendus": {"supprimer_absence", "absences_en_cours"}},
    {"phrase": "qui est absent en ce moment ?", "attendus": {"absences_en_cours"}},
    {"phrase": "qui peut remplacer Maëlys demain matin ?",
     "attendus": {"proposer_remplacant"}},
    # Relevés d'heures / paie
    {"phrase": "qui n'a pas rendu son relevé d'heures ?",
     "attendus": {"releves_manquants"}},
    {"phrase": "combien d'heures sup a fait Chérine en août ?",
     "attendus": {"stats_heures", "releve_du_mois"}},
    {"phrase": "relance ceux qui n'ont pas rendu leur relevé",
     "attendus": {"releves_manquants", "envoyer_relance", "preparer_relance"}},
    {"phrase": "valide le relevé de Thibaud", "attendus": {"valider_releve"}},
    {"phrase": "montre-moi le récap paie avant l'envoi au comptable",
     "attendus": {"apercu_recap_comptable"}},
    # Dossier salarié
    {"phrase": "montre-moi le dossier de Khadijetou",
     "attendus": {"dossier_salarie", "profil_salarie"}},
    {"phrase": "quels documents manquent dans les dossiers de l'équipe ?",
     "attendus": {"documents_manquants_equipe"}},
    {"phrase": "génère une attestation de travail pour Elsa",
     "attendus": {"generer_attestation", "preparer_attestation"}},
    {"phrase": "note dans le journal de Shirley : entretien annuel réalisé",
     "attendus": {"ajouter_note_journal"}},
    {"phrase": "mets à jour le téléphone d'Émilie : 06 12 34 56 78",
     "attendus": {"mettre_a_jour_profil"}},
    {"phrase": "la visite médicale de Lionel est prévue le 12/10",
     "attendus": {"mettre_a_jour_profil", "profil_salarie", "ajouter_note_journal"}},
    # Mails RH
    {"phrase": "qu'a demandé le comptable dans ses mails ?",
     "attendus": {"mails_rh_du_jour"}},
    {"phrase": "envoie un mail à Sabah pour lui rappeler sa visite médicale",
     "attendus": {"envoyer_mail", "preparer_mail"}},
    # Recrutement
    {"phrase": "liste les candidats reçus", "attendus": {"lister_candidats"}},
    {"phrase": "convoque le candidat Martin en entretien mardi",
     "attendus": {"preparer_mail_convocation", "envoyer_mail_candidat", "fiche_candidat",
                  "rechercher_candidat"}},
    # Base / transverse
    {"phrase": "quelles sont les échéances RH à venir ?",
     "attendus": {"echeances_a_venir"}},
    {"phrase": "c'était une erreur, annule ce que tu viens de faire",
     "attendus": {"annuler_derniere_action"}},
    {"phrase": "retiens que le comptable veut le récap le 24 de chaque mois",
     "attendus": {"memoriser"}},
    # Noms estropiés : chercher_salarie doit rattraper
    {"phrase": "la fiche de Stefany s'il te plaît", "typo": True,
     "attendus": {"chercher_salarie", "profil_salarie"}},
    {"phrase": "qui est Kadijetou ?", "typo": True,
     "attendus": {"chercher_salarie", "profil_salarie", "lister_employes"}},
]

REPONSE_SIMULEE_PAR_OUTIL = {
    "demandes_conges_en_attente": "1 demande en attente : id d-banc1, Employé A, "
                                  "du 2026-12-21 au 2026-12-22, solde suffisant.",
    "releves_manquants": "2 manquants ce mois : Employé A, Employé C.",
    "solde_conges": "Employé A : droit 25 j, posés 10 j, restant 15 j.",
    "lister_candidats": "1 candidat : Martin (Reçu).",
    "chercher_salarie": "C'est :\n- Employé C : préparatrice (actif)",
}


def executer_espion(journal):
    """Exécuteur passé à run_agent : journalise, ne touche à rien."""
    def executer(nom, args, annuaire):
        journal.append(nom)
        return REPONSE_SIMULEE_PAR_OUTIL.get(
            nom, "OK — réponse simulée du banc d'essai (aucune donnée réelle).")
    return executer


def main():
    ap = argparse.ArgumentParser(description="Banc d'essai de compréhension de l'agent RH")
    ap.add_argument("--reel", action="store_true",
                    help="appelle le vrai moteur IA (choix d'outil par le modèle)")
    ap.add_argument("--pause", type=float, default=8.0,
                    help="pause en secondes entre phrases en mode réel (défaut 8)")
    args = ap.parse_args()

    employes = A.charger_employes()
    if not employes:
        print("employees.csv introuvable ou vide : banc d'essai impossible.")
        return 1
    table, _ = construire_table(employes)
    annuaire = annuaire_pseudo(employes)
    with A.app.app_context():
        roster = A._roster_pseudo(annuaire, A.charger_profils())
    moteur, modele = _moteur()

    ko_offline, ko_reel = [], []
    for i, cas in enumerate(CAS, 1):
        phrase, attendus = cas["phrase"], cas["attendus"]
        pseudo = pseudonymiser_texte(phrase, table)
        # 1) pseudonymisation : plus aucun prénom réel (sauf typo volontaire)
        fuite = any(p and p.lower() in pseudo.lower()
                    for p in (e.get("prenom") for e in employes))
        ok_pseudo = cas.get("typo") or not fuite
        # 2) routage : les outils attendus partent bien au modèle
        doms = agent_rh.selectionner_domaines([{"role": "user", "content": pseudo}])
        noms_specs = {s["nom"] for s in agent_rh.specs_pour(doms)}
        ok_route = bool(attendus & noms_specs)
        if not (ok_pseudo and ok_route):
            ko_offline.append((phrase, "pseudonymisation" if not ok_pseudo else
                               f"routage (domaines={sorted(doms) if doms else 'TOUS'})"))
        etat = "OK " if (ok_pseudo and ok_route) else "KO "

        # 3) mode réel : quel outil le modèle choisit-il ?
        outil_txt = ""
        if args.reel:
            journal, t0 = [], time.time()
            try:
                agent_rh.run_agent([{"role": "user", "content": phrase}], employes,
                                   executer_espion(journal), moteur=moteur, modele=modele,
                                   roster_txt=roster, mode="validation")
                ok_choix = bool(set(journal) & attendus)
                outil_txt = f" · modèle -> {journal or ['(aucun outil)']} · {time.time() - t0:.0f}s"
                if not ok_choix:
                    etat = "KO "
                    ko_reel.append((phrase, journal))
            except Exception as e:
                etat = "KO "
                outil_txt = f" · ERREUR moteur ({time.time() - t0:.0f}s) : {e}"
                ko_reel.append((phrase, [f"erreur : {e}"]))

        print(f"{etat}[{i:2}/{len(CAS)}] {phrase}{outil_txt}", flush=True)
        if args.reel and i < len(CAS):
            time.sleep(args.pause)

    print()
    n = len(CAS)
    print(f"HORS-LIGNE : {n - len(ko_offline)}/{n} OK"
          + (f" — échecs : {[p for p, _ in ko_offline]}" if ko_offline else ""))
    if args.reel:
        print(f"RÉEL ({moteur}/{modele}) : {n - len(ko_reel)}/{n} OK")
        for phrase, outils in ko_reel:
            print(f"   KO : {phrase!r} -> {outils}")
    for phrase, motif in ko_offline:
        print(f"   KO hors-ligne : {phrase!r} ({motif})")
    return 1 if ko_offline else 0


if __name__ == "__main__":
    sys.exit(main())
