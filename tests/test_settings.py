"""Settings: what a badly filled-in box does, and what the screen then says.

Two failures live here, both of which look like "the app is broken" to the
person running it:

* A setting typed into a hosting dashboard arrives as a *string*, and people
  leave boxes empty or paste "587 " with a space. Anything the app reads with
  int() used to bring the whole site down at startup with the reason buried in
  a deploy log. Nothing here may crash; a value that cannot be read falls back
  to the shipped default.
* The Outbox page used to tell everybody to edit a ``.env`` file. On Render,
  Railway or Fly there is no file to edit - the settings are typed into a
  dashboard - so that advice sent people looking for a file that cannot exist
  and their email never went. The page has to know where it is running.

Each case runs in its own subprocess, because settings are read once when the
app starts and that is exactly the behaviour being tested.

    python tests/test_settings.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def run(script: str, env_extra: dict[str, str]) -> tuple[int, str]:
    """Start the app in a clean process with these settings and report back."""
    tmp = tempfile.mkdtemp(prefix="ra-settings-")
    env = dict(os.environ)
    # Wipe anything the developer's own shell has set, so the test measures
    # the app and not the machine it runs on.
    for key in list(env):
        if key.startswith(("RA_", "RENDER", "RAILWAY", "FLY_", "DYNO")):
            env.pop(key)
    env.update({
        "RA_DATA_DIR": tmp,
        "RA_DATABASE_URL": f"sqlite:///{tmp}/test.db",
        "RA_ENV_FILE": f"{tmp}/none.env",
        "PYTHONPATH": str(ROOT),
    })
    env.update(env_extra)
    done = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True, timeout=180)
    return done.returncode, (done.stdout + done.stderr)


READ_SETTINGS = """
from app import config
print("PORT=%r" % config.SMTP_PORT)
print("HOST=%r" % config.SMTP_HOST)
print("ENABLED=%r" % config.EMAIL_ENABLED)
print("PROXIES=%r" % config.TRUSTED_PROXIES)
print("SEED=%r" % config.DEMO_SEED)
print("STARTTLS=%r" % config.SMTP_STARTTLS)
print("WHERE=%r" % config.HOST_NAME)
"""

OUTBOX_PAGE = """
from fastapi.testclient import TestClient
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Role, User
from app.security import hash_password

Base.metadata.create_all(bind=engine)
db = SessionLocal()
db.add(User(name="Buyer", email="b@m.local", role=Role.BUYER,
            password_hash=hash_password("test1234")))
db.commit()
db.close()

client = TestClient(app, base_url="http://test")
client.get("/login")
token = client.cookies.get("ra_csrf")
client.post("/login", data={"email": "b@m.local", "password": "test1234", "next": "/"},
            headers={"X-CSRF-Token": token or ""}, follow_redirects=False)
client.headers.update({"accept": "text/html,application/xhtml+xml"})
print("<<<PAGE>>>")
print(client.get("/outbox").text)
"""


API_SETTINGS = """
from app import config
print("ENABLED=%r" % config.EMAIL_ENABLED)
print("API=%r" % config.MAIL_API)
"""

# The send is intercepted at the last possible moment - the outgoing web
# request - so everything the app builds is measured, and nothing leaves.
CAPTURE_SEND = """
import io, urllib.request
sent = {}

class Fake(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False

def fake_urlopen(request, timeout=None):
    sent["url"] = request.full_url
    sent["headers"] = dict(request.headers)
    sent["body"] = request.data.decode()
    return Fake(b'{"messageId":"1"}')

urllib.request.urlopen = fake_urlopen

from app.db import Base, SessionLocal, engine
from app import mailer
from app.models import EmailMessage
Base.metadata.create_all(bind=engine)

db = SessionLocal()
row = mailer.queue_email(db, to_email="supplier@example.com", to_name="A Supplier",
                         subject="Starts soon: Pens", html_body="<p>Please bid.</p>",
                         event="starting_soon")
msg_id = row.id
db.close()
print("STATUS=%r" % mailer.deliver(msg_id))
print(sent["url"])
print(sent["headers"])
print(sent["body"])
"""

REFUSED_SEND = """
import io, urllib.error, urllib.request

def fake_urlopen(request, timeout=None):
    raise urllib.error.HTTPError(
        request.full_url, 400, "Bad Request", {},
        io.BytesIO(b'{"message":"sender address is not valid"}'))

urllib.request.urlopen = fake_urlopen

from app.db import Base, SessionLocal, engine
from app import mailer
Base.metadata.create_all(bind=engine)
db = SessionLocal()
row = mailer.queue_email(db, to_email="s@example.com", subject="Hello",
                         html_body="<p>Hi.</p>")
msg_id = row.id
db.close()
print("STATUS=%r" % mailer.deliver(msg_id))
db = SessionLocal()
print(db.get(mailer.EmailMessage, msg_id).error)
db.close()
"""

VERSION_CHECK = """
from fastapi.testclient import TestClient
from app.main import app
body = TestClient(app, base_url="http://test").get("/healthz").json()
print("VERSION", repr(body.get("version")))
print("MODE", repr(body.get("email_mode")))
"""

EXPLAIN_BLOCKED = """
from app import mailer
print(mailer.explain(OSError(101, "Network is unreachable")))
"""


def main() -> int:
    print("\nA. A settings box filled in badly must not take the site down")
    for label, value, expected in [
        ("left completely empty", "", 587),
        ("with a stray space", " 587 ", 587),
        ("typed as words", "five eight seven", 587),
        ("pasted with a decimal point", "465.0", 465),
        ("filled in properly", "2525", 2525),
    ]:
        code, out = run(READ_SETTINGS, {"RA_SMTP_PORT": value})
        check(f"the mail port {label} starts the app", code == 0, out.strip()[-200:])
        check(f"...and reads as {expected}", f"PORT={expected!r}" in out,
              out.strip()[:120])

    code, out = run(READ_SETTINGS, {"RA_TRUSTED_PROXIES": "", "RA_MAX_UPLOAD_MB": "lots"})
    check("an empty proxy count and a nonsense size still start", code == 0,
          out.strip()[-200:])
    check("...and the proxy count falls back to none", "PROXIES=0" in out)

    print("\nB. A yes/no setting written the way people actually write it")
    for value, expected in [("1", True), ("true", True), ("yes", True), ("on", True),
                            ("0", False), ("false", False), ("no", False),
                            ("off", False), ("", False)]:
        code, out = run(READ_SETTINGS, {"RA_DEMO_SEED": value})
        check(f"RA_DEMO_SEED={value!r} means {expected}",
              code == 0 and f"SEED={expected!r}" in out, out.strip()[:120])

    print("\nC. A mail host with spaces round it is still a mail host")
    code, out = run(READ_SETTINGS, {"RA_SMTP_HOST": "  smtp.gmail.com  "})
    check("the spaces are dropped rather than sent to the mail server",
          "HOST='smtp.gmail.com'" in out, out.strip()[:160])
    check("...and sending is switched on", "ENABLED=True" in out)

    print("\nD. Nothing set at all is still practice mode")
    code, out = run(READ_SETTINGS, {})
    check("no mail host means nothing is sent", "ENABLED=False" in out)
    check("...and the app does not think it is on a hosting service",
          "WHERE=''" in out, out.strip()[:160])

    print("\nE. The Outbox tells you where to put the settings — on your own PC")
    code, out = run(OUTBOX_PAGE, {})
    page = out.split("<<<PAGE>>>", 1)[-1]
    check("the page loads", code == 0 and "Practice mode" in page, out.strip()[-300:])
    check("it points at the .env file next to the app", ".env" in page)
    check("...and does not invent a hosting service",
          "Environment → Add environment variable" not in page)

    print("\nF. The same page, on Render, where there is no file to edit")
    # RA_BASE_URL only so the test client keeps its cookie: on a real https
    # address the session cookie is marked "secure", which is correct there
    # and simply unreachable over the loopback the tests use.
    code, out = run(OUTBOX_PAGE, {"RENDER": "true", "RA_BASE_URL": "http://test"})
    page = out.split("<<<PAGE>>>", 1)[-1]
    check("the page loads", code == 0 and "Practice mode" in page, out.strip()[-300:])
    check("it names the service you are actually on", "Render" in page)
    check("it names the five settings to add",
          all(name in page for name in ("RA_SMTP_HOST", "RA_SMTP_PORT", "RA_SMTP_USER",
                                        "RA_SMTP_PASSWORD", "RA_MAIL_FROM")))
    check("it says where to type them", "Add environment variable" in page)
    check("it no longer tells you to copy a .env file that cannot exist",
          ".env.example" not in page)
    check("...and it explains why the rows say “Saved here”", "Saved here" in page)

    print("\nG. With a mail server set, the page says so instead")
    code, out = run(OUTBOX_PAGE, {"RENDER": "true", "RA_SMTP_HOST": "smtp.example.com",
                                  "RA_SMTP_USER": "me@example.com",
                                  "RA_MAIL_FROM": "me@example.com"})
    page = out.split("<<<PAGE>>>", 1)[-1]
    check("the practice-mode warning is gone", "Practice mode" not in page,
          out.strip()[-300:])
    check("it names the mail server it will use", "smtp.example.com" in page)
    check("...and never prints the password", "RA_SMTP_PASSWORD" not in page)

    print("\nH. An email service key is enough on its own — no mail server needed")
    code, out = run(API_SETTINGS, {"RA_MAIL_API_KEY": "xkeysib-abc123"})
    check("a Brevo key switches sending on", "ENABLED=True" in out, out.strip()[-200:])
    check("...and the service is recognised from the key alone", "API='brevo'" in out)
    code, out = run(API_SETTINGS, {"RA_MAIL_API_KEY": "re_abc123"})
    check("a Resend key is recognised too", "API='resend'" in out, out.strip()[:200])
    code, out = run(API_SETTINGS, {"RA_MAIL_API": "brevo"})
    check("naming a service with no key does not pretend to be switched on",
          "ENABLED=False" in out and "API=''" in out, out.strip()[:200])

    print("\nI. The message really is built for that service")
    for key, expect in [("xkeysib-k", ("api.brevo.com", "htmlContent", "api-key")),
                        ("re_k", ("api.resend.com", '"html"', "Bearer"))]:
        code, out = run(CAPTURE_SEND, {"RA_MAIL_API_KEY": key,
                                       "RA_MAIL_FROM": "me@example.com"})
        check(f"{expect[0]} is asked to send it", code == 0 and expect[0] in out,
              out.strip()[-250:])
        for needle in expect[1:]:
            # urllib tidies header capitalisation on the way out ("api-key"
            # becomes "Api-key"), and header names are case-insensitive.
            check(f"...with {needle}", needle.lower() in out.lower())
        check("...to the right person", "supplier@example.com" in out)
        check("...and the subject and body travel with it",
              "Starts soon" in out and "Please bid" in out)

    print("\nJ. A refusal by the service is passed on in its own words")
    code, out = run(REFUSED_SEND, {"RA_MAIL_API_KEY": "xkeysib-k"})
    check("the failure is recorded against the message", "STATUS='failed'" in out,
          out.strip()[-250:])
    check("...saying who refused it", "brevo refused" in out)
    check("...and what they said", "sender address is not valid" in out)

    print("\nK. A blocked port is explained as a blocked port, not a mail problem")
    code, out = run(EXPLAIN_BLOCKED, {"RENDER": "true"})
    check("“Network is unreachable” is translated", "blocks outbound" in out,
          out.strip()[-300:])
    check("...it names the service doing the blocking", "Render's network" in out)
    check("...and gives the way round it", "RA_MAIL_API_KEY" in out)
    check("...without blaming the password", "App password" not in out)

    print("\nL. The running build says which build it is")
    code, out = run(VERSION_CHECK, {})
    check("/healthz names the version, without a password", "VERSION" in out and
          (ROOT / "VERSION").read_text().strip() in out, out.strip()[-250:])
    check("...and says how email is being sent", "'outbox'" in out, out.strip()[-250:])
    code, out = run(VERSION_CHECK, {"RA_MAIL_API_KEY": "xkeysib-k"})
    check("...which changes when an email service is switched on", "'brevo'" in out,
          out.strip()[-250:])

    print("\n" + "-" * 62)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All settings checks passed.")
    return 0


def test_settings():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
