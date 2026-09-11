"""Application configuration, driven entirely by environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Read a plain ``.env`` file sitting next to the app, if there is one.

    Real environment variables always win, so a .env file is a convenience for
    running locally and never overrides how a server is configured.
    """
    path = Path(os.getenv("RA_ENV_FILE", BASE_DIR / ".env"))
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()
DATA_DIR = Path(os.getenv("RA_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_DIR = DATA_DIR / "outbox"
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("RA_DATABASE_URL", f"sqlite:///{DATA_DIR / 'reverse_auction.db'}")


def _secret_key() -> str:
    """The key that signs session cookies.

    ``RA_SECRET_KEY`` always wins. With nothing set we generate a random key
    once and keep it in the data folder, rather than falling back to a value
    that is printed in the source: a shipped default key means anyone can mint
    a cookie for any account.
    """
    from_env = os.getenv("RA_SECRET_KEY", "").strip()
    if from_env:
        return from_env
    path = DATA_DIR / "secret_key"
    try:
        if path.is_file():
            saved = path.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        import secrets as _secrets
        generated = _secrets.token_urlsafe(48)
        path.write_text(generated, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:      # pragma: no cover - Windows and odd filesystems
            pass
        print("No RA_SECRET_KEY was set, so a random one was generated and saved to "
              f"{path}. Sign-ins stay valid across restarts. Set RA_SECRET_KEY in "
              "production, especially if you run more than one process.")
        return generated
    except OSError:          # pragma: no cover - read-only data folder
        import secrets as _secrets
        print("WARNING: RA_SECRET_KEY is not set and the data folder is not writable, so "
              "a temporary key is in use. Everyone will be signed out when this process "
              "restarts.")
        return _secrets.token_urlsafe(48)


SECRET_KEY = _secret_key()
APP_NAME = os.getenv("RA_APP_NAME", "ReverseBid")
#: The address people actually reach this app on. It goes into every link in
#: every email, and it decides whether session cookies are marked "secure", so
#: getting it wrong sends suppliers links to localhost. Hosts that know their
#: own address publish it, so use that when RA_BASE_URL has not been set.
BASE_URL = (os.getenv("RA_BASE_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or os.getenv("RAILWAY_PUBLIC_DOMAIN_URL")
            or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
                if os.getenv("RAILWAY_PUBLIC_DOMAIN") else "")
            or (f"https://{os.environ['FLY_APP_NAME']}.fly.dev"
                if os.getenv("FLY_APP_NAME") else "")
            or "http://localhost:8000").rstrip("/")
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

# ---------------------------------------------------------------- documents
MAX_UPLOAD_MB = int(os.getenv("RA_MAX_UPLOAD_MB", "10"))
MAX_ATTACHMENTS = int(os.getenv("RA_MAX_ATTACHMENTS", "30"))
#: How long an invitation link to set a password stays usable.
INVITE_DAYS = int(os.getenv("RA_INVITE_DAYS", "30"))

# ---------------------------------------------------------------- behind a proxy
#: How many reverse proxies sit in front of this app. Zero - the default, and
#: the right answer when you run it yourself - means X-Forwarded-For is
#: ignored entirely, because anyone can put whatever they like in it. Set it
#: to 1 behind a single nginx or load balancer, and so on.
TRUSTED_PROXIES = int(os.getenv("RA_TRUSTED_PROXIES", "0"))

# ---------------------------------------------------------------- demo mode
#: Fill an empty database with the sample company on startup. For a
#: try-it-out deployment, where the point is that anyone who opens the link
#: can sign in and click around. Never set this on an installation with real
#: auctions in it: it only acts when the database is completely empty, but the
#: accounts it creates have a published password.
DEMO_SEED = os.getenv("RA_DEMO_SEED", "0") not in ("0", "false", "False", "")
