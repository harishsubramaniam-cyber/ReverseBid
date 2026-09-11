"""Email delivery.

Two modes, chosen automatically:

* **SMTP** - when ``RA_SMTP_HOST`` is set, messages are really sent.
* **Dev outbox** - otherwise every message is written to ``data/outbox/*.eml``
  and shown in the in-app Outbox page, so the full notification flow can be
  exercised without sending anything to real people.

Either way a row is written to ``email_messages``, so the Outbox is a complete
log of what the platform sent, when, and to whom.
"""
from __future__ import annotations

import queue
import re
import smtplib
import socket
import threading
import traceback
from datetime import datetime
from email.message import EmailMessage as PyEmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from . import config
from .db import SessionLocal
from .models import EmailMessage

_queue: "queue.Queue[int]" = queue.Queue()
_worker_started = False
_lock = threading.Lock()


def _html_to_text(html: str) -> str:
    text = re.sub(r"<(br|/p|/div|/tr|/h[1-6])[^>]*>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def queue_email(db, *, to_email: str, subject: str, html_body: str, to_name: str = "",
                event: str = "", auction_id: int | None = None,
                text_body: str = "", org_id: int | None = None) -> EmailMessage:
    """Persist the message and hand it to the background sender.

    ``org_id`` is what keeps one buying organisation's Outbox to itself.
    """
    msg = EmailMessage(
        to_email=to_email, to_name=to_name, subject=subject,
        html_body=html_body, text_body=text_body or _html_to_text(html_body),
        event=event, auction_id=auction_id, org_id=org_id, status="queued",
    )
    db.add(msg)
    db.commit()
    _ensure_worker()
    _queue.put(msg.id)
    return msg


def _ensure_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, name="ra-mailer", daemon=True).start()
        _worker_started = True


def requeue_pending(retry_failed: bool = True, org_id: int | None = None) -> int:
    """Pick up anything left over from a previous run.

    The queue only ever lived in memory, so a message written just before the
    app stopped sat at "queued" for ever: never sent, never in the outbox, and
    showing red on the Outbox page with no way to try again. Called on startup
    so a restart is also the retry.
    """
    wanted = ["queued", "failed"] if retry_failed else ["queued"]
    db = SessionLocal()
    try:
        query = db.query(EmailMessage.id).filter(EmailMessage.status.in_(wanted))
        if org_id is not None:
            query = query.filter(EmailMessage.org_id == org_id)
        rows = query.order_by(EmailMessage.id.asc()).all()
    finally:
        db.close()
    if not rows:
        return 0
    _ensure_worker()
    for (msg_id,) in rows:
        _queue.put(msg_id)
    return len(rows)


def _worker() -> None:  # pragma: no cover - background thread
    while True:
        msg_id = _queue.get()
        try:
            deliver(msg_id)
        except Exception:
            traceback.print_exc()
        finally:
            _queue.task_done()


def deliver(msg_id: int) -> str:
    """Deliver one queued message. Returns the resulting status.

    Whatever happens, the row must not be left saying "queued": to the buyer a
    message stuck there looks exactly like one still on its way, so a mail
    server that could not be reached - or a database that would not take the
    update - simply looked like nothing happening at all. Every failure is now
    recorded against the message, in words, where the Outbox shows it.
    """
    db = SessionLocal()
    try:
        msg = db.get(EmailMessage, msg_id)
        if not msg or msg.status in ("sent", "outbox"):
            return msg.status if msg else "missing"
        msg.error = ""
        try:
            mime = _build_mime(msg)
            if config.EMAIL_ENABLED:
                try:
                    _smtp_send(mime, msg.to_email)
                    msg.status, msg.sent_at, msg.error = "sent", datetime.utcnow(), ""
                except Exception as exc:
                    msg.status, msg.error = "failed", explain(exc)
            else:
                path = Path(config.OUTBOX_DIR) / f"{msg.id:06d}-{_slug(msg.subject)}.eml"
                path.write_bytes(bytes(mime))
                msg.status, msg.sent_at, msg.file_path = "outbox", datetime.utcnow(), str(path)
            db.commit()
        except Exception as exc:
            # Building the message, or writing the row itself, went wrong.
            db.rollback()
            try:
                msg = db.get(EmailMessage, msg_id)
                msg.status, msg.error = "failed", explain(exc)
                db.commit()
            except Exception:            # pragma: no cover - database unavailable
                traceback.print_exc()
        return msg.status
    finally:
        db.close()


def explain(exc: Exception) -> str:
    """The failure in words a buyer can act on, with the raw detail kept.

    ``SMTPAuthenticationError(535, b'5.7.8 Username and Password not accepted')``
    tells a procurement manager nothing. "Gmail would not accept the sign-in -
    use a 16-character App password, not your normal password" tells them what
    to change.
    """
    name = type(exc).__name__
    raw = str(exc)
    low = raw.lower()
    hint = ""
    if isinstance(exc, smtplib.SMTPAuthenticationError) or "authentication" in low \
            or "username and password not accepted" in low or "5.7.8" in raw:
        hint = ("The mail server would not accept the username and password. On Gmail this "
                "must be a 16-character App password, not your normal password, and "
                "RA_SMTP_USER must be the full address.")
    elif isinstance(exc, socket.timeout) or name == "TimeoutError" or "timed out" in low:
        hint = (f"Nothing answered at {config.SMTP_HOST}:{config.SMTP_PORT} within 30 seconds. "
                "That port is usually blocked by an office or campus network or a firewall - "
                "try port 465 with RA_SMTP_SSL=1, or a different network.")
    elif name in ("gaierror", "herror") or "name or service not known" in low \
            or "getaddrinfo" in low:
        hint = (f"The address “{config.SMTP_HOST}” could not be found. Check RA_SMTP_HOST for a "
                "typo - Gmail is smtp.gmail.com.")
    elif isinstance(exc, ConnectionRefusedError) or "refused" in low:
        hint = (f"{config.SMTP_HOST} refused the connection on port {config.SMTP_PORT}. Check "
                "the port: 587 with RA_SMTP_STARTTLS=1, or 465 with RA_SMTP_SSL=1.")
    elif "ssl" in low or "wrong version number" in low:
        hint = ("The encryption settings do not match the port. Use 587 with "
                "RA_SMTP_STARTTLS=1 and RA_SMTP_SSL=0, or 465 with RA_SMTP_SSL=1.")
    elif isinstance(exc, smtplib.SMTPRecipientsRefused) or "recipient" in low:
        hint = "The mail server would not accept that recipient address."
    elif isinstance(exc, smtplib.SMTPSenderRefused) or "sender" in low:
        hint = ("The mail server would not accept the From address. RA_MAIL_FROM usually has to "
                "be the same address as RA_SMTP_USER.")
    return f"{hint} [{name}: {raw}]" if hint else f"{name}: {raw}"


def send_test(to_email: str) -> tuple[bool, str]:
    """Send one message right now and say plainly how it went.

    Deliberately synchronous: the whole point is to hand back the mail
    server's own answer while the person is still looking at the screen,
    instead of a row that says "queued" and settles a minute later.
    """
    body = (f"<p>This is a test message from {config.APP_NAME}.</p>"
            "<p>If you are reading it in your inbox, sending works — your auction "
            "invitations, outbid alerts and award letters will go out the same way.</p>")
    mime = PyEmailMessage()
    mime["Subject"] = f"{config.APP_NAME} test email"
    mime["From"] = formataddr((config.MAIL_FROM_NAME, config.MAIL_FROM))
    mime["To"] = to_email
    mime["Date"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    mime["Message-ID"] = make_msgid(domain="reversebid.local")
    mime.set_content(_html_to_text(body))
    mime.add_alternative(body, subtype="html")

    if not config.EMAIL_ENABLED:
        path = Path(config.OUTBOX_DIR) / "test-message.eml"
        path.write_bytes(bytes(mime))
        return False, ("No mail server is set, so nothing was sent — the test message was "
                       "saved to data/outbox/test-message.eml instead. Put your settings in "
                       "the .env file and restart to send for real.")
    try:
        _smtp_send(mime, to_email)
    except Exception as exc:
        return False, f"{config.SMTP_HOST} did not accept the message. {explain(exc)}"
    return True, (f"Sent to {to_email} through {config.SMTP_HOST}. If it is not in the inbox "
                  "within a minute, look in the spam folder.")


def settings_summary() -> dict:
    """What the app is actually using, for the Outbox page. Never the password."""
    return {
        "enabled": config.EMAIL_ENABLED,
        "host": config.SMTP_HOST,
        "port": config.SMTP_PORT,
        "user": config.SMTP_USER,
        "from": config.MAIL_FROM,
        "security": ("SSL (implicit)" if config.SMTP_SSL
                     else ("STARTTLS" if config.SMTP_STARTTLS else "none")),
        "password_set": bool(config.SMTP_PASSWORD),
        "env_file": str(config.BASE_DIR / ".env"),
        "env_file_found": (config.BASE_DIR / ".env").is_file(),
    }


def flush(timeout: float = 30.0) -> None:
    """Block until the queue drains - used by tests and CLI scripts."""
    _ensure_worker()
    done = threading.Event()

    def _wait():
        _queue.join()
        done.set()

    threading.Thread(target=_wait, daemon=True).start()
    done.wait(timeout)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "message"


def _build_mime(msg: EmailMessage) -> PyEmailMessage:
    mime = PyEmailMessage()
    mime["Subject"] = msg.subject
    mime["From"] = formataddr((config.MAIL_FROM_NAME, config.MAIL_FROM))
    mime["To"] = formataddr((msg.to_name, msg.to_email)) if msg.to_name else msg.to_email
    mime["Date"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    mime["Message-ID"] = make_msgid(domain="reversebid.local")
    if msg.event:
        mime["X-RA-Event"] = msg.event
    mime.set_content(msg.text_body or _html_to_text(msg.html_body))
    mime.add_alternative(msg.html_body, subtype="html")
    return mime


def _smtp_send(mime: PyEmailMessage, to_email: str) -> None:  # pragma: no cover - network
    if config.SMTP_SSL:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    try:
        server.ehlo()
        if config.SMTP_STARTTLS and not config.SMTP_SSL:
            server.starttls()
            server.ehlo()
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(mime, from_addr=config.MAIL_FROM, to_addrs=[to_email])
    finally:
        try:
            server.quit()
        except Exception:
            pass
