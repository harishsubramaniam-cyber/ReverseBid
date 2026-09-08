"""Application configuration, driven entirely by environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("RA_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_DIR = DATA_DIR / "outbox"
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("RA_DATABASE_URL", f"sqlite:///{DATA_DIR / 'reverse_auction.db'}")
SECRET_KEY = os.getenv("RA_SECRET_KEY", "dev-secret-change-me")
APP_NAME = os.getenv("RA_APP_NAME", "ReverseBid")
BASE_URL = os.getenv("RA_BASE_URL", "http://localhost:8000")
CURRENCY = os.getenv("RA_CURRENCY", "INR")
CURRENCY_SYMBOL = os.getenv("RA_CURRENCY_SYMBOL", "₹")

# ---------------------------------------------------------------- email
SMTP_HOST = os.getenv("RA_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("RA_SMTP_PORT", "587"))
SMTP_USER = os.getenv("RA_SMTP_USER", "")
SMTP_PASSWORD = os.getenv("RA_SMTP_PASSWORD", "")
SMTP_STARTTLS = os.getenv("RA_SMTP_STARTTLS", "1") not in ("0", "false", "False")
SMTP_SSL = os.getenv("RA_SMTP_SSL", "0") not in ("0", "false", "False")
MAIL_FROM = os.getenv("RA_MAIL_FROM", "no-reply@reversebid.local")
MAIL_FROM_NAME = os.getenv("RA_MAIL_FROM_NAME", APP_NAME)

#: When no SMTP host is configured the mailer writes .eml files to the outbox
#: instead of sending. Every message is recorded in the database either way.
EMAIL_ENABLED = bool(SMTP_HOST)

# ---------------------------------------------------------------- engine
SCHEDULER_INTERVAL_SECONDS = int(os.getenv("RA_SCHEDULER_INTERVAL", "5"))
ENDING_SOON_MINUTES = int(os.getenv("RA_ENDING_SOON_MINUTES", "5"))
STARTING_SOON_MINUTES = int(os.getenv("RA_STARTING_SOON_MINUTES", "30"))
