"""Awarding.

House rule: **one bidder per item.** A line is won outright by whoever the
buyer picks — normally L1 — for the whole quantity. Different lines may go to
different bidders, or every line to the same one, but a single line is never
carved up between two suppliers.
"""
from __future__ import annotations

import math
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import engine, notify
from ..audit import record
from ..db import get_db
from ..errors import ActionError
from ..models import Auction, AuctionStatus, Award, Participant, User, Vendor
from ..security import buyer_only
from ..utils import fmt_money, fmt_qty
from ..web import client_ip, redirect, render

router = APIRouter(prefix="/auctions")


def _safe_auction(db: Session, auction_id: int, user: User) -> Auction | None:
    """The auction, but only if it is this organisation's."""
    auction = db.get(Auction, auction_id)
    return auction if auction is not None and auction.org_id == user.org_id else None


def _awardable(db: Session, auction_id: int, user: User) -> Auction:
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist.")
    if auction.status not in (AuctionStatus.CLOSED, AuctionStatus.AWARDED):
        raise ActionError("You can award once bidding has closed. Use “Close bidding now” if "
                          "you want to finish early.")
    return auction


def _award_screen(request: Request, db: Session, user: User, auction,
                  *, error: str = "", form=None):
    """The award screen. ``form`` is what was just submitted, so a rejected
    award comes back with every winner, price and note still on the page -
    losing a whole auction's negotiated prices over one typo was brutal."""
    rows = []
    for line in auction.lines:
        ranked = engine.best_per_vendor(db, line.id)
        existing = db.query(Award).filter(Award.line_id == line.id).first()
        chosen = existing.vendor_id if existing else (ranked[0].vendor_id if ranked else None)
        price = existing.unit_price if existing else (ranked[0].unit_price if ranked else "")
        note = existing.notes if existing else ""
        if form is not None:
            raw_choice = (form.get(f"winner_{line.id}") or "").strip()
            chosen = int(raw_choice) if raw_choice.isdigit() else None
            price = (form.get(f"price_{line.id}") or "").strip()
            note = (form.get(f"note_{line.id}") or "").strip()
        # Every money column on this screen has to be on the basis the auction
        # is actually decided on. The rows were ranked on delivered cost and
        # then priced on the headline bid, so the table read L1, L2 with the
        # numbers beside them going the other way, and the Saving column
        # subtracted a headline cost from a delivered baseline - pointing the
        # buyer at the worse deal by a wide margin.
        priced = []
        for bid in ranked:
            unit = engine.compare_price(bid)
            priced.append({
                "bid": bid, "vendor": bid.vendor, "unit": unit,
                "headline": bid.unit_price,
                "adders": engine.adders_for(db, auction, bid.vendor_id),
                "total": round(unit * line.qty, 2),
                "saving": round(engine.line_baseline(db, line) - unit * line.qty, 2),
            })
        rows.append({
            "line": line, "label": engine.line_label(line), "ranked": ranked,
            "priced": priced,
            "baseline": engine.line_baseline(db, line),
            "existing": existing, "best": ranked[0] if ranked else None,
            "chosen": chosen, "price": price, "note": note,
        })
    bidders = sorted({(bid.vendor_id, bid.vendor.name)
                      for row in rows for bid in row["ranked"]}, key=lambda pair: pair[1])
    return render(request, "award.html",
                  {"auction": auction, "rows": rows, "bidders": bidders, "error": error,
                   "summary": engine.auction_summary(db, auction),
                   "compare_price": engine.compare_price,
                   "adders_for": lambda vendor_id: engine.adders_for(db, auction, vendor_id)},
                  user=user, db=db, help_key="auction_detail_buyer")


@router.get("/{auction_id}/award")
def award_form(auction_id: int, request: Request, user: User = Depends(buyer_only),
               db: Session = Depends(get_db)):
    try:
        auction = _awardable(db, auction_id, user)
    except ActionError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    return _award_screen(request, db, user, auction)


@router.post("/{auction_id}/award")
async def post_award(auction_id: int, request: Request, user: User = Depends(buyer_only),
                     db: Session = Depends(get_db)):
    try:
        auction = _awardable(db, auction_id, user)
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
            if not raw_vendor.isdigit():
                raise ActionError(f"The bidder chosen for “{label}” was not one of the "
                                  "choices on the page. Reload it and pick again.")
            vendor = db.get(Vendor, int(raw_vendor))
            if not vendor:
                raise ActionError(f"The bidder chosen for “{label}” no longer exists.")
            if not db.query(Participant).filter_by(auction_id=auction.id,
                                                   vendor_id=vendor.id).first():
                raise ActionError(f"{vendor.name} was not invited to this auction, so they "
                                  f"cannot be awarded “{label}”.")
            bid = engine.vendor_best(db, line.id, vendor.id)

            raw_price = (form.get(f"price_{line.id}") or "").strip()
            if raw_price:
                try:
                    price = float(raw_price)
                except ValueError:
                    raise ActionError(f"On “{label}”, “{raw_price}” is not a price.")
                if not math.isfinite(price):
                    raise ActionError(f"On “{label}”, “{raw_price}” is not a real price.")
            elif bid:
                price = bid.unit_price
            else:
                raise ActionError(f"{vendor.name} did not bid on “{label}”, so there is no price "
                                  "to award at. Type one in, or leave that item unawarded.")
            # Round FIRST. Checking before rounding let a sub-paisa price such
            # as 0.004 through the "more than zero" guard and then stored it as
            # 0.00 - booking the whole line at nothing, emailing the winner
            # that figure, and reporting a 100% saving.
            price = round(price, 2)
            if price <= 0:
                raise ActionError(f"On “{label}”, the award price has to be more than zero. "
                                  "Prices are kept to the paisa, so anything under 0.01 "
                                  "rounds away to nothing.")

            # Where the auction was compared on delivered cost, record what the
            # awarded price works out to delivered - that is the money the
            # business spends, and what the savings are measured against.
            adders = engine.adders_for(db, auction, vendor.id)
            landed_unit = adders.landed(price) if auction.compare_landed else None
            award = Award(auction_id=auction.id, line_id=line.id, vendor_id=vendor.id,
                          # Only point at the bid when the award really is at
                          # that price; otherwise the link would claim a bidder
                          # offered a figure they never typed.
                          bid_id=bid.id if (bid and round(bid.unit_price, 2) == price) else None,
                          qty=line.qty, unit_price=price,
                          total=round(line.qty * price, 2),
                          landed_unit_price=landed_unit,
                          landed_total=(round(line.qty * landed_unit, 2)
                                        if landed_unit is not None else None),
                          awarded_by_id=user.id,
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
        db.expire_all()
        return _award_screen(request, db, user, _safe_auction(db, auction_id, user),
                             error=str(exc), form=form)

    # Winners hear what they won; everyone else hears the outcome too. Every
    # figure in those emails is on the basis the auction was decided on.
    landed = bool(auction.compare_landed)
    by_vendor: dict[int, list[Award]] = {}
    for award in created:
        by_vendor.setdefault(award.vendor_id, []).append(award)
    for vendor_id, awards in by_vendor.items():
        vendor = db.get(Vendor, vendor_id)
        rows = [(engine.line_label(a.line), fmt_qty(a.qty),
                 fmt_money((a.landed_unit_price or a.unit_price) if landed else a.unit_price))
                for a in awards]
        notify.awarded(db, auction, vendor, rows,
                       sum((a.landed_total or a.total) if landed else a.total for a in awards))
    for part in auction.participants:
        if part.vendor_id not in by_vendor:
            notify.not_awarded(db, auction, part.vendor)

    notify.award_summary(
        db, auction,
        # The awarded value and the savings have to be on one basis. Sending
        # the headline total beside a delivered saving made the two figures in
        # the same sentence impossible to reconcile.
        [(engine.line_label(a.line), a.vendor.name,
          f"{fmt_qty(a.qty)} @ {fmt_money(a.landed_unit_price or a.unit_price)}"
          if landed else f"{fmt_qty(a.qty)} @ {fmt_money(a.unit_price)}") for a in created],
        total=sum((a.landed_total or a.total) if landed else a.total for a in created),
        savings=summary["savings"], savings_pct=summary["savings_pct"])

    return redirect(f"/auctions/{auction.id}?tab=award",
                    f"Awarded to {len(by_vendor)} bidder(s). Savings of "
                    f"{fmt_money(summary['savings'])} ({summary['savings_pct']:.1f}%). "
                    "Everyone has been emailed the outcome.")
