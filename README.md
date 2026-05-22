# Bot Mail Pharmacie

Envoi automatique de mails aux employés (relevés mensuels des heures) le 20 de chaque mois.

## Structure

```
bot/
├── email_sender.py          # Script principal d'envoi
├── config.py                # Configuration
├── employees.csv            # Liste des employés
├── documents/               # Dossier où déposer les Word à envoyer
├── logs/                    # Logs d'envoi (créé automatiquement)
├── .env                     # Tes identifiants Gmail (ne pas partager)
├── setup_task_scheduler.ps1 # Script d'installation planificateur Windows
└── requirements.txt
```

## Installation

### 1. Installer Python
Télécharger sur https://python.org et cocher "Add to PATH"

### 2. Installer les dépendances
```
pip install -r requirements.txt
```

### 3. Créer le fichier .env
Copier `.env.example` en `.env` et remplir tes identifiants :
```
GMAIL_USER=pharmanantu@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

> **Mot de passe d'application Gmail** : Va sur myaccount.google.com → Sécurité → Validation en 2 étapes → Mots de passe des applications

### 4. Remplir la liste des employés
Ouvrir `employees.csv` et ajouter chaque employé :
```
nom,prenom,email
Dupont,Marie,marie.dupont@gmail.com
```

### 5. Créer le dossier documents
```
mkdir documents
```
Dépose tes fichiers Word (.docx) dans ce dossier avant chaque envoi.

### 6. Tester l'envoi
```
python email_sender.py
```

### 7. Programmer l'envoi automatique (une seule fois)
Lancer PowerShell en Administrateur et exécuter :
```
.\setup_task_scheduler.ps1
```
Le bot enverra automatiquement les mails le **20 de chaque mois à 8h00**.

## Utilisation mensuelle

1. Dépose le fichier Word dans le dossier `documents/`
2. Le 20 du mois, le bot envoie automatiquement
3. Vérifie les logs dans `logs/` pour confirmer les envois
