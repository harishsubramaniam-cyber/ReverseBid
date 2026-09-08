from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from .. import notify
from ..audit import record
from ..db import get_db
from ..models import Approval, ApprovalStatus, Auction, AuctionStatus, User
from ..security import approver_only
from ..web import client_ip, redirect, render

router = APIRouter(prefix="/approvals")

DECISIONS = {
    "approve": (ApprovalStatus.APPROVED, AuctionStatus.SCHEDULED, "approved"),
    "reject": (ApprovalStatus.REJECTED, AuctionStatus.REJECTED, "rejected"),
    "rework": (ApprovalStatus.REWORK, AuctionStatus.REWORK, "rework"),
}


@router.get("")
def pending(request: Request, user: User = Depends(approver_only),
            db: Session = Depends(get_db)):
    rows = (db.query(Approval).filter(Approval.status == ApprovalStatus.PENDING)
              .order_by(Approval.requested_at.asc()).all())
    history = (db.query(Approval).filter(Approval.status != ApprovalStatus.PENDING)
                 .order_by(Approval.acted_at.desc()).limit(25).all())
    return render(request, "approvals.html", {"rows": rows, "history": history},
                  user=user, db=db, help_key="approvals")


@router.post("/{approval_id}")
def decide(approval_id: int, request: Request, decision: str = Form(...),
           comments: str = Form(""), user: User = Depends(approver_only),
           db: Session = Depends(get_db)):
    approval = db.get(Approval, approval_id)
    if not approval or approval.status != ApprovalStatus.PENDING:
        raise HTTPException(404, "That request has already been dealt with.")
    if decision not in DECISIONS:
        raise HTTPException(400, "Choose approve, reject or rework.")

    approval_status, auction_status, label = DECISIONS[decision]
    auction: Auction = approval.auction
    approval.status = approval_status
    approval.approver_id = user.id
    approval.comments = comments
    approval.acted_at = datetime.utcnow()
    auction.status = auction_status
    if decision == "approve":
        auction.published_at = datetime.utcnow()
    record(db, action=f"auction.{label}", entity_type="auction", entity_id=auction.id,
           actor=user, auction_id=auction.id, ip=client_ip(request),
           detail={"comments": comments})
    db.commit()

    notify.approval_decided(db, auction, label, comments)
    if decision == "approve":
        sent = notify.auction_invited(db, auction)
        return redirect("/approvals",
                        f"Approved. The auction is scheduled and {sent} bidder contact(s) "
                        "have been invited.")
    return redirect("/approvals", f"Auction sent back as “{label}”. The creator has been emailed.")
