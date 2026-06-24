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

# --- Assistant RH (lecture Gmail + analyse Claude, exécuté sur le runner GitHub) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ASSISTANT_MODELE = os.getenv("ASSISTANT_MODELE", "claude-haiku-4-5")
# Expéditeurs surveillés (séparés par des virgules ; "@domaine" = tout le domaine)
COMPTA_EMAILS = os.getenv("COMPTA_EMAILS", "")        # ex : jan@expertise-compta.com
PLANNING_SENDER = os.getenv("PLANNING_SENDER", "")    # ex : noreply@monplanningpharma.fr
ADMIN_RH_DOMAINS = os.getenv("ADMIN_RH_DOMAINS", "")  # ex : @urssaf.fr, @ameli.fr
ASSISTANT_JOURS = int(os.getenv("ASSISTANT_JOURS", "2"))
ASSISTANT_MAX_MAILS = int(os.getenv("ASSISTANT_MAX_MAILS", "25"))
