import os
from dotenv import load_dotenv

load_dotenv()

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

EMPLOYEES_FILE = "contacts (3).csv"
DOCUMENTS_FOLDER = "documents"
LOGS_FOLDER = "logs"

# Jour du mois pour l'envoi automatique
SEND_DAY = 20
