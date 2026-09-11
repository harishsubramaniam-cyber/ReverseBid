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


def first_name(full: str | None) -> str:
    """What to call someone in a greeting.

    Plain ``name.split()[0]`` greets "R Venkatesh" as "R", and writing the
    initial first is completely ordinary - K Sharma, M S Dhoni, A. Kumar. So
    skip over leading initials and use the first real word; if the whole name
    is initials, use it as it was typed.
    """
    parts = (full or "").split()
    for part in parts:
        bare = part.replace(".", "")
        if len(bare) > 1:
            return part.strip(".,")
    return " ".join(parts) or "there"


def plain_money(value) -> str:
    """A number for an <input type=number>: no symbol, no separators, no
    trailing ".0". The award screen showed "60700.0" in the box beside
    "₹ 60,700.00" everywhere else."""
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.2f}".rstrip("0").rstrip(".")
