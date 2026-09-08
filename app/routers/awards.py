"""Line-item award, split across as many vendors as the buyer likes."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import engine, notify
from ..audit import record
from ..db import get_db
from ..models import Auction, AuctionLine, AuctionStatus, Award, Bid, User, Vendor
from ..security import buyer_only
from ..utils import fmt_money, fmt_qty
from ..web import client_ip, redirect, render

router = APIRouter(prefix="/auctions")


@router.get("/{auction_id}/award")
def award_form(auction_id: int, request: Request, user: User = Depends(buyer_only),
               db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    if auction.status not in (AuctionStatus.CLOSED, AuctionStatus.AWARDED):
        raise HTTPException(400, "You can award once bidding has closed.")
    rows = []
    for line in auction.lines:
        ranked = engine.best_per_vendor(db, line.id)
        existing = db.query(Award).filter(Award.line_id == line.id).all()
        rows.append({"line": line, "label": engine.line_label(line), "ranked": ranked,
                     "existing": existing, "best": ranked[0] if ranked else None})
    return render(request, "award.html",
                  {"auction": auction, "rows": rows,
                   "summary": engine.auction_summary(db, auction)},
                  user=user, db=db, help_key="auction_detail_buyer")


@router.post("/{auction_id}/award")
async def post_award(auction_id: int, request: Request, user: User = Depends(buyer_only),
                     db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    if auction.status not in (AuctionStatus.CLOSED, AuctionStatus.AWARDED):
        raise HTTPException(400, "You can award once bidding has closed.")

    form = await request.form()
    line_ids = form.getlist("award_line_id")
    vendor_ids = form.getlist("award_vendor_id")
    qtys = form.getlist("award_qty")
    prices = form.getlist("award_price")
    notes = form.getlist("award_note")

    db.query(Award).filter(Award.auction_id == auction.id).delete()
    db.flush()

    per_line: dict[int, float] = {}
    created: list[Award] = []
    for index, raw_line in enumerate(line_ids):
        vendor_raw = vendor_ids[index] if index < len(vendor_ids) else ""
        if not raw_line or not vendor_raw:
            continue
        line = db.get(AuctionLine, int(raw_line))
        if not line or line.auction_id != auction.id:
            continue
        try:
            qty = float(qtys[index] or 0)
            price = float(prices[index] or 0)
        except ValueError:
            raise HTTPException(400, "Award quantity and price must be numbers.")
        if qty <= 0:
            continue
        if price <= 0:
            raise HTTPException(400, "Award price must be greater than zero.")
        per_line[line.id] = per_line.get(line.id, 0) + qty
        if round(per_line[line.id], 6) > round(line.qty, 6):
            raise HTTPException(
                400, f"You have awarded more than the quantity available on "
                     f"“{engine.line_label(line)}” ({fmt_qty(line.qty)}).")
        vendor_id = int(vendor_raw)
        bid = engine.vendor_best(db, line.id, vendor_id)
        award = Award(auction_id=auction.id, line_id=line.id, vendor_id=vendor_id,
                      bid_id=bid.id if bid else None, qty=qty, unit_price=price,
                      total=round(qty * price, 2), awarded_by_id=user.id,
                      notes=(notes[index] if index < len(notes) else ""))
        db.add(award)
        created.append(award)

    if not created:
        raise HTTPException(400, "Pick at least one bidder to award to.")

    auction.status = AuctionStatus.AWARDED
    auction.awarded_at = datetime.utcnow()
    summary = engine.auction_summary(db, auction)
    record(db, action="auction.award", entity_type="auction", entity_id=auction.id, actor=user,
           auction_id=auction.id, ip=client_ip(request),
           detail={"awards": [{"line": a.line_id, "vendor": a.vendor_id, "qty": a.qty,
                               "price": a.unit_price} for a in created],
                   "savings": round(summary["savings"], 2)})
    db.commit()

    # Tell the winners what they won, and everyone else that they did not.
    by_vendor: dict[int, list[Award]] = {}
    for award in created:
        by_vendor.setdefault(award.vendor_id, []).append(award)
    for vendor_id, awards in by_vendor.items():
        vendor = db.get(Vendor, vendor_id)
        rows = [(engine.line_label(a.line), fmt_qty(a.qty), fmt_money(a.unit_price))
                for a in awards]
        notify.awarded(db, auction, vendor, rows, sum(a.total for a in awards))
    for part in auction.participants:
        if part.vendor_id not in by_vendor:
            notify.not_awarded(db, auction, part.vendor)

    return redirect(f"/auctions/{auction.id}?tab=award",
                    f"Awarded. Savings of {fmt_money(summary['savings'])} "
                    f"({summary['savings_pct']:.1f}%). All bidders have been emailed.")
