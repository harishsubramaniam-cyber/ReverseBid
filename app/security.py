"""Password hashing, session cookies, CSRF and request-level auth helpers."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .config import BASE_URL, SECRET_KEY
from .db import get_db
from .models import Role, User

SESSION_COOKIE = "ra_session"
SESSION_MAX_AGE = 60 * 60 * 12
CSRF_COOKIE = "ra_csrf"
#: Only mark cookies "secure" when the app is actually served over HTTPS -
#: otherwise a plain-HTTP install (every Windows demo) could never sign in.
COOKIE_SECURE = BASE_URL.lower().startswith("https://")
_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="ra-session")
_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def make_session(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id, "n": secrets.token_hex(4)})


def read_session(token: str):
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, Exception):
        return None


def set_session_cookie(response, user_id: int) -> None:
    response.set_cookie(SESSION_COOKIE, make_session(user_id), httponly=True,
                        samesite="lax", secure=COOKIE_SECURE, max_age=SESSION_MAX_AGE,
                        path="/")


# ------------------------------------------------------------------ where to go next
def safe_next(target: str | None, fallback: str = "/") -> str:
    """Only ever redirect inside this app.

    ``?next=`` arrives from the sign-in link and from the 401 handler, so an
    address typed there must not be able to bounce someone to another site
    after they have signed in.
    """
    value = (target or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return fallback
    return value


# ------------------------------------------------------------------ invitations
_invite = URLSafeTimedSerializer(SECRET_KEY, salt="ra-invite")


def make_invite(email: str, role: str, vendor_id: int | None = None,
                org_id: int | None = None) -> str:
    """A signed link that lets one address set a password, once.

    Suppliers never choose which vendor they belong to: the buyer has already
    created the vendor record, and this token is what binds the new login to
    it. It carries the organisation too, so the account it creates lands in
    the right company - the same address may well hold an account with
    another one. Nothing is stored, so an unused invitation simply expires.
    """
    return _invite.dumps({"e": email.strip().lower(), "r": role, "v": vendor_id,
                          "o": org_id})


def read_invite(token: str, max_age_days: int) -> dict | None:
    try:
        data = _invite.loads(token, max_age=max_age_days * 86400)
    except (BadSignature, Exception):
        return None
    if not isinstance(data, dict) or not data.get("e") or data.get("r") not in ("vendor", "buyer"):
        return None
    if not data.get("o"):
        return None          # an invitation that names no organisation is not usable
    return data


# ------------------------------------------------------------------ CSRF
def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_token_for(request: Request) -> str:
    """The token this page should carry, reusing the one already in the browser."""
    existing = request.cookies.get(CSRF_COOKIE, "")
    return existing if len(existing) >= 20 else new_csrf_token()


def set_csrf_cookie(response, token: str) -> None:
    response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="lax",
                        secure=COOKIE_SECURE, max_age=SESSION_MAX_AGE, path="/")


_UNSAFE = ("POST", "PUT", "PATCH", "DELETE")


async def csrf_protect(request: Request) -> None:
    """Reject a form that did not come from a page this app rendered.

    The token is written as an http-only cookie when a page is rendered and
    repeated in a hidden field on every form (or the ``X-CSRF-Token`` header
    for the few things JavaScript posts). Both have to match.
    """
    if request.method not in _UNSAFE:
        return
    cookie = request.cookies.get(CSRF_COOKIE, "")
    submitted = request.headers.get("x-csrf-token", "")
    if not submitted:
        try:
            form = await request.form()
            submitted = str(form.get("csrf_token") or "")
        except Exception:      # pragma: no cover - unparseable body
            submitted = ""
    if not cookie or not submitted or not hmac.compare_digest(cookie, submitted):
        raise HTTPException(
            status_code=403,
            detail="This page had been open too long, or was opened from somewhere else, so "
                   "we did not save it. Go back, reload the page and try again.")


# ------------------------------------------------------------------ sign-in throttle
_ATTEMPT_WINDOW = 15 * 60
_MAX_ATTEMPTS = 10
#: The per-account bucket, which every computer shares.
_MAX_PER_ACCOUNT = 50
_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


def _prune(stamps: list[float], now: float) -> list[float]:
    return [t for t in stamps if now - t < _ATTEMPT_WINDOW]


def login_blocked(key: str, limit: int = _MAX_ATTEMPTS) -> int:
    """Seconds the caller must wait, or 0 when they may try again now.

    ``limit`` is deliberately looser for the per-account bucket: that one
    exists to blunt a guesser spread across many addresses, and a tight limit
    on it would let anyone lock a colleague out by typing their address and
    ten wrong passwords.
    """
    now = time.time()
    with _attempts_lock:
        stamps = _prune(_attempts.get(key, []), now)
        _attempts[key] = stamps
        if len(stamps) < limit:
            return 0
        return max(1, int(_ATTEMPT_WINDOW - (now - stamps[0])))


def note_failed_login(key: str) -> None:
    now = time.time()
    with _attempts_lock:
        _attempts[key] = _prune(_attempts.get(key, []), now) + [now]


def clear_failed_logins(key: str) -> None:
    with _attempts_lock:
        _attempts.pop(key, None)


# ------------------------------------------------------------------ who is asking
def current_user_optional(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    data = read_session(token)
    if not data:
        return None
    user = db.get(User, data.get("uid"))
    if user and user.is_active:
        return user
    return None


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="login-required")
    return user


def require_roles(*roles: Role):
    def _dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="You do not have access to this page.")
        return user
    return _dep


buyer_only = require_roles(Role.BUYER, Role.ADMIN)
buyer_side = require_roles(Role.BUYER, Role.ADMIN)
vendor_only = require_roles(Role.VENDOR)
