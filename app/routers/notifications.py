from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import EmailMessage, Notification, User
from ..security import buyer_side, current_user
from ..web import redirect, render

router = APIRouter()


@router.get("/notifications")
def inbox(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = (db.query(Notification).filter(Notification.user_id == user.id)
              .order_by(Notification.created_at.desc()).limit(100).all())
    return render(request, "notifications.html", {"rows": rows}, user=user, db=db)


@router.post("/notifications/read-all")
def read_all(user: User = Depends(current_user), db: Session = Depends(get_db)):
    (db.query(Notification).filter(Notification.user_id == user.id,
                                   Notification.read_at.is_(None))
       .update({"read_at": datetime.utcnow()}))
    db.commit()
    return redirect("/notifications", "All caught up.")


@router.get("/outbox")
def outbox(request: Request, q: str = "", user: User = Depends(buyer_side),
           db: Session = Depends(get_db)):
    query = db.query(EmailMessage)
    if q:
        like = f"%{q}%"
        query = query.filter(EmailMessage.subject.ilike(like) |
                             EmailMessage.to_email.ilike(like))
    rows = query.order_by(EmailMessage.created_at.desc()).limit(200).all()
    return render(request, "outbox.html", {"rows": rows, "q": q}, user=user, db=db,
                  help_key="outbox")


@router.get("/outbox/{message_id}")
def outbox_detail(message_id: int, request: Request, user: User = Depends(buyer_side),
                  db: Session = Depends(get_db)):
    message = db.get(EmailMessage, message_id)
    if not message:
        raise HTTPException(404, "That email is not in the outbox.")
    return render(request, "outbox_detail.html", {"m": message}, user=user, db=db,
                  help_key="outbox")


@router.get("/outbox/{message_id}/raw", response_class=HTMLResponse)
def outbox_raw(message_id: int, user: User = Depends(buyer_side),
               db: Session = Depends(get_db)):
    """The message as the recipient would see it, for the preview frame.

    Served as text/plain, this showed the buyer a screen of HTML source rather
    than the email - the whole point of the Outbox is seeing what went out.
    """
    message = db.get(EmailMessage, message_id)
    if not message:
        raise HTTPException(404, "That email is not in the outbox.")
    return HTMLResponse(message.html_body)
