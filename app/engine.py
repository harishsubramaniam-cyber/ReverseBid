"""The reverse auction engine: bid validation, ranking, auto-extension, savings.

House rules (all configurable per auction):

* The starting price is a **ceiling** - no bid may be above it.
* A new bid must beat the current lowest bid by at least the **minimum decrement**.
* It may not drop more than the **maximum decrement** in one step (0 = no cap).
* Rank L1 is the lowest price; ties are broken by who bid first.
* A bid in the last N seconds pushes the finish line back (**auto-extension**).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import notify
from .audit import record
from .models import (Auction, AuctionLine, AuctionStatus, Award, Bid, DecrementType,
                     Participant, User, Vendor)
from .utils import alias_for, fmt_money


class BidError(ValueError):
    """Raised with a plain-language message that is shown straight to the bidder."""


# ------------------------------------------------------------------ ranking
def line_bids(db: Session, line_id: int) -> list[Bid]:
    return (db.query(Bid)
              .filter(Bid.line_id == line_id, Bid.withdrawn.is_(False))
              .order_by(Bid.unit_price.asc(), Bid.created_at.asc()).all())


def best_per_vendor(db: Session, line_id: int) -> list[Bid]:
    """Each vendor's own best (lowest) live bid on a line, ranked L1 first."""
    seen: dict[int, Bid] = {}
    for bid in line_bids(db, line_id):
        if bid.vendor_id not in seen:
            seen[bid.vendor_id] = bid
    return sorted(seen.values(), key=lambda b: (b.unit_price, b.created_at))


def best_bid(db: Session, line_id: int) -> Bid | None:
    ranked = best_per_vendor(db, line_id)
    return ranked[0] if ranked else None


def vendor_best(db: Session, line_id: int, vendor_id: int) -> Bid | None:
    for bid in best_per_vendor(db, line_id):
        if bid.vendor_id == vendor_id:
            return bid
    return None


def vendor_rank(db: Session, line_id: int, vendor_id: int) -> int | None:
    for index, bid in enumerate(best_per_vendor(db, line_id), start=1):
        if bid.vendor_id == vendor_id:
            return index
    return None


def overall_ranking(db: Session, auction: Auction) -> list[tuple[int, float, int]]:
    """(vendor_id, total across all lines, lines bid) ordered by total ascending."""
    totals: dict[int, list[float]] = {}
    for line in auction.lines:
        for bid in best_per_vendor(db, line.id):
            entry = totals.setdefault(bid.vendor_id, [0.0, 0])
            entry[0] += bid.unit_price * line.qty
            entry[1] += 1
    line_count = len(auction.lines)
    rows = [(vid, val, int(cnt)) for vid, (val, cnt) in totals.items()]
    # A bidder who priced every line is comparable to the baseline; a partial
    # bidder's smaller total is not a better offer, so they rank below.
    return sorted(rows, key=lambda r: (r[2] < line_count, r[1]))


def alias_map(db: Session, auction: Auction) -> dict[int, str]:
    """Stable pseudonyms so hidden bidders stay consistent across the auction."""
    out: dict[int, str] = {}
    for index, part in enumerate(sorted(auction.participants, key=lambda p: p.id)):
        out[part.vendor_id] = part.alias or alias_for(index)
    return out


def display_name(db: Session, auction: Auction, vendor: Vendor, viewer: User) -> str:
    if not auction.hide_bidder_names or viewer.is_buyer_side:
        return vendor.name
    if viewer.vendor_id == vendor.id:
        return f"{vendor.name} (you)"
    return alias_map(db, auction).get(vendor.id, "Bidder")


# ------------------------------------------------------------------ bid limits
@dataclass
class BidWindow:
    """The price range a bidder may use right now, ready to show in the UI."""
    reference: float          # current lowest, or the ceiling if no bids yet
    reference_is_ceiling: bool
    max_allowed: float        # highest price accepted (reference - min decrement)
    min_allowed: float        # lowest price accepted (reference - max decrement)
    min_step: float
    max_step: float | None

    @property
    def suggestion(self) -> float:
        return round(self.max_allowed, 2)


def decrement_value(auction: Auction, reference: float, amount: float) -> float:
    if auction.decrement_type == DecrementType.PERCENT:
        return round(reference * amount / 100.0, 2)
    return round(amount, 2)


def bid_window(db: Session, auction: Auction, line: AuctionLine) -> BidWindow:
    current = best_bid(db, line.id)
    reference = current.unit_price if current else line.starting_price
    min_step = decrement_value(auction, reference, auction.min_decrement or 0.0)
    max_step = decrement_value(auction, reference, auction.max_decrement) if auction.max_decrement else None
    if current:
        max_allowed = round(reference - min_step, 2)
    else:
        # The very first bid only has to sit at or below the ceiling.
        max_allowed = round(reference, 2)
    min_allowed = round(reference - max_step, 2) if max_step else 0.01
    return BidWindow(reference=reference, reference_is_ceiling=current is None,
                     max_allowed=max(max_allowed, 0.01), min_allowed=max(min_allowed, 0.01),
                     min_step=min_step, max_step=max_step)


# ------------------------------------------------------------------ placing bids
def place_bid(db: Session, auction: Auction, line: AuctionLine, user: User,
              unit_price: float, note: str = "", ip: str = "") -> Bid:
    if not user.vendor_id:
        raise BidError("Only vendor users can bid.")
    if auction.status != AuctionStatus.LIVE:
        raise BidError("This auction is not open for bidding right now.")
    if datetime.utcnow() >= auction.end_at:
        raise BidError("The auction has just closed, so no further bids can be accepted.")
    if not db.query(Participant).filter_by(auction_id=auction.id,
                                           vendor_id=user.vendor_id).first():
        raise BidError("You are not on the invited bidder list for this auction.")
    if unit_price is None or unit_price <= 0:
        raise BidError("Enter a price greater than zero.")

    unit_price = round(float(unit_price), 2)
    window = bid_window(db, auction, line)
    previous_best = best_bid(db, line.id)

    if unit_price > line.starting_price:
        raise BidError(
            f"Your price is above the starting price of {fmt_money(line.starting_price)}. "
            "In a reverse auction the starting price is the most the buyer will pay, so your "
            "bid has to be at or below it.")
    if unit_price > window.max_allowed:
        raise BidError(
            f"Too high. The current lowest bid is {fmt_money(window.reference)} and you must go "
            f"at least {fmt_money(window.min_step)} below it — so "
            f"{fmt_money(window.max_allowed)} or less.")
    if window.max_step and unit_price < window.min_allowed:
        raise BidError(
            f"Too big a drop in one step. You can go down by at most "
            f"{fmt_money(window.max_step)} at a time, so {fmt_money(window.min_allowed)} "
            "is the lowest you can bid right now.")

    own = vendor_best(db, line.id, user.vendor_id)
    if own and unit_price >= own.unit_price:
        raise BidError(f"You have already bid {fmt_money(own.unit_price)} on this item. "
                       "A new bid has to be lower than your own last bid.")

    bid = Bid(auction_id=auction.id, line_id=line.id, vendor_id=user.vendor_id,
              user_id=user.id, unit_price=unit_price, qty=line.qty,
              total=round(unit_price * line.qty, 2), note=note[:400])
    db.add(bid)
    db.flush()

    label = line_label(line)
    record(db, action="bid.place", entity_type="bid", entity_id=bid.id, actor=user,
           auction_id=auction.id, ip=ip,
           detail={"line": label, "unit_price": unit_price, "total": bid.total})

    extended = maybe_extend(db, auction, user)
    db.commit()

    rank = vendor_rank(db, line.id, user.vendor_id) or 1
    notify.bid_received(db, auction, user, label, unit_price, rank)

    # Whoever was L1 before this bid has now been beaten.
    if previous_best and previous_best.vendor_id != user.vendor_id and \
            unit_price < previous_best.unit_price:
        notify.outbid(db, auction, previous_best.vendor, label,
                      new_best=unit_price, your_price=previous_best.unit_price)
    if extended:
        notify.auction_extended(db, auction, auction.extend_by_seconds)
    return bid


def withdraw_bid(db: Session, bid: Bid, user: User, reason: str = "", ip: str = "") -> None:
    auction = bid.auction
    if auction.status != AuctionStatus.LIVE:
        raise BidError("Bids can only be withdrawn while the auction is still live.")
    if user.vendor_id != bid.vendor_id and not user.is_buyer_side:
        raise BidError("You can only withdraw your own bids.")
    bid.withdrawn = True
    bid.withdrawn_at = datetime.utcnow()
    bid.withdraw_reason = reason[:400]
    record(db, action="bid.withdraw", entity_type="bid", entity_id=bid.id, actor=user,
           auction_id=auction.id, ip=ip, detail={"reason": reason,
                                                 "unit_price": bid.unit_price})
    db.commit()
    notify.bid_withdrawn(db, auction, bid.vendor, line_label(bid.line))


def line_label(line: AuctionLine) -> str:
    unit = f" / {line.unit.code}" if line.unit else ""
    return f"{line.item.name}{unit}"


# ------------------------------------------------------------------ auto-extension
def maybe_extend(db: Session, auction: Auction, actor: User | None = None) -> bool:
    if not auction.auto_extend:
        return False
    if auction.extensions_used >= (auction.max_extensions or 0):
        return False
    remaining = (auction.end_at - datetime.utcnow()).total_seconds()
    if remaining > auction.extend_trigger_seconds:
        return False
    auction.end_at = auction.end_at + timedelta(seconds=auction.extend_by_seconds)
    auction.extensions_used += 1
    auction.ending_soon_notified = False
    record(db, action="auction.auto_extend", entity_type="auction", entity_id=auction.id,
           actor=actor, auction_id=auction.id,
           detail={"new_end": auction.end_at.isoformat(),
                   "extension": auction.extensions_used})
    return True


# ------------------------------------------------------------------ savings
def line_result(db: Session, line: AuctionLine) -> dict:
    ranked = best_per_vendor(db, line.id)
    lowest = ranked[0] if ranked else None
    highest = ranked[-1] if ranked else None
    final_unit = lowest.unit_price if lowest else line.starting_price
    baseline = line.baseline
    final_value = final_unit * line.qty
    return {
        "line": line, "bids": len(line_bids(db, line.id)), "bidders": len(ranked),
        "lowest": lowest, "highest": highest,
        "final_unit": final_unit, "baseline": baseline, "final_value": final_value,
        "savings": baseline - final_value,
        "savings_pct": ((baseline - final_value) / baseline * 100) if baseline else 0.0,
    }


def auction_summary(db: Session, auction: Auction) -> dict:
    awards = db.query(Award).filter(Award.auction_id == auction.id).all()
    baseline = auction.baseline_value
    if awards:
        final_value = sum(a.total for a in awards)
        awarded_line_ids = {a.line_id for a in awards}
        for line in auction.lines:
            if line.id not in awarded_line_ids:
                final_value += line.baseline
        basis = "awarded"
    else:
        final_value = sum(line_result(db, l)["final_value"] for l in auction.lines)
        basis = "best bid"
    total_bids = db.query(Bid).filter(Bid.auction_id == auction.id,
                                      Bid.withdrawn.is_(False)).count()
    bidder_ids = {b.vendor_id for b in auction.bids if not b.withdrawn}
    return {
        "auction": auction, "baseline": baseline, "final_value": final_value,
        "savings": baseline - final_value,
        "savings_pct": ((baseline - final_value) / baseline * 100) if baseline else 0.0,
        "basis": basis, "total_bids": total_bids,
        "participants": len(auction.participants), "active_bidders": len(bidder_ids),
        "awards": awards,
    }
