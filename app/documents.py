"""Documents attached to an auction: drawings, specs, terms, compliance sheets.

Files live on disk under ``data/attachments/<auction id>/`` and the database
holds a row describing each one. Two rules run through this module:

* **Only file types a procurement person actually sends.** Anything that a
  browser might execute - HTML, SVG, scripts - is refused outright, because an
  uploaded file is served from the same address as the app itself.
* **A supplier's paperwork is for the buyer alone.** Every download is checked
  against who is asking, never against a guessable id.
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from . import config
from .errors import ActionError

#: extension -> content type. Anything not on this list is refused.
ALLOWED: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".dwg": "image/vnd.dwg",
    ".dxf": "image/vnd.dxf",
    ".step": "application/octet-stream",
    ".stp": "application/octet-stream",
    ".igs": "application/octet-stream",
    ".zip": "application/zip",
}

#: Shown to a person when their file is refused.
ALLOWED_LABEL = "PDF, images, Word, Excel, PowerPoint, CSV, text, DWG/DXF/STEP or ZIP"

#: Only these are ever handed back with a content type a browser will render.
#: Everything else downloads, so nothing uploaded here can run as a page.
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif"}

_SAFE = re.compile(r"[^A-Za-z0-9._ ()-]+")


def storage_dir(auction_id: int) -> Path:
    path = Path(config.DATA_DIR) / "attachments" / str(auction_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def clean_name(raw: str) -> str:
    """A display name safe to put in a header and show on a page."""
    name = Path((raw or "").replace("\\", "/")).name
    name = _SAFE.sub("_", name).strip(" ._") or "document"
    return name[:200]


def check_type(filename: str) -> tuple[str, str]:
    """Returns (extension, content type), or refuses in plain words."""
    suffix = Path(clean_name(filename)).suffix.lower()
    if not suffix:
        raise ActionError(
            f"“{clean_name(filename)}” has no file extension, so we cannot tell what it is. "
            f"Attach a {ALLOWED_LABEL} file.")
    if suffix not in ALLOWED:
        raise ActionError(
            f"We do not accept {suffix} files. Attach a {ALLOWED_LABEL} file instead.")
    return suffix, ALLOWED[suffix]


def save(auction_id: int, filename: str, data: bytes) -> tuple[str, str, str, int]:
    """Write one upload to disk.

    Returns (display name, name on disk, content type, size in bytes).
    """
    display = clean_name(filename)
    suffix, content_type = check_type(display)
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    if not data:
        raise ActionError(f"“{display}” is empty, so there is nothing to attach.")
    if len(data) > limit:
        raise ActionError(f"“{display}” is {len(data) / 1024 / 1024:.1f} MB. The limit is "
                          f"{config.MAX_UPLOAD_MB} MB per file — try compressing it, or send "
                          "it as a ZIP.")
    stored = f"{secrets.token_hex(8)}{suffix}"
    (storage_dir(auction_id) / stored).write_bytes(data)
    return display, stored, content_type, len(data)


def path_for(auction_id: int, stored_name: str) -> Path:
    """Where one document lives, with no way to point outside its own folder."""
    safe = Path(stored_name).name
    return storage_dir(auction_id) / safe


def delete_file(auction_id: int, stored_name: str) -> None:
    try:
        path_for(auction_id, stored_name).unlink(missing_ok=True)
    except OSError:            # pragma: no cover - locked or already gone
        pass


def download_headers(filename: str, content_type: str) -> dict[str, str]:
    """Headers that make a download safe on the app's own address."""
    # A quoted ASCII name for old browsers, plus the real name for the rest.
    fallback = clean_name(filename).encode("ascii", "replace").decode("ascii")
    disposition = "inline" if content_type in INLINE_TYPES else "attachment"
    return {
        "Content-Disposition": f'{disposition}; filename="{fallback}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
