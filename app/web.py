"""Templating, flash messages and small view helpers shared by every router."""
from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import config
from .emails_util import describe as email_describe, parse as email_list
from .help_content import FIELD_HELP, PAGE_HELP
from .models import Notification, User
from .utils import (TZ_NAME, epoch, fmt_dt, fmt_money, fmt_qty, humanize_seconds, pct,
                    to_local, to_local_string)

FLASH_COOKIE = "ra_flash"
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))
templates.env.globals.update(
    app_name=config.APP_NAME, currency=config.CURRENCY_SYMBOL, tz_name=TZ_NAME,
    fmt_money=fmt_money, fmt_dt=fmt_dt, fmt_qty=fmt_qty, pct=pct,
    to_local=to_local, to_local_string=to_local_string, humanize=humanize_seconds, epoch=epoch,
    FIELD_HELP=FIELD_HELP, PAGE_HELP=PAGE_HELP, email_enabled=config.EMAIL_ENABLED,
    email_list=email_list, email_describe=email_describe,
)
templates.env.filters["money"] = fmt_money


def flash(response, message: str, kind: str = "ok") -> None:
    response.set_cookie(FLASH_COOKIE, json.dumps({"m": message, "k": kind}),
                        max_age=30, httponly=True, samesite="lax", path="/")


def redirect(url: str, message: str = "", kind: str = "ok", status: int = 303) -> RedirectResponse:
    response = RedirectResponse(url, status_code=status)
    if message:
        flash(response, message, kind)
    return response


def read_flash(request: Request):
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def render(request: Request, template: str, context: dict[str, Any] | None = None,
           *, user: User | None = None, db: Session | None = None,
           help_key: str = "", status_code: int = 200):
    ctx: dict[str, Any] = {"request": request, "user": user, "flash": read_flash(request),
                           "help_key": help_key, "page_help": PAGE_HELP.get(help_key)}
    if user and db is not None:
        ctx["unread_count"] = (db.query(Notification)
                                 .filter(Notification.user_id == user.id,
                                         Notification.read_at.is_(None)).count())
    ctx.update(context or {})
    response = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    if request.cookies.get(FLASH_COOKIE):
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
