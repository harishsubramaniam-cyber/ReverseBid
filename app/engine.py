"""The reverse auction engine: bid validation, ranking, auto-extension, savings.

House rules (all configurable per auction):

* The starting price is a **ceiling** - no bid may be above it.
* A new bid must beat the current lowest bid by at least the **minimum decrement**.
* It may not drop more than the **maximum decrement** in one step (0 = no cap).
* Rank L1 is the lowest price; ties are broken by who bid first.
* A bid in the last N seconds pushes the finish line back (**auto-extension**).
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from . import notify
from .audit import record
from .models import (Auction, AuctionLine, AuctionStatus, Award, Bid, DecrementType,
                     Participant, User, Vendor)
from .utils import alias_for, fmt_money

#: The smallest price anyone can bid. Below this there is nothing left to win.
MIN_PRICE = 0.01


class BidError(ValueError):
    """Raised with a plain-language message that is shown straight to the bidder."""


# ------------------------------------------------------------------ delivered cost
#: The components a buyer can add to a bidder's price to compare like with
#: like: (attribute on Participant, label shown to people).
ADDER_FIELDS = (("freight", "Freight"), ("duty", "Duty"),
                ("packaging", "Packaging"), ("other", "Other"))


@dataclass
class Adders:
    """What one bidder's price costs on top, to get the goods to the door.

    Two numbers do the work: an amount per unit (freight, packaging) and a
    percentage of the bid (duty, insurance). ``lines`` keeps the components
    apart so a person can see where the money goes.
    """
    per_unit: float = 0.0
    percent: float = 0.0
    lines: list[tuple[str, float, str]] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.per_unit or self.percent)

    def landed(self, bid_price: float) -> float:
        """The delivered price for a given bid."""
        return round(bid_price * (1 + self.percent / 100.0) + self.per_unit, 2)

    def to_bid(self, landed_price: float) -> float:
        """The most a bidder may type to land at or below ``landed_price``.

        Rounded down, never up: a price rounded up by a paisa would be
        offered on screen and then refused by the engine.
        """
        raw = (landed_price - self.per_unit) / (1 + self.percent / 100.0)
        return math.floor(raw * 100) / 100.0

    def from_bid(self, landed_price: float) -> float:
        """The least a bidder may type to land at or above ``landed_price``.

        The mirror of ``to_bid``, and it has to round the other way. Flooring a
        *lower* bound pushed the allowed bid under the floor, so a bidder with
        a percentage adder could drop a paisa further than the maximum
        decrement allows - the engine advertising, and then accepting, a bid
        that broke its own rule.
        """
        raw = (landed_price - self.per_unit) / (1 + self.percent / 100.0)
        return math.ceil(raw * 100) / 100.0

    def describe(self) -> str:
        parts = []
        for label, value, basis in self.lines:
            if not value:
                continue
            parts.append(f"{label} {fmt_money(value)}" if basis == "unit"
                         else f"{label} {value:g}%")
        return ", ".join(parts) or "none"


NO_ADDERS = Adders()


def adders_for(db: Session, auction: Auction, vendor_id: int | None) -> Adders:
    """This bidder's delivered-cost adders on this auction.

    Returns nothing at all unless the auction is being compared on delivered
    cost, so an ordinary auction is untouched by any of this.
    """
    if not auction.compare_landed or not vendor_id:
        return NO_ADDERS
    part = (db.query(Participant)
              .filter_by(auction_id=auction.id, vendor_id=vendor_id).first())
    return adders_from(part)


def adders_from(part: Participant | None) -> Adders:
    if part is None:
        return NO_ADDERS
    per_unit = percent = 0.0
    lines: list[tuple[str, float, str]] = []
    for attr, label in ADDER_FIELDS:
        value = float(getattr(part, attr, 0.0) or 0.0)
        basis = getattr(part, f"{attr}_basis", "unit") or "unit"
        if attr == "other" and (part.other_label or "").strip():
            label = part.other_label.strip()
        if value:
            lines.append((label, value, basis))
            if basis == "percent":
                percent += value
            else:
                per_unit += value
    return Adders(per_unit=round(per_unit, 2), percent=percent, lines=lines)


def compare_price(bid: Bid) -> float:
    """The number a bid is ranked on: its delivered price where the auction
    works that way, otherwise the bid itself."""
    auction = bid.auction
    if auction is not None and auction.compare_landed and bid.landed_unit_price:
        return bid.landed_unit_price
    return bid.unit_price


# ------------------------------------------------------------------ ranking
def line_bids(db: Session, line_id: int) -> list[Bid]:
    """The live bids on a line, cheapest first. Withdrawn bids are out of the race.

    "Cheapest" means the delivered price where the auction is compared that
    way, so the ordering here is the ranking everyone sees.
    """
    bids = (db.query(Bid)
              .filter(Bid.line_id == line_id, Bid.withdrawn.is_(False))
              .order_by(Bid.created_at.asc(), Bid.id.asc()).all())
    return sorted(bids, key=lambda bid: (compare_price(bid), bid.created_at, bid.id))


def all_line_bids(db: Session, line_id: int) -> list[Bid]:
    """Every bid ever placed on a line, withdrawn ones included, newest last.

    Ranking must ignore withdrawn bids, but anything that claims to show the
    history of an item has to show them - a bid that was placed and pulled is
    exactly what a buyer reviewing an auction needs to see.
    """
    return (db.query(Bid)
              .filter(Bid.line_id == line_id)
              .order_by(Bid.created_at.asc(), Bid.id.asc()).all())


def best_per_vendor(db: Session, line_id: int) -> list[Bid]:
    """Each vendor's own best (lowest) live bid on a line, ranked L1 first."""
    seen: dict[int, Bid] = {}
    for bid in line_bids(db, line_id):
        if bid.vendor_id not in seen:
            seen[bid.vendor_id] = bid
    return sorted(seen.values(), key=lambda b: (compare_price(b), b.created_at, b.id))


def highest_bid(db: Session, line_id: int) -> Bid | None:
    """The worst price anyone offered - the opening price, in practice.

    Deliberately not ``best_per_vendor(...)[-1]``: that is the highest of each
    vendor's *lowest* bids, which is a different (and much smaller) number.
    """
    bids = line_bids(db, line_id)
    return bids[-1] if bids else None


def best_bid(db: Session, line_id: int) -> Bid | None:
    ranked = best_per_vendor(db, line_id)
    return ranked[0] if ranked else None


def vendor_best(db: Session, line_id: int, vendor_id: int) -> Bid | None:
    for bid in best_per_vendor(db, line_id):
        if bid.vendor_id == vendor_id:
            return bid
    return None


def vendor_floor(db: Session, line_id: int, vendor_id: int) -> Bid | None:
    """The lowest price this vendor has ever offered on the line.

    Withdrawn bids count here. Otherwise a bidder could withdraw a keen price
    and then re-bid higher, walking their own offer back up - which is exactly
    what "a new bid has to be lower than your own last bid" exists to stop.
    """
    own = (db.query(Bid)
             .filter(Bid.line_id == line_id, Bid.vendor_id == vendor_id).all())
    if not own:
        return None
    return sorted(own, key=lambda bid: (bid.unit_price, bid.created_at, bid.id))[0]


def vendor_rank(db: Session, line_id: int, vendor_id: int) -> int | None:
    for index, bid in enumerate(best_per_vendor(db, line_id), start=1):
        if bid.vendor_id == vendor_id:
            return index
    return None


def overall_ranking(db: Session, auction: Auction) -> list[dict]:
    """Each bidder's basket: what they would cost, and what that saves.

    The saving has to be measured against the baseline of the items they
    actually priced. Comparing a partial bidder's smaller basket against the
    whole auction's baseline made not bidding on an item look like an enormous
    saving - on a two-item auction, the bidder who skipped the big line came
    out twenty times "better" than the one who priced everything.
    """
    totals: dict[int, list[float]] = {}
    for line in auction.lines:
        base = line_baseline(db, line)
        for bid in best_per_vendor(db, line.id):
            entry = totals.setdefault(bid.vendor_id, [0.0, 0, 0.0])
            entry[0] += compare_price(bid) * line.qty
            entry[1] += 1
            entry[2] += base
    line_count = len(auction.lines)
    rows = [{"vendor_id": vid, "total": val, "lines": int(cnt), "baseline": base,
             "savings": base - val, "complete": int(cnt) == line_count}
            for vid, (val, cnt, base) in totals.items()]
    # A bidder who priced every line is comparable to the baseline; a partial
    # bidder's smaller total is not a better offer, so they rank below.
    return sorted(rows, key=lambda r: (not r["complete"], r["total"]))


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


def line_baseline(db: Session, line: AuctionLine) -> float:
    """What this line was expected to cost.

    With a starting price it is quantity x ceiling. Without one there is no
    budget to compare against, so the highest bid received stands in - savings
    are then measured from the worst price offered, which is the honest
    reading. A line with neither is worth nothing to the savings maths.
    """
    if line.has_ceiling:
        return line.qty * line.starting_price
    top = highest_bid(db, line.id)
    return line.qty * compare_price(top) if top else 0.0


def auction_baseline(db: Session, auction: Auction) -> float:
    return sum(line_baseline(db, line) for line in auction.lines)


# ------------------------------------------------------------------ bid limits
@dataclass
class BidWindow:
    """The price range a bidder may use right now, ready to show in the UI.

    ``max_allowed`` and ``min_allowed`` are always in the price the bidder
    actually types. ``reference`` is in the price the auction is *compared*
    on - the same thing, unless delivered cost is being compared, in which
    case it is a delivered price and ``adders`` is what stands between them.
    """
    reference: float | None   # current lowest, or the ceiling if no bids yet
    reference_is_ceiling: bool
    max_allowed: float | None  # highest price accepted; None = no ceiling yet
    min_allowed: float        # lowest price accepted (reference - max decrement)
    min_step: float
    max_step: float | None
    #: True when the price has fallen so far that no legal bid is left.
    exhausted: bool = False
    #: Delivered-cost comparison, and this bidder's own adders.
    landed: bool = False
    adders: Adders = field(default_factory=Adders)

    @property
    def suggestion(self) -> float | None:
        return None if self.max_allowed is None else round(self.max_allowed, 2)

    @property
    def max_allowed_landed(self) -> float | None:
        """What the highest allowed bid works out to, delivered."""
        if self.max_allowed is None:
            return None
        return self.adders.landed(self.max_allowed)

    @property
    def open_ended(self) -> bool:
        """No ceiling and no bids yet - the bidder names the opening price."""
        return self.max_allowed is None and not self.exhausted


def decrement_value(auction: Auction, reference: float, amount: float) -> float:
    """How much lower the next bid has to be, in money.

    A percentage of a cheap unit can round to nothing, which would quietly turn
    the rule off, so any decrement the buyer actually asked for is worth at
    least one paisa.
    """
    if not amount:
        return 0.0
    if auction.decrement_type == DecrementType.PERCENT:
        step = round(reference * amount / 100.0, 2)
    else:
        step = round(amount, 2)
    return max(step, MIN_PRICE)


def bid_window(db: Session, auction: Auction, line: AuctionLine,
               vendor_id: int | None = None) -> BidWindow:
    """What this bidder may offer on this line right now.

    Where delivered cost is being compared, the rules are applied to delivered
    prices and then converted back into the price this particular bidder types
    - so two bidders with different freight see different numbers, which is
    the whole point of comparing that way.
    """
    adders = adders_for(db, auction, vendor_id)
    landed = bool(auction.compare_landed)
    current = best_bid(db, line.id)
    if current is None and not line.has_ceiling:
        # No ceiling, no bids: anything positive opens the line.
        return BidWindow(reference=None, reference_is_ceiling=True, max_allowed=None,
                         min_allowed=MIN_PRICE, min_step=0.0, max_step=None,
                         landed=landed, adders=adders)

    reference = round(compare_price(current) if current else line.starting_price, 2)
    min_step = decrement_value(auction, reference, auction.min_decrement or 0.0)
    max_step = (decrement_value(auction, reference, auction.max_decrement)
                if auction.max_decrement else None)
    if current:
        ceiling_compare = round(reference - max(min_step, MIN_PRICE), 2)
    else:
        # The very first bid only has to sit at or below the ceiling.
        ceiling_compare = round(reference, 2)
    max_allowed = round(adders.to_bid(ceiling_compare), 2)
    if max_allowed < MIN_PRICE:
        # Nothing left to bid: either the price has bottomed out, or this
        # bidder's own delivered costs already use up the whole ceiling.
        return BidWindow(reference=reference, reference_is_ceiling=current is None,
                         max_allowed=None, min_allowed=MIN_PRICE, min_step=min_step,
                         max_step=max_step, exhausted=True, landed=landed, adders=adders)
    # A lower bound, so it rounds up - see Adders.from_bid.
    min_allowed = (round(adders.from_bid(round(reference - max_step, 2)), 2)
                   if max_step else MIN_PRICE)
    return BidWindow(reference=reference, reference_is_ceiling=current is None,
                     max_allowed=max_allowed, min_allowed=max(min_allowed, MIN_PRICE),
                     min_step=min_step, max_step=max_step, landed=landed, adders=adders)


# ------------------------------------------------------------------ placing bids
#: One lock per line, so two bids on the same item cannot be checked against
#: the same "current lowest" and both be accepted. Deployments that run several
#: worker processes need the database to do this instead - see _lock_auction.
_line_locks: dict[int, threading.Lock] = {}
_line_locks_guard = threading.Lock()


def _line_lock(line_id: int) -> threading.Lock:
    with _line_locks_guard:
        return _line_locks.setdefault(line_id, threading.Lock())


def _lock_auction(db: Session, auction_id: int) -> None:
    """Take a database row lock on the auction, where the database has them.

    SQLite has no row locks, but it also has no second worker process to race
    with in the setup this ships as; PostgreSQL does, and this is what keeps
    two workers honest.
    """
    try:
        if db.get_bind().dialect.name == "sqlite":
            return
        db.query(Auction).filter(Auction.id == auction_id).with_for_update().first()
    except Exception:            # pragma: no cover - dialect without FOR UPDATE
        pass


def place_bid(db: Session, auction: Auction, line: AuctionLine, user: User,
              unit_price: float, note: str = "", ip: str = "") -> Bid:
    if not user.vendor_id:
        raise BidError("Only vendor users can bid.")
    if unit_price is None or not math.isfinite(unit_price) or unit_price <= 0:
        raise BidError("Enter a real price greater than zero.")
    if unit_price > 1e12:
        raise BidError("That price is too large to be real. Check for an extra digit.")
    unit_price = round(float(unit_price), 2)

    with _line_lock(line.id):
        _lock_auction(db, auction.id)
        # Re-read: another bid may have moved the price or the clock since the
        # page was drawn, and in a second worker since this request started.
        db.refresh(auction)
        db.refresh(line)

        if auction.status != AuctionStatus.LIVE:
            raise BidError("This auction is not open for bidding right now.")
        if datetime.utcnow() >= auction.end_at:
            raise BidError("The auction has just closed, so no further bids can be accepted.")
        if not db.query(Participant).filter_by(auction_id=auction.id,
                                               vendor_id=user.vendor_id).first():
            raise BidError("You are not on the invited bidder list for this auction.")

        window = bid_window(db, auction, line, user.vendor_id)
        previous_best = best_bid(db, line.id)
        ceiling = round(line.starting_price, 2) if line.has_ceiling else None
        adders = window.adders
        landed_price = adders.landed(unit_price)
        # What everything below is measured in: the delivered price where the
        # auction is compared that way, the bid itself where it is not.
        offered = landed_price if window.landed else unit_price
        delivered_note = (
            f" Your price of {fmt_money(unit_price)} plus {adders.describe()} comes to "
            f"{fmt_money(landed_price)} delivered." if window.landed and adders.any else "")

        # "There is no price you could offer" comes before "your price is too
        # high": telling a bidder whose own freight exceeds the ceiling to try
        # a lower number would send them round in circles.
        if window.exhausted:
            if window.landed and adders.any and window.reference_is_ceiling:
                raise BidError(
                    f"Your delivered costs ({adders.describe()}) already use up the whole "
                    f"starting price of {fmt_money(window.reference)}, so there is no price "
                    "you could offer. Ask the buyer to look at this item.")
            raise BidError(
                f"The lowest bid is already {fmt_money(window.reference)}, and going "
                f"{fmt_money(window.min_step)} below that would leave nothing to bid. "
                "Bidding on this item has gone as far as it can.")
        if ceiling is not None and offered > ceiling:
            what = "delivered price" if window.landed and adders.any else "price"
            raise BidError(
                f"Your {what} is above the starting price of {fmt_money(ceiling)}. "
                "In a reverse auction the starting price is the most the buyer will pay, so "
                f"your bid has to be at or below it.{delivered_note}")
        if window.max_allowed is not None and unit_price > window.max_allowed:
            lowest = ("The current lowest delivered price is" if window.landed
                      else "The current lowest bid is")
            raise BidError(
                f"Too high. {lowest} {fmt_money(window.reference)} and you must go at least "
                f"{fmt_money(window.min_step)} below it — so type "
                f"{fmt_money(window.max_allowed)} or less.{delivered_note}")
        # Belt and braces for the case where the required step rounded down to
        # nothing: a new bid still has to be genuinely lower, never a match.
        if previous_best and offered >= window.reference:
            lowest = ("The current lowest delivered price is" if window.landed
                      else "The current lowest bid is")
            raise BidError(
                f"Too high. {lowest} {fmt_money(window.reference)} and your bid has to come "
                f"in below it.{delivered_note}")
        if window.max_step and unit_price < window.min_allowed:
            raise BidError(
                f"Too big a drop in one step. You can go down by at most "
                f"{fmt_money(window.max_step)} at a time, so {fmt_money(window.min_allowed)} "
                "is the lowest you can bid right now.")

        own = vendor_floor(db, line.id, user.vendor_id)
        if own and unit_price >= own.unit_price:
            if own.withdrawn:
                raise BidError(
                    f"You bid {fmt_money(own.unit_price)} on this item earlier and withdrew "
                    "it. A new bid still has to be lower than that — withdrawing a bid does "
                    "not let you offer a higher price. Speak to the buyer if that price was "
                    "a mistake.")
            raise BidError(f"You have already bid {fmt_money(own.unit_price)} on this item. "
                           "A new bid has to be lower than your own last bid.")

        bid = Bid(auction_id=auction.id, line_id=line.id, vendor_id=user.vendor_id,
                  user_id=user.id, unit_price=unit_price, qty=line.qty,
                  total=round(unit_price * line.qty, 2),
                  landed_unit_price=landed_price, note=note[:400])
        db.add(bid)
        db.flush()

        label = line_label(line)
        detail = {"line": label, "unit_price": unit_price, "total": bid.total}
        if window.landed:
            detail["landed_unit_price"] = landed_price
            detail["adders"] = adders.describe()
        record(db, action="bid.place", entity_type="bid", entity_id=bid.id, actor=user,
               auction_id=auction.id, ip=ip, detail=detail)

        extended = maybe_extend(db, auction, user)
        extended_by = auction.extend_by_seconds
        db.commit()

    rank = vendor_rank(db, line.id, user.vendor_id) or 1
    notify.bid_received(db, auction, user, label, unit_price, rank)

    # Whoever was L1 before this bid has now been beaten.
    if previous_best and previous_best.vendor_id != user.vendor_id and \
            offered < compare_price(previous_best):
        notify.outbid(db, auction, previous_best.vendor, label,
                      new_best=offered, your_price=compare_price(previous_best),
                      landed=window.landed)
    if extended:
        notify.auction_extended(db, auction, extended_by)
    return bid


def withdraw_bid(db: Session, bid: Bid, user: User, reason: str = "", ip: str = "") -> None:
    auction = bid.auction
    if auction.status != AuctionStatus.LIVE or datetime.utcnow() >= auction.end_at:
        raise BidError("Bidding has finished, so bids can no longer be withdrawn. "
                       "Speak to the buyer if this bid was a mistake.")
    if user.vendor_id != bid.vendor_id and not user.is_buyer_side:
        raise BidError("You can only withdraw your own bids.")
    if bid.withdrawn:
        raise BidError("That bid has already been withdrawn.")
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
    """Push the finish line back for a bid that landed in the closing window.

    The move is written as one conditional UPDATE guarded on the extension
    count we read, so two bids arriving together cannot both claim the same
    extension - which would tell every bidder twice that the clock had moved
    while it only moved once.
    """
    if not auction.auto_extend:
        return False
    used = auction.extensions_used or 0
    if used >= (auction.max_extensions or 0):
        return False
    if not auction.extend_by_seconds or auction.extend_by_seconds <= 0:
        return False
    remaining = (auction.end_at - datetime.utcnow()).total_seconds()
    if remaining <= 0 or remaining > auction.extend_trigger_seconds:
        return False

    new_end = auction.end_at + timedelta(seconds=auction.extend_by_seconds)
    result = db.execute(
        update(Auction)
        .where(Auction.id == auction.id,
               Auction.extensions_used == used,
               Auction.status == AuctionStatus.LIVE)
        .values(end_at=new_end, extensions_used=used + 1, ending_soon_notified=False))
    if result.rowcount != 1:
        # Somebody else got there first. Pick up their change and say nothing.
        db.refresh(auction)
        return False
    db.refresh(auction)
    record(db, action="auction.auto_extend", entity_type="auction", entity_id=auction.id,
           actor=actor, auction_id=auction.id,
           detail={"new_end": auction.end_at.isoformat(),
                   "extension": auction.extensions_used})
    return True


# ------------------------------------------------------------------ savings
def line_result(db: Session, line: AuctionLine) -> dict:
    """Where one line ended up, and what it saved.

    Once the line has been awarded the awarded price is what the buyer will
    actually pay, so that - not the lowest bid - is the honest final figure.
    Without it the line-level savings never added up to the auction total when
    the buyer awarded to anyone but L1, or negotiated the price.
    """
    ranked = best_per_vendor(db, line.id)
    lowest = ranked[0] if ranked else None
    highest = highest_bid(db, line.id)
    award = db.query(Award).filter(Award.line_id == line.id).first()
    baseline = line_baseline(db, line)
    decided = line.auction is not None and line.auction.status == AuctionStatus.AWARDED
    landed = line.auction is not None and bool(line.auction.compare_landed)
    if award:
        # Delivered where the auction was compared that way: that is the money
        # the business actually parts with.
        final_unit = (award.landed_unit_price or award.unit_price) if landed \
            else award.unit_price
        basis = "awarded"
        # Use the total that was BOOKED, not qty x price recomputed. The award
        # stores round(qty x price, 2) and auction_summary adds those stored
        # totals up, so on a fractional quantity the line figures and the
        # auction figure drifted apart by a fraction of a paisa and the columns
        # stopped adding up.
        booked = (award.landed_total or award.total) if landed else award.total
    elif decided:
        # The auction has been awarded and this line was left out, so nothing
        # was bought and nothing was saved. Counting the best bid here would
        # claim a saving the buyer never made, and the line figures would not
        # add up to the auction total.
        final_unit = (baseline / line.qty) if line.qty else 0.0
        basis = "not awarded"
    elif lowest:
        final_unit = compare_price(lowest)
        basis = "best delivered price" if landed else "best bid"
    else:
        final_unit = line.starting_price or 0.0
        basis = "no bids"
    final_value = booked if award else final_unit * line.qty
    return {
        "line": line, "bids": len(line_bids(db, line.id)), "bidders": len(ranked),
        "lowest": lowest, "highest": highest, "award": award, "basis": basis,
        "final_unit": final_unit, "baseline": baseline, "final_value": final_value,
        "savings": baseline - final_value,
        "savings_pct": ((baseline - final_value) / baseline * 100) if baseline else 0.0,
    }


def auction_summary(db: Session, auction: Auction) -> dict:
    awards = db.query(Award).filter(Award.auction_id == auction.id).all()
    baseline = auction_baseline(db, auction)
    landed = bool(auction.compare_landed)
    if awards:
        final_value = sum((award.landed_total or award.total) if landed else award.total
                          for award in awards)
        awarded_line_ids = {a.line_id for a in awards}
        for line in auction.lines:
            if line.id not in awarded_line_ids:
                final_value += line_baseline(db, line)
        basis = "awarded"
    else:
        final_value = sum(line_result(db, l)["final_value"] for l in auction.lines)
        basis = "best delivered price" if landed else "best bid"
    total_bids = db.query(Bid).filter(Bid.auction_id == auction.id,
                                      Bid.withdrawn.is_(False)).count()
    bidder_ids = {b.vendor_id for b in auction.bids if not b.withdrawn}
    return {
        "auction": auction, "baseline": baseline, "final_value": final_value,
        "savings": baseline - final_value,
        "savings_pct": ((baseline - final_value) / baseline * 100) if baseline else 0.0,
        "basis": basis, "total_bids": total_bids,
        "open_lines": sum(1 for l in auction.lines if not l.has_ceiling),
        "participants": len(auction.participants), "active_bidders": len(bidder_ids),
        "awards": awards, "landed": landed,
    }
