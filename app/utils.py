"""Small shared helpers: money/date formatting and timezone handling."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config

TZ_NAME = os.getenv("RA_TIMEZONE", "Asia/Kolkata")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # pragma: no cover
    TZ = timezone.utc


def to_local(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ)


def from_local_string(value: str) -> datetime:
    """Parse an ``<input type=datetime-local>`` value into naive UTC."""
    dt = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    return dt.replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)


def to_local_string(dt: datetime | None) -> str:
    local = to_local(dt)
    return local.strftime("%Y-%m-%dT%H:%M") if local else ""


def epoch(dt: datetime | None) -> int:
    """Unix timestamp, for the front-end countdown clocks."""
    if not dt:
        return 0
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def fmt_dt(dt: datetime | None, with_tz: bool = True) -> str:
    local = to_local(dt)
    if not local:
        return "—"
    return local.strftime("%d %b %Y, %I:%M %p") + (f" {local.tzname()}" if with_tz else "")


def fmt_money(value: float | None, symbol: bool = True) -> str:
    if value is None:
        return "—"
    prefix = f"{config.CURRENCY_SYMBOL} " if symbol else ""
    return f"{prefix}{value:,.2f}"


def fmt_qty(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def humanize_seconds(total: int) -> str:
    if total <= 0:
        return "0s"
    parts, units = [], (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
    for label, size in units:
        if total >= size:
            parts.append(f"{total // size}{label}")
            total %= size
    return " ".join(parts[:2])


def alias_for(index: int) -> str:
    """Bidder A, Bidder B ... used when bidder names are hidden."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return f"Bidder {letters}"
