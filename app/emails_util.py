"""Parsing and validating the address lists people type into the app.

Users type email addresses in free text - commas, semicolons, new lines,
"Name <a@b.com>" - so every entry point runs through here.
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[,;\n\r]+")
_ANGLE = re.compile(r"^.*<([^>]+)>$")
#: Deliberately permissive: enough to catch typos, not a full RFC 5322 parser.
_VALID = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


class EmailError(ValueError):
    """Carries a message written for the person who typed the address."""


def parse(raw: str | None) -> list[str]:
    """Split free text into a clean, de-duplicated list of addresses."""
    if not raw:
        return []
    out: list[str] = []
    for chunk in _SPLIT.split(raw):
        candidate = chunk.strip()
        if not candidate:
            continue
        match = _ANGLE.match(candidate)
        if match:
            candidate = match.group(1).strip()
        candidate = candidate.strip("<>").lower()
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
