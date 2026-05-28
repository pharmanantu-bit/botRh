from datetime import datetime
import logging
import os

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(f"logs/run_daily_{datetime.now().strftime('%Y_%m')}.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

jour = datetime.now().day
logging.info(f"run_daily.py lancé — jour du mois : {jour}")

if jour == 20:
    logging.info("Jour 20 — envoi des relevés mensuels")
    from email_sender import send_emails
    send_emails()

elif jour == 22:
    logging.info("Jour 22 — envoi des relances")
    from relance_sender import send_relances
    send_relances()

else:
    logging.info("Rien à faire aujourd'hui.")
