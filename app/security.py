"""Password hashing, session cookies and request-level auth helpers."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .config import SECRET_KEY
from .db import get_db
from .models import Role, User

SESSION_COOKIE = "ra_session"
SESSION_MAX_AGE = 60 * 60 * 12
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
buyer_side = require_roles(Role.BUYER, Role.ADMIN, Role.APPROVER)
vendor_only = require_roles(Role.VENDOR)
approver_only = require_roles(Role.APPROVER, Role.ADMIN)
