"""Private conversations between a bidder and the auction creator."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from .. import notify
from ..audit import record
from ..db import get_db
from ..models import Auction, Message, Participant, User, Vendor
from ..security import current_user
from ..web import client_ip, redirect

router = APIRouter(prefix="/auctions")


@router.post("/{auction_id}/messages")
def post_message(auction_id: int, request: Request, body: str = Form(...),
                 vendor_id: int = Form(0), user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    body = body.strip()
    if not body:
        return redirect(f"/auctions/{auction_id}#conversation", "Write a message first.",
                        kind="error")

    if user.is_vendor:
        vendor_id = user.vendor_id
        if not db.query(Participant).filter_by(auction_id=auction.id,
                                               vendor_id=vendor_id).first():
            raise HTTPException(403, "You are not a bidder on this auction.")
        recipients = [auction.creator]
    else:
        if not vendor_id:
            raise HTTPException(400, "Choose which bidder you are replying to.")
        recipients = notify.vendor_recipients(db, vendor_id, auction)

    message = Message(auction_id=auction.id, vendor_id=vendor_id, sender_id=user.id, body=body)
    db.add(message)
    record(db, action="message.post", entity_type="message", actor=user, auction_id=auction.id,
           ip=client_ip(request), detail={"vendor_id": vendor_id, "chars": len(body)})
    db.commit()
    notify.message_posted(db, auction, recipients, user, body)
    return redirect(f"/auctions/{auction_id}?tab=conversation#conversation",
                    "Message sent, and the other side has been emailed.")
