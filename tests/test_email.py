"""Email: what happens when the mail server is wrong, missing or slow.

This is the part of the app that fails on somebody else's network, so it gets
its own suite. It stands up a real SMTP server on a spare port and points the
app at it, then breaks it in every way a person's setup is actually broken:
wrong password, wrong port, a host that does not exist, a host that never
answers. In every case the message must end up saying *why*, on the screen,
and never sit at "queued" for ever.

    python tests/test_email.py
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import warnings
from pathlib import Path

# aiosmtpd shouts about running without TLS; on a loopback test server that is
# exactly what we want, and the noise hides the results.
warnings.filterwarnings("ignore", module="aiosmtpd.*")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-email-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient           # noqa: E402

from app import config, mailer                      # noqa: E402
from app.db import Base, SessionLocal, engine       # noqa: E402
from app.main import app                            # noqa: E402
from app.models import EmailMessage, Role, User     # noqa: E402
from app.security import hash_password              # noqa: E402

Base.metadata.create_all(bind=engine)
PW = "test1234"
FAILS: list[str] = []


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


class Client(TestClient):
    def post(self, url, **kwargs):
        token = self.cookies.get("ra_csrf")
        if token:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", token)
            kwargs["headers"] = headers
        return super().post(url, **kwargs)


def flash_of(response) -> str:
    import json
    from http.cookies import SimpleCookie
    header = response.headers.get("set-cookie", "")
    if "ra_flash" not in header:
        return ""
    jar = SimpleCookie()
    jar.load(header)
    try:
        return json.loads(jar["ra_flash"].value)["m"]
    except Exception:
        return ""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- a real server
class Inbox:
    """A tiny SMTP server, so "did it really send" is a fact, not a mock."""

    def __init__(self, *, require_login: str | None = None):
        self.messages: list[tuple[str, list[str], bytes]] = []
        self.require_login = require_login
        self.port = free_port()
        self._controller = None

    def start(self) -> bool:
        try:
            from aiosmtpd.controller import Controller
        except ImportError:
            return False

        inbox = self

        class Handler:
            async def handle_DATA(self, server, session, envelope):
                inbox.messages.append((envelope.mail_from, list(envelope.rcpt_tos),
                                       envelope.content))
                return "250 Message accepted"

        auth = None
        if self.require_login:
            from aiosmtpd.smtp import AuthResult

            def authenticator(server, session, envelope, mechanism, auth_data):
                password = getattr(auth_data, "password", b"") or b""
                if password.decode() == inbox.require_login:
                    return AuthResult(success=True)
                return AuthResult(success=False, handled=False)
            auth = authenticator

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self._controller = Controller(
                    Handler(), hostname="127.0.0.1", port=self.port,
                    authenticator=auth, auth_require_tls=False,
                    auth_required=bool(self.require_login))
                self._controller.start()
        except Exception:
            return False
        return True

    def stop(self):
        if self._controller:
            try:
                self._controller.stop()
            except Exception:
                pass


def point_at(host: str, port: int, *, user: str = "", password: str = "",
             starttls: bool = False, ssl: bool = False):
    """Repoint the running app's mail settings, as a restart with a new .env would."""
    config.SMTP_HOST = host
    config.SMTP_PORT = port
    config.SMTP_USER = user
    config.SMTP_PASSWORD = password
    config.SMTP_STARTTLS = starttls
    config.SMTP_SSL = ssl
    config.EMAIL_ENABLED = bool(host)


def send_one(subject="Test") -> EmailMessage:
    db = SessionLocal()
    try:
        msg = mailer.queue_email(db, to_email="someone@example.com", subject=subject,
                                 html_body="<p>Hello</p>", event="test")
        msg_id = msg.id
    finally:
        db.close()
    mailer.flush(timeout=90)
    db = SessionLocal()
    try:
        return db.get(EmailMessage, msg_id)
    finally:
        db.close()


def main() -> int:                                            # noqa: C901
    db = SessionLocal()
    db.add(User(name="Buyer", email="buyer@m.local", role=Role.BUYER,
                password_hash=hash_password(PW)))
    db.commit()
    db.close()
    client = Client(app, base_url="http://test")
    client.get("/login")
    client.post("/login", data={"email": "buyer@m.local", "password": PW, "next": "/"},
                follow_redirects=False)
    client.headers.update({"accept": "text/html,application/xhtml+xml"})

    # ------------------------------------------------------------------ A
    print("\nA. With no mail server at all")
    point_at("", 587)
    row = send_one("Practice")
    check("the message is saved, not sent", row.status == "outbox", row.status)
    check("...and the .eml file is really there", Path(row.file_path).is_file(), row.file_path)
    page = client.get("/outbox").text
    check("the Outbox says plainly that nothing is being sent",
          "nothing is actually being sent" in page.lower())
    check("...and points at the file to create", ".env" in page)
    r = client.post("/outbox/test", data={"to_email": "me@example.com"}, follow_redirects=False)
    msg = flash_of(r)
    check("a test email explains there is no mail server", "No mail server is set" in msg,
          msg[:60])
    check("...and writes the test message out anyway",
          (Path(config.OUTBOX_DIR) / "test-message.eml").is_file())

    # ------------------------------------------------------------------ B
    print("\nB. A mail server that cannot be reached")
    point_at("no-such-host.invalid", 587)
    row = send_one("Bad host")
    check("a host that does not exist fails rather than hanging", row.status == "failed",
          row.status)
    check("...and says the address could not be found", "could not be found" in row.error,
          row.error[:70])

    point_at("127.0.0.1", free_port())
    row = send_one("Refused")
    check("a closed port fails", row.status == "failed", row.status)
    check("...and names the port", str(config.SMTP_PORT) in row.error, row.error[:80])

    page = client.get("/outbox").text
    check("the reason is printed on the Outbox page", "could not be found" in page)
    check("the failures offer a retry", "Try them again" in page)

    # ------------------------------------------------------------------ C
    print("\nC. A mail server that is really there")
    inbox = Inbox()
    if not inbox.start():
        print("  – aiosmtpd is not installed, so the live-server checks are skipped.")
        print("    pip install aiosmtpd")
    else:
        point_at("127.0.0.1", inbox.port)
        row = send_one("Real")
        check("the message is sent", row.status == "sent", f"{row.status} {row.error[:50]}")
        check("...and really arrived", len(inbox.messages) == 1, f"{len(inbox.messages)}")
        check("...addressed to the right person",
              inbox.messages[0][1] == ["someone@example.com"], str(inbox.messages[0][1]))
        check("...with the subject on it", b"Real" in inbox.messages[0][2])

        r = client.post("/outbox/test", data={"to_email": "check@example.com"},
                        follow_redirects=False)
        msg = flash_of(r)
        check("the test email is sent too", len(inbox.messages) == 2, msg[:60])
        check("...and says so in plain words", "Sent to check@example.com" in msg, msg[:60])

        before = len(inbox.messages)
        r = client.post("/outbox/retry", follow_redirects=False)
        mailer.flush(timeout=60)
        db = SessionLocal()
        stuck = db.query(EmailMessage).filter(EmailMessage.status.in_(["queued", "failed"])).count()
        db.close()
        check("retrying sends everything that had failed", stuck == 0, f"{stuck} left")
        check("...which really means more messages arrived", len(inbox.messages) > before,
              f"{before} → {len(inbox.messages)}")
        inbox.stop()

    # ------------------------------------------------------------------ D
    print("\nD. The wrong password")
    auth_inbox = Inbox(require_login="the-right-password")
    if not auth_inbox.start():
        print("  – aiosmtpd is not installed, so the sign-in checks are skipped.")
    else:
        point_at("127.0.0.1", auth_inbox.port, user="me@example.com", password="wrong")
        row = send_one("Bad password")
        check("a refused sign-in fails", row.status == "failed", row.status)
        check("...and says to use an App password", "App password" in row.error, row.error[:90])
        r = client.post("/outbox/test", follow_redirects=False)
        check("the test button reports it the same way", "App password" in flash_of(r),
              flash_of(r)[:80])

        point_at("127.0.0.1", auth_inbox.port, user="me@example.com",
                 password="the-right-password")
        row = send_one("Good password")
        check("the right password gets through", row.status == "sent",
              f"{row.status} {row.error[:60]}")
        auth_inbox.stop()

    # ------------------------------------------------------------------ E
    print("\nE. Nothing is ever left in limbo")
    point_at("", 587)
    db = SessionLocal()
    orphan = EmailMessage(to_email="x@example.com", subject="Left over", html_body="<p>x</p>",
                          text_body="x", status="queued")
    db.add(orphan)
    db.commit()
    orphan_id = orphan.id
    db.close()
    count = mailer.requeue_pending()
    mailer.flush(timeout=60)
    db = SessionLocal()
    row = db.get(EmailMessage, orphan_id)
    left = db.query(EmailMessage).filter(EmailMessage.status == "queued").count()
    db.close()
    check("a message left over from a previous run is picked up", count >= 1, f"{count}")
    check("...and dealt with", row.status != "queued", row.status)
    check("nothing at all is left saying queued", left == 0, f"{left} stuck")

    # The status panel must describe the settings actually in force, so that
    # "why is nothing sending" can be answered by looking at one screen.
    point_at("smtp.example.com", 465, user="me@example.com", password="secret", ssl=True)
    page = client.get("/outbox").text
    check("the Outbox shows the server in use", "smtp.example.com" in page)
    check("...the port", ">465<" in page or "465" in page)
    check("...the sign-in name", "me@example.com" in page)
    check("...and never the password", "secret" not in page)

    print("\n" + "-" * 62)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All email checks passed.")
    return 0


def test_email():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
