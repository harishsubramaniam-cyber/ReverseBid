"""Parsing and validating the address lists people type into the app.

Users type email addresses in free text - commas, semicolons, new lines,
"Name <a@b.com>" - so every entry point runs through here.
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[,;\n\r]+")
_ANGLE = re.compile(r"^.*<([^>]+)>$")
#: Pulled out before any splitting, so a display name containing a comma -
#: "Menon, Ravi <ravi@x.com>", which is exactly what Outlook copies - does not
#: get torn in half and rejected.
_ANGLE_ANY = re.compile(r"<([^<>]+)>")
#: Deliberately permissive: enough to catch typos, not a full RFC 5322 parser.
_VALID = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


class EmailError(ValueError):
    """Carries a message written for the person who typed the address."""


def parse(raw: str | None) -> list[str]:
    """Split free text into a clean, de-duplicated list of addresses."""
    if not raw:
        return []
    out: list[str] = []
    # Anything in angle brackets is an address with a display name in front of
    # it. Take those out first, so a display name containing a comma cannot be
    # split down the middle and reported as a bad address.
    found_angle = False

    def _keep(address: str) -> str:
        nonlocal found_angle
        found_angle = True
        cleaned = address.strip().strip("<>").lower()
        if cleaned and cleaned not in out:
            out.append(cleaned)
        return " "

    remainder = _ANGLE_ANY.sub(lambda m: _keep(m.group(1)), raw)
    for chunk in _SPLIT.split(remainder):
        candidate = chunk.strip().strip("<>").lower()
        if not candidate:
            continue
        # What is left beside a "Name <address>" entry is the leftover display
        # name, not something the person meant as an address.
        if found_angle and "@" not in candidate:
            continue
        if candidate not in out:
            out.append(candidate)
    return out


def validate(raw: str | None, *, field: str = "email address") -> list[str]:
    """Parse and reject anything that is obviously not an address."""
    addresses = parse(raw)
    bad = [a for a in addresses if not _VALID.match(a)]
    if bad:
        raise EmailError(
            f"“{bad[0]}” does not look like an {field}. Separate several addresses "
            "with a comma or put each on its own line.")
    return addresses


def normalise(raw: str | None) -> str:
    """The canonical form we store: one address per line."""
    return "\n".join(parse(raw))


def describe(addresses: list[str], limit: int = 3) -> str:
    """A short human summary, e.g. 'a@x.com, b@x.com and 2 more'."""
    if not addresses:
        return "—"
    if len(addresses) <= limit:
        return ", ".join(addresses)
    return f"{', '.join(addresses[:limit])} and {len(addresses) - limit} more"
