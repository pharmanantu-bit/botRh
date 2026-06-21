import os
from dotenv import load_dotenv

# Dossier du projet — ancre les chemins en absolu (le serveur WSGI ne s'exécute
# pas depuis le dossier du projet, sinon les fichiers sont introuvables).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

EMPLOYEES_FILE = os.path.join(BASE_DIR, "employees.csv")
DOCUMENTS_FOLDER = os.path.join(BASE_DIR, "documents")
LOGS_FOLDER = os.path.join(BASE_DIR, "logs")

# Jour du mois pour l'envoi automatique
SEND_DAY = 20
