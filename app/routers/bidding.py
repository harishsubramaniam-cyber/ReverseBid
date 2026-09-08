from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..engine import BidError, place_bid, withdraw_bid
from ..models import Auction, AuctionLine, Bid, User
from ..security import current_user, vendor_only
from ..web import client_ip, redirect

router = APIRouter(prefix="/auctions")


@router.post("/{auction_id}/bid")
def post_bid(auction_id: int, request: Request, line_id: int = Form(...),
             unit_price: float = Form(...), note: str = Form(""),
             user: User = Depends(vendor_only), db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    line = db.get(AuctionLine, line_id)
    if not auction or not line or line.auction_id != auction.id:
        raise HTTPException(404, "That item is not part of this auction.")
    try:
        bid = place_bid(db, auction, line, user, unit_price, note, ip=client_ip(request))
    except BidError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    from ..engine import vendor_rank
    rank = vendor_rank(db, line.id, user.vendor_id)
    good = "You are now L1 — the lowest bid." if rank == 1 else f"Bid placed. You are at L{rank}."
    return redirect(f"/auctions/{auction_id}", f"{good} We emailed you a confirmation.")


@router.post("/{auction_id}/bids/{bid_id}/withdraw")
def post_withdraw(auction_id: int, bid_id: int, request: Request, reason: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    bid = db.get(Bid, bid_id)
    if not bid or bid.auction_id != auction_id:
        raise HTTPException(404, "That bid does not exist.")
    try:
        withdraw_bid(db, bid, user, reason, ip=client_ip(request))
    except BidError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    return redirect(f"/auctions/{auction_id}",
                    "Bid withdrawn. Ranks have been recalculated and the buyer told.")
