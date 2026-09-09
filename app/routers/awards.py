"""Awarding.

House rule: **one bidder per item.** A line is won outright by whoever the
buyer picks — normally L1 — for the whole quantity. Different lines may go to
different bidders, or every line to the same one, but a single line is never
carved up between two suppliers.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import engine, notify
from ..audit import record
from ..db import get_db
from ..errors import ActionError
from ..models import Auction, AuctionStatus, Award, User, Vendor
from ..security import buyer_only
from ..utils import fmt_money, fmt_qty
from ..web import client_ip, redirect, render

router = APIRouter(prefix="/auctions")


def _awardable(db: Session, auction_id: int) -> Auction:
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    if auction.status not in (AuctionStatus.CLOSED, AuctionStatus.AWARDED):
        raise ActionError("You can award once bidding has closed. Use “Close bidding now” if "
                          "you want to finish early.")
    return auction


@router.get("/{auction_id}/award")
def award_form(auction_id: int, request: Request, user: User = Depends(buyer_only),
               db: Session = Depends(get_db)):
    try:
        auction = _awardable(db, auction_id)
    except ActionError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")

    rows = []
    for line in auction.lines:
        ranked = engine.best_per_vendor(db, line.id)
        existing = db.query(Award).filter(Award.line_id == line.id).first()
        rows.append({
            "line": line, "label": engine.line_label(line), "ranked": ranked,
            "baseline": engine.line_baseline(db, line),
            "existing": existing, "best": ranked[0] if ranked else None,
            "chosen": existing.vendor_id if existing else (ranked[0].vendor_id if ranked else None),
        })
    bidders = sorted({(bid.vendor_id, bid.vendor.name)
                      for row in rows for bid in row["ranked"]}, key=lambda pair: pair[1])
    return render(request, "award.html",
                  {"auction": auction, "rows": rows, "bidders": bidders,
                   "summary": engine.auction_summary(db, auction)},
                  user=user, db=db, help_key="auction_detail_buyer")


@router.post("/{auction_id}/award")
async def post_award(auction_id: int, request: Request, user: User = Depends(buyer_only),
                     db: Session = Depends(get_db)):
    try:
        auction = _awardable(db, auction_id)
    except ActionError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")

    form = await request.form()
    try:
        created: list[Award] = []
        db.query(Award).filter(Award.auction_id == auction.id).delete()
        db.flush()

        for line in auction.lines:
            raw_vendor = (form.get(f"winner_{line.id}") or "").strip()
            if not raw_vendor:
                continue                      # this line is deliberately left unawarded
            label = engine.line_label(line)
            vendor = db.get(Vendor, int(raw_vendor))
            if not vendor:
                raise ActionError(f"The bidder chosen for “{label}” no longer exists.")
            bid = engine.vendor_best(db, line.id, vendor.id)

            raw_price = (form.get(f"price_{line.id}") or "").strip()
            if raw_price:
                try:
                    price = float(raw_price)
                except ValueError:
                    raise ActionError(f"On “{label}”, “{raw_price}” is not a price.")
            elif bid:
                price = bid.unit_price
            else:
                raise ActionError(f"{vendor.name} did not bid on “{label}”, so there is no price "
                                  "to award at. Type one in, or leave that item unawarded.")
            if price <= 0:
                raise ActionError(f"On “{label}”, the award price has to be more than zero.")

            award = Award(auction_id=auction.id, line_id=line.id, vendor_id=vendor.id,
                          bid_id=bid.id if bid else None, qty=line.qty, unit_price=price,
                          total=round(line.qty * price, 2), awarded_by_id=user.id,
                          notes=(form.get(f"note_{line.id}") or "")[:500])
            db.add(award)
            created.append(award)

        if not created:
            raise ActionError("Nothing was awarded — choose a winning bidder on at least one item.")

        auction.status = AuctionStatus.AWARDED
        auction.awarded_at = datetime.utcnow()
        # Flush first: the session does not autoflush, so without this the
        # summary would still be reading the *old* awards and quote the wrong
        # savings in the confirmation and in the emails.
        db.flush()
        summary = engine.auction_summary(db, auction)
        record(db, action="auction.award", entity_type="auction", entity_id=auction.id,
               actor=user, auction_id=auction.id, ip=client_ip(request),
               detail={"awards": [{"line": a.line_id, "vendor": a.vendor_id, "qty": a.qty,
                                   "price": a.unit_price} for a in created],
                       "savings": round(summary["savings"], 2)})
        db.commit()
    except ActionError as exc:
        db.rollback()
        return redirect(f"/auctions/{auction_id}/award", str(exc), kind="error")

    # Winners hear what they won; everyone else hears the outcome too.
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

    notify.award_summary(
        db, auction,
        [(engine.line_label(a.line), a.vendor.name,
          f"{fmt_qty(a.qty)} @ {fmt_money(a.unit_price)}") for a in created],
        total=sum(a.total for a in created), savings=summary["savings"],
        savings_pct=summary["savings_pct"])

    return redirect(f"/auctions/{auction.id}?tab=award",
                    f"Awarded to {len(by_vendor)} bidder(s). Savings of "
                    f"{fmt_money(summary['savings'])} ({summary['savings_pct']:.1f}%). "
                    "Everyone has been emailed the outcome.")
