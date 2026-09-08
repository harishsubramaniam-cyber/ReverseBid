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
                text_body: str = "") -> EmailMessage:
    """Persist the message and hand it to the background sender."""
    msg = EmailMessage(
        to_email=to_email, to_name=to_name, subject=subject,
        html_body=html_body, text_body=text_body or _html_to_text(html_body),
        event=event, auction_id=auction_id, status="queued",
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
    """Deliver one queued message. Returns the resulting status."""
    db = SessionLocal()
    try:
        msg = db.get(EmailMessage, msg_id)
        if not msg or msg.status in ("sent", "outbox"):
            return msg.status if msg else "missing"
        mime = _build_mime(msg)
        if config.EMAIL_ENABLED:
            try:
                _smtp_send(mime, msg.to_email)
                msg.status, msg.sent_at, msg.error = "sent", datetime.utcnow(), ""
            except Exception as exc:
                msg.status, msg.error = "failed", f"{type(exc).__name__}: {exc}"
        else:
            path = Path(config.OUTBOX_DIR) / f"{msg.id:06d}-{_slug(msg.subject)}.eml"
            path.write_bytes(bytes(mime))
            msg.status, msg.sent_at, msg.file_path = "outbox", datetime.utcnow(), str(path)
        db.commit()
        return msg.status
    finally:
        db.close()


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
