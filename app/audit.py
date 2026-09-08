"""Append-only audit trail. Every state-changing action calls record()."""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from .models import AuditLog, User


def record(db: Session, *, action: str, entity_type: str, entity_id: Optional[int] = None,
           actor: Optional[User] = None, auction_id: Optional[int] = None,
           detail: Any = None, ip: str = "", commit: bool = False) -> AuditLog:
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, default=str, ensure_ascii=False)
    log = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        auction_id=auction_id,
        actor_id=actor.id if actor else None,
        actor_label=(f"{actor.name} ({actor.email})" if actor else "system"),
        detail=detail or "",
        ip=ip,
    )
    db.add(log)
    if commit:
        db.commit()
    return log


def for_auction(db: Session, auction_id: int, limit: int = 500):
    return (db.query(AuditLog)
              .filter(AuditLog.auction_id == auction_id)
              .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
              .limit(limit).all())
