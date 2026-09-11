from __future__ import annotations

import math
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from .. import audit, engine, notify
from ..audit import record
from ..engine import ADDER_FIELDS
from ..errors import ActionError, FormError
from ..db import get_db
from ..emails_util import EmailError, describe, normalise, parse as parse_emails, validate
from ..models import (Attachment, Auction, AuctionLine, AuctionStatus, Award, Bid,
                      DecrementType, Item, Message, Participant, Unit, User, Vendor)
from ..security import buyer_only, current_user
from ..utils import alias_for, fmt_dt, fmt_money, fmt_qty, from_local_string
from ..web import client_ip, redirect, render

router = APIRouter(prefix="/auctions")

#: Every tab the detail page knows how to draw. Anything else falls back to
#: "bids" rather than rendering a tab strip with nothing underneath it.
#: tests/test_regressions.py checks this against the template, so a tab added
#: there and forgotten here cannot go unnoticed.
TABS = ("bids", "details", "documents", "award", "conversation", "history")

OPEN_TO_VENDOR = (AuctionStatus.SCHEDULED, AuctionStatus.LIVE, AuctionStatus.CLOSED,
                  AuctionStatus.AWARDED, AuctionStatus.CANCELLED)


# ------------------------------------------------------------------ helpers
def next_reference(db: Session, org_id: int | None) -> str:
    year = datetime.utcnow().year
    count = db.query(Auction).filter(Auction.org_id == org_id).count() + 1
    while db.query(Auction).filter(Auction.reference == f"RA-{year}-{count:04d}",
                                   Auction.org_id == org_id).first():
        count += 1
    return f"RA-{year}-{count:04d}"


def visible_auction(db: Session, auction_id: int, user: User) -> Auction:
    """The one gate every screen that opens an auction goes through.

    An auction belongs to one buying organisation. Nobody outside it has any
    business knowing it exists, so this answers "does not exist" rather than
    "not allowed" - a 403 would confirm the id is real.
    """
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist.")
    if user.is_buyer_side:
        return auction
    part = db.query(Participant).filter_by(auction_id=auction.id,
                                           vendor_id=user.vendor_id).first()
    if not part or auction.status not in OPEN_TO_VENDOR:
        raise HTTPException(403, "This auction is not open to you.")
    return auction


def _whole_number(raw: str, label: str, field: str) -> int | None:
    """An id from a dropdown. Anything else came from a tampered or stale page."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise FormError(f"{label} was not one of the choices on the form. Reload the page "
                        "and pick it again.", field)
    return int(raw)


def parse_lines(form, db: Session, org_id: int | None) -> list[dict]:
    """Read the item rows. The starting price is optional; everything else is not."""
    items = form.getlist("line_item_id")
    units = form.getlist("line_unit_id")
    qtys = form.getlist("line_qty")
    prices = form.getlist("line_price")
    specs = form.getlist("line_spec")
    rows: list[dict] = []
    for index, item_id in enumerate(items):
        if not item_id:
            continue
        position = len(rows) + 1
        raw_qty = (qtys[index] if index < len(qtys) else "").strip()
        raw_price = (prices[index] if index < len(prices) else "").strip()
        try:
            qty = float(raw_qty or 0)
        except ValueError:
            raise FormError(f"Item {position}: the quantity “{raw_qty}” is not a number.",
                            "line_qty")
        # inf and nan are numbers to float() but not to anybody else: they were
        # stored and then rendered as "₹ inf" across every screen.
        if not math.isfinite(qty) or qty > 1e12:
            raise FormError(f"Item {position}: “{raw_qty}” is not a real quantity.", "line_qty")
        if qty <= 0:
            raise FormError(f"Item {position} needs a quantity greater than zero.", "line_qty")

        price = None
        if raw_price:
            try:
                price = float(raw_price)
            except ValueError:
                raise FormError(f"Item {position}: the starting price “{raw_price}” is not a "
                                "number. Leave it empty if you do not want a ceiling.",
                                "line_price")
            if not math.isfinite(price) or price > 1e12:
                raise FormError(f"Item {position}: “{raw_price}” is not a real price.",
                                "line_price")
            if price <= 0:
                raise FormError(f"Item {position}: a starting price has to be more than zero. "
                                "Leave it empty if you do not want a ceiling at all.",
                                "line_price")
            # Bids are compared to the paisa, so the ceiling is stored that way
            # too. Otherwise the screen offered a price the engine refused.
            price = round(price, 2)
        item_key = _whole_number(item_id, f"The item on row {position}", "line_item_id")
        item = db.get(Item, item_key) if item_key else None
        # Only this organisation's items: a tampered dropdown must not be able
        # to pull another company's item onto this auction.
        if item is not None and item.org_id != org_id:
            item = None
        if not item:
            raise FormError(f"Item {position} no longer exists. Pick a different one.",
                            "line_item_id")
        unit_key = _whole_number(units[index] if index < len(units) else "",
                                 f"The unit on row {position}", "line_unit_id")
        unit = db.get(Unit, unit_key) if unit_key else None
        if unit_key and (unit is None or unit.org_id != org_id):
            raise FormError(f"Item {position}: that unit no longer exists. Pick another.",
                            "line_unit_id")
        rows.append({"item_id": item.id, "unit_id": unit_key,
                     "qty": qty, "starting_price": price,
                     "specification": specs[index] if index < len(specs) else ""})
    if not rows:
        raise FormError("Add at least one item — an auction needs something to bid on.",
                        "line_item_id")
    return rows


def form_context(db: Session, org_id: int | None, auction: Auction | None = None) -> dict:
    """The pickers on the auction form.

    Archived vendors and items are hidden - except any this auction already
    uses. Leaving them out meant the browser could not post them back, so
    saving an unrelated change quietly uninvited a bidder or deleted a line.
    """
    items = (db.query(Item).filter(Item.is_active.is_(True), Item.org_id == org_id)
               .order_by(Item.name).all())
    vendors = (db.query(Vendor).filter(Vendor.is_active.is_(True), Vendor.org_id == org_id)
                 .order_by(Vendor.name).all())
    if auction is not None:
        have_items = {item.id for item in items}
        for line in auction.lines:
            if line.item and line.item_id not in have_items:
                items.append(line.item)
                have_items.add(line.item_id)
        items.sort(key=lambda item: item.name.lower())
        have_vendors = {vendor.id for vendor in vendors}
        for part in auction.participants:
            if part.vendor and part.vendor_id not in have_vendors:
                vendors.append(part.vendor)
                have_vendors.add(part.vendor_id)
        vendors.sort(key=lambda vendor: vendor.name.lower())
    return {
        "items": items,
        "units": db.query(Unit).filter(Unit.org_id == org_id).order_by(Unit.code).all(),
        "vendors": vendors,
    }


# ------------------------------------------------------------------ list
@router.get("")
def list_auctions(request: Request, status: str = "", q: str = "",
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = db.query(Auction).filter(Auction.org_id == user.org_id)
    if user.is_vendor:
        query = (query.join(Participant, Participant.auction_id == Auction.id)
                      .filter(Participant.vendor_id == user.vendor_id,
                              Auction.status.in_(OPEN_TO_VENDOR)))
    if status:
        try:
            query = query.filter(Auction.status == AuctionStatus(status))
        except ValueError:
            status = ""          # an unknown status in the URL just means "all"
    if q:
        like = f"%{q}%"
        query = query.filter(Auction.title.ilike(like) | Auction.reference.ilike(like))
    auctions = query.order_by(Auction.start_at.desc()).all()
    rows = [{"auction": a, "summary": engine.auction_summary(db, a),
             "my_rank": _my_rank(db, a, user)} for a in auctions]
    return render(request, "auctions_list.html",
                  {"rows": rows, "status": status, "q": q, "statuses": list(AuctionStatus)},
                  user=user, db=db, help_key="dashboard")


def _my_rank(db: Session, auction: Auction, user: User):
    if not user.is_vendor:
        return None
    ranks = [engine.vendor_rank(db, l.id, user.vendor_id) for l in auction.lines]
    ranks = [r for r in ranks if r]
    return min(ranks) if ranks else None


# ------------------------------------------------------------------ create
def _prefill(form) -> dict:
    """Everything the person typed, shaped the way the form template reads it,
    so a rejected submission comes back filled in rather than blank."""
    items = form.getlist("line_item_id")
    lines = []
    for index, item_id in enumerate(items):
        pick = lambda name, i=index: (form.getlist(name)[i]
                                      if i < len(form.getlist(name)) else "")
        lines.append({"item_id": item_id, "unit_id": pick("line_unit_id"),
                      "qty": pick("line_qty"), "starting_price": pick("line_price"),
                      "specification": pick("line_spec")})
    return {
        "title": form.get("title", ""), "description": form.get("description", ""),
        "terms": form.get("terms", ""), "start_at": form.get("start_at", ""),
        "end_at": form.get("end_at", ""), "cc_emails": form.get("cc_emails", ""),
        "decrement_type": form.get("decrement_type", "absolute"),
        "min_decrement": form.get("min_decrement", ""),
        "max_decrement": form.get("max_decrement", ""),
        "extend_trigger_minutes": form.get("extend_trigger_minutes", ""),
        "extend_by_minutes": form.get("extend_by_minutes", ""),
        "max_extensions": form.get("max_extensions", ""),
        "show_rank": form.get("show_rank") == "on",
        "show_lowest_bid": form.get("show_lowest_bid") == "on",
        "hide_bidder_names": form.get("hide_bidder_names") == "on",
        "compare_landed": form.get("compare_landed") == "on",
        "auto_extend": form.get("auto_extend") == "on",
        "adders": {int(v): {field: form.get(f"{field}_{v}", "")
                            for field, _ in ADDER_FIELDS}
                   | {f"{field}_basis": form.get(f"{field}_basis_{v}", "")
                      for field, _ in ADDER_FIELDS}
                   | {"other_label": form.get(f"other_label_{v}", "")}
                   for v in form.getlist("vendor_ids") if str(v).isdigit()},
        "lines": lines,
        # Anything that is not a plain id came from a tampered or stale page.
        # Skip it here: this function only redraws the form, and it must never
        # be the thing that fails while explaining a failure.
        "vendor_ids": [int(v) for v in form.getlist("vendor_ids") if str(v).isdigit()],
        "overrides": {int(v): form.get(f"notify_emails_{v}", "")
                      for v in form.getlist("vendor_ids") if str(v).isdigit()},
    }


def _form_screen(request: Request, db: Session, user: User, auction: Auction | None,
                 *, error: FormError | None = None, form=None):
    """The create/edit screen, with an error banner and the typed values kept."""
    context = form_context(db, user.org_id, auction)
    start = datetime.utcnow() + timedelta(hours=1)
    context.update({
        "auction": auction,
        "default_start": start, "default_end": start + timedelta(hours=2),
        "lines": auction.lines if auction else [],
        "selected_vendors": [p.vendor_id for p in auction.participants] if auction else [],
        "overrides": {p.vendor_id: p.notify_emails for p in auction.participants} if auction else {},
        "saved_adders": ({p.vendor_id: p for p in auction.participants} if auction else {}),
        "adder_fields": ADDER_FIELDS,
        "prefill": _prefill(form) if form is not None else None,
        "error": error.message if error else "",
        "error_field": error.field if error else "",
    })
    return render(request, "auction_form.html", context, user=user, db=db,
                  help_key="auction_new", status_code=200)


@router.get("/new")
def new_auction(request: Request, user: User = Depends(buyer_only),
                db: Session = Depends(get_db)):
    return _form_screen(request, db, user, None)


@router.post("/new")
async def create_auction(request: Request, user: User = Depends(buyer_only),
                         db: Session = Depends(get_db)):
    form = await request.form()
    try:
        title = (form.get("title") or "").strip()
        if not title:
            raise FormError("Give the auction a title, so bidders know what it is for.", "title")
        auction = Auction(reference=next_reference(db, user.org_id), creator_id=user.id,
                          org_id=user.org_id, title=title,
                          description=form.get("description", ""), terms=form.get("terms", ""))
        _apply_settings(auction, form)
        lines = parse_lines(form, db, user.org_id)
        db.add(auction)
        db.flush()
        for row in lines:
            db.add(AuctionLine(auction_id=auction.id, **row))
        _sync_participants(db, auction, form)
        record(db, action="auction.create", entity_type="auction", entity_id=auction.id,
               actor=user, auction_id=auction.id, ip=client_ip(request),
               detail={"title": auction.title, "lines": len(lines)})
        db.commit()
        if form.get("action") == "publish":
            message = _publish_now(db, auction, user, request)
            return redirect(f"/auctions/{auction.id}", message)
    except FormError as exc:
        db.rollback()
        return _form_screen(request, db, user, None, error=exc, form=form)
    except ActionError as exc:
        # Saved fine, but could not go out — say so on the auction itself.
        return redirect(f"/auctions/{auction.id}",
                        f"Saved as a draft, but not published: {exc}", kind="error")
    return redirect(f"/auctions/{auction.id}",
                    "Saved as a draft. Check it over, then press Publish to invite your bidders.")


def _number(form, name: str, label: str, default: float = 0.0,
            limit: float = 1e9) -> float:
    raw = (form.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise FormError(f"{label} has to be a number — “{raw}” is not.", name)
    # "1e999" is a valid entry for a number box and floats to infinity, which
    # then blew up on int() and took the whole half-filled form with it.
    if not math.isfinite(value):
        raise FormError(f"{label} has to be a real number — “{raw}” is not.", name)
    if value < 0:
        raise FormError(f"{label} cannot be negative.", name)
    if value > limit:
        raise FormError(f"{label} is far too large. Try a smaller number.", name)
    return value


def _apply_settings(auction: Auction, form) -> None:
    for name, label in (("start_at", "the opening time"), ("end_at", "the closing time")):
        if not (form.get(name) or "").strip():
            raise FormError(f"Please set {label} for the auction.", name)
    was_start, was_end = auction.start_at, auction.end_at
    try:
        auction.start_at = from_local_string(form.get("start_at", ""))
        auction.end_at = from_local_string(form.get("end_at", ""))
    except ValueError:
        raise FormError("The dates did not come through properly. Click the calendar icon in "
                        "each date box and pick a date and a time.", "start_at")
    auction.original_end_at = auction.end_at
    # A rescheduled auction needs its time-based alerts again. These flags were
    # never reset, so a bidder got "starts soon" for the old time and no
    # warning at all before the new one.
    if was_start != auction.start_at:
        auction.starting_soon_notified = False
    if was_end != auction.end_at:
        auction.ending_soon_notified = False
    if auction.end_at <= auction.start_at:
        raise FormError("The auction closes before it opens. Set the closing time later than "
                        "the opening time.", "end_at")
    if (auction.end_at - auction.start_at).total_seconds() < 60:
        raise FormError("Give bidders at least a minute — set the closing time further out.",
                        "end_at")
    try:
        auction.decrement_type = DecrementType(form.get("decrement_type", "absolute")
                                               or "absolute")
    except ValueError:
        raise FormError("Choose whether the minimum drop is a fixed amount or a percentage.",
                        "decrement_type")
    auction.min_decrement = _number(form, "min_decrement", "The minimum decrement")
    auction.max_decrement = _number(form, "max_decrement", "The maximum decrement")
    if auction.max_decrement and auction.max_decrement < auction.min_decrement:
        raise FormError("The maximum decrement is smaller than the minimum, which leaves no "
                        "price a bidder could legally offer.", "max_decrement")
    if auction.decrement_type == DecrementType.PERCENT and auction.min_decrement >= 100:
        raise FormError("A minimum decrement of 100% or more would leave nothing to bid.",
                        "min_decrement")
    auction.show_rank = form.get("show_rank") == "on"
    auction.show_lowest_bid = form.get("show_lowest_bid") == "on"
    auction.hide_bidder_names = form.get("hide_bidder_names") == "on"
    auction.compare_landed = form.get("compare_landed") == "on"
    auction.auto_extend = form.get("auto_extend") == "on"
    auction.extend_trigger_seconds = int(_number(form, "extend_trigger_minutes",
                                                 "The extension trigger", 2,
                                                 limit=7 * 24 * 60) * 60)
    auction.extend_by_seconds = int(_number(form, "extend_by_minutes",
                                            "The extension length", 3,
                                            limit=7 * 24 * 60) * 60)
    auction.max_extensions = int(_number(form, "max_extensions", "The number of extensions", 5,
                                         limit=1000))
    if auction.auto_extend and auction.max_extensions and auction.extend_by_seconds <= 0:
        raise FormError("Auto-extension is on, so each extension needs to add some time.",
                        "extend_by_minutes")
    try:
        auction.cc_emails = "\n".join(validate(form.get("cc_emails", ""),
                                               field="email address"))
    except EmailError as exc:
        raise FormError(str(exc), "cc_emails")


def _participants(db: Session, auction: Auction) -> list[Participant]:
    return (db.query(Participant).filter(Participant.auction_id == auction.id)
              .order_by(Participant.id).all())


def _sync_participants(db: Session, auction: Auction, form) -> list[Vendor]:
    """Invite the ticked vendors, and record any per-auction address override.

    Returns the vendors newly added by this submission, so a published auction
    can send them the invitation they would otherwise never receive.
    """
    wanted: set[int] = set()
    for raw in form.getlist("vendor_ids"):
        vendor_id = _whole_number(raw, "One of the ticked bidders", "vendor_ids")
        if vendor_id is None:
            continue
        candidate = db.get(Vendor, vendor_id)
        # Same again for the bidder list, and this one matters more: an
        # unchecked id here would put another company's supplier - name,
        # address and all - onto this auction.
        if not candidate or candidate.org_id != auction.org_id:
            raise FormError("One of the ticked bidders no longer exists. Reload the page and "
                            "choose again.", "vendor_ids")
        wanted.add(vendor_id)
    if not wanted:
        raise FormError("Tick at least one bidder — only invited vendors can see the auction.",
                        "vendor_ids")
    existing = {p.vendor_id: p for p in _participants(db, auction)}
    added = sorted(wanted - set(existing))
    for vendor_id in added:
        db.add(Participant(auction_id=auction.id, vendor_id=vendor_id))
    for vendor_id in set(existing) - wanted:
        db.delete(existing[vendor_id])
    db.flush()
    # Read back from the database: on a brand-new auction the in-memory
    # ``auction.participants`` collection is still empty at this point.
    parts = _participants(db, auction)
    # Keep an alias once it has been handed out - bidders have already seen
    # "Bidder B" on screen and in their emails, so renumbering on an edit would
    # move that name to a different company mid-auction. New bidders take the
    # next letter that is free.
    used = {part.alias for part in parts if part.alias}
    spare = 0
    for part in parts:
        if not part.alias:
            while alias_for(spare) in used:
                spare += 1
            part.alias = alias_for(spare)
            used.add(part.alias)
        typed = form.get(f"notify_emails_{part.vendor_id}", "")
        try:
            part.notify_emails = "\n".join(validate(typed, field="email address"))
        except EmailError as exc:
            vendor = db.get(Vendor, part.vendor_id)
            raise FormError(f"{exc} (in the box under "
                            f"{vendor.name if vendor else 'one of the bidders'})", "vendor_ids")
        _apply_adders(db, part, form, vendor_id in added)
    return [v for v in (db.get(Vendor, vendor_id) for vendor_id in added) if v]


def _apply_adders(db: Session, part: Participant, form, is_new: bool) -> None:
    """Read this bidder's delivered-cost adders off the form.

    A bidder invited for the first time starts from the defaults on their
    vendor record, so a buyer who has already told us what a supplier's
    freight costs does not have to type it again.
    """
    vendor = db.get(Vendor, part.vendor_id)
    posted_any = any(form.get(f"{field}_{part.vendor_id}") is not None
                     for field, _ in ADDER_FIELDS)
    for field, label in ADDER_FIELDS:
        name = f"{field}_{part.vendor_id}"
        basis_name = f"{field}_basis_{part.vendor_id}"
        if form.get(name) is not None:
            value = _number(form, name, f"{label} for {vendor.name if vendor else 'a bidder'}",
                            limit=1e9)
            basis = (form.get(basis_name) or "unit").strip().lower()
            if basis not in ("unit", "percent"):
                basis = "unit"
            if basis == "percent" and value >= 100:
                raise FormError(
                    f"{label} for {vendor.name if vendor else 'a bidder'} is {value:g}% — a "
                    "percentage that large is almost certainly a typo.", "vendor_ids")
            setattr(part, field, value)
            setattr(part, f"{field}_basis", basis)
        elif is_new and not posted_any and vendor is not None:
            setattr(part, field, getattr(vendor, f"default_{field}", 0.0) or 0.0)
            setattr(part, f"{field}_basis",
                    getattr(vendor, f"default_{field}_basis", "unit") or "unit")
    label_field = f"other_label_{part.vendor_id}"
    if form.get(label_field) is not None:
        part.other_label = (form.get(label_field) or "").strip()[:60]
    elif is_new and not posted_any and vendor is not None:
        part.other_label = vendor.default_other_label or ""


# ------------------------------------------------------------------ edit
def _editable_auction(db: Session, auction_id: int, user: User) -> Auction:
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist. It may have been deleted.")
    if not auction.editable:
        raise ActionError("Bidding has already started, so the auction can no longer be "
                          "edited. You can still cancel it if it is wrong.")
    return auction


@router.get("/{auction_id}/edit")
def edit_auction(auction_id: int, request: Request, user: User = Depends(buyer_only),
                 db: Session = Depends(get_db)):
    try:
        auction = _editable_auction(db, auction_id, user)
    except ActionError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    return _form_screen(request, db, user, auction)


@router.post("/{auction_id}/edit")
async def update_auction(auction_id: int, request: Request, user: User = Depends(buyer_only),
                         db: Session = Depends(get_db)):
    try:
        auction = _editable_auction(db, auction_id, user)
    except ActionError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    form = await request.form()
    was_published = auction.published
    before = {"title": auction.title, "start": auction.start_at.isoformat(),
              "end": auction.end_at.isoformat()}
    was_start, was_end, was_title = auction.start_at, auction.end_at, auction.title
    # Everything a bidder prices against, as it stands before the edit. A
    # bidder told only "the buyer changed something" has to guess what.
    was_terms = (auction.description or "", auction.terms or "")
    was_rules = (auction.min_decrement, auction.max_decrement,
                 auction.decrement_type, bool(auction.compare_landed))
    was_lines = {engine.line_label(line): (line.qty, line.starting_price)
                 for line in auction.lines}
    try:
        title = (form.get("title") or "").strip()
        if not title:
            raise FormError("Give the auction a title, so bidders know what it is for.", "title")
        auction.title = title
        auction.description = form.get("description", "")
        auction.terms = form.get("terms", "")
        _apply_settings(auction, form)
        rows = parse_lines(form, db, user.org_id)
        for line in list(auction.lines):
            db.delete(line)
        db.flush()
        for row in rows:
            db.add(AuctionLine(auction_id=auction.id, **row))
        newly_invited = _sync_participants(db, auction, form)
        record(db, action="auction.update", entity_type="auction", entity_id=auction.id,
               actor=user, auction_id=auction.id, ip=client_ip(request),
               detail={"before": before, "after": {"title": auction.title,
                                                   "start": auction.start_at.isoformat(),
                                                   "end": auction.end_at.isoformat()}})
        db.commit()
        # This session keeps objects alive across a commit, so auction.lines
        # and auction.participants would still be the ones this edit deleted.
        # Everything below - publishing, the emails, the change list - reads
        # them, and quoted the pre-edit items to the bidders.
        db.expire_all()
        auction = db.get(Auction, auction_id)
        if form.get("action") == "publish":
            message = _publish_now(db, auction, user, request)
            return redirect(f"/auctions/{auction.id}", "Changes saved. " + message)
    except FormError as exc:
        db.rollback()
        db.expire_all()
        return _form_screen(request, db, user, db.get(Auction, auction_id),
                            error=exc, form=form)
    except ActionError as exc:
        return redirect(f"/auctions/{auction.id}",
                        f"Changes saved, but not published: {exc}", kind="error")

    # Editing an auction the bidders already know about used to be silent: a
    # vendor added on the edit could bid without ever being invited, and a
    # moved closing time reached nobody.
    if not was_published:
        return redirect(f"/auctions/{auction.id}", "Changes saved.")
    changes: list[str] = []
    if was_title != auction.title:
        changes.append(f"Title is now “{auction.title}”")
    if was_start != auction.start_at:
        changes.append(f"Bidding now opens {fmt_dt(auction.start_at)}")
    if was_end != auction.end_at:
        changes.append(f"Bidding now closes {fmt_dt(auction.end_at)}")
    if was_terms != (auction.description or "", auction.terms or ""):
        changes.append("The description or the terms have been rewritten — read them again "
                       "before you bid")
    if was_rules != (auction.min_decrement, auction.max_decrement,
                     auction.decrement_type, bool(auction.compare_landed)):
        changes.append("The bidding rules have changed — the item list on the auction page "
                       "shows what you may now bid")
    # Read the rows back from the database: this session does not expire
    # objects on commit, so auction.lines would still hand back the ones the
    # edit deleted - and every line change would go unreported.
    now_lines = {engine.line_label(line): (line.qty, line.starting_price)
                 for line in db.query(AuctionLine)
                                .filter(AuctionLine.auction_id == auction.id).all()}
    for label in was_lines:
        if label not in now_lines:
            changes.append(f"“{label}” has been taken off this auction")
    for label, (qty, price) in now_lines.items():
        if label not in was_lines:
            changes.append(f"“{label}” has been added: {fmt_qty(qty)}"
                           + (f", starting price {fmt_money(price)}" if price else ""))
            continue
        was_qty, was_price = was_lines[label]
        if was_qty != qty:
            changes.append(f"“{label}”: quantity is now {fmt_qty(qty)} "
                           f"(it was {fmt_qty(was_qty)})")
        if was_price != price:
            changes.append(
                f"“{label}”: starting price is now "
                + (fmt_money(price) if price else "open, with no ceiling")
                + (f" (it was {fmt_money(was_price)})" if was_price else " (there was none)"))
    notify.auction_changed(db, auction, newly_invited, changes)
    told = []
    if newly_invited:
        told.append(f"{len(newly_invited)} new bidder(s) invited by email")
    if changes:
        told.append("the bidders have been emailed the change")
    suffix = f" — {', '.join(told)}." if told else ""
    return redirect(f"/auctions/{auction.id}", f"Changes saved{suffix}")


# ------------------------------------------------------------------ lifecycle
def _publish_now(db: Session, auction: Auction, user: User, request: Request,
                 start_now: bool = False) -> str:
    """Publish to the bidders. Raises ActionError with a plain-language reason.

    No approval step, by design. If the opening time has already passed — or the
    buyer asked to start now — bidding opens immediately rather than waiting for
    the next clock tick.
    """
    if auction.status in (AuctionStatus.LIVE, AuctionStatus.SCHEDULED):
        raise ActionError("This auction has already been published.")
    if auction.status in (AuctionStatus.CLOSED, AuctionStatus.AWARDED, AuctionStatus.CANCELLED):
        raise ActionError("This auction has finished, so it cannot be published again.")
    if not _participants(db, auction):
        raise ActionError("Invite at least one bidder before publishing.")
    if not auction.lines:
        raise ActionError("Add at least one item before publishing.")

    now = datetime.utcnow()
    going_live = start_now or auction.start_at <= now
    if going_live and auction.end_at <= now:
        raise ActionError("The closing time is already in the past. Set a closing time in the "
                          "future, then publish.")
    auction.published_at = now
    if going_live:
        auction.start_at = min(auction.start_at, now)
        auction.status = AuctionStatus.LIVE
        auction.started_at = now
    else:
        auction.status = AuctionStatus.SCHEDULED
    record(db, action="auction.publish", entity_type="auction", entity_id=auction.id,
           actor=user, auction_id=auction.id, ip=client_ip(request),
           detail={"vendors": len(auction.participants), "live_immediately": going_live})
    db.commit()

    sent = notify.auction_invited(db, auction)
    if going_live:
        notify.auction_started(db, auction)
    copied = notify.auction_published(db, auction, sent)
    extra = f" A copy went to {copied - 1} colleague(s)." if copied > 1 else ""
    opening = ("Bidding is open now." if going_live
               else f"Bidding opens {fmt_dt(auction.start_at)}.")
    return f"Published — {sent} bidder contact(s) invited by email. {opening}{extra}"


@router.post("/{auction_id}/publish")
def publish(auction_id: int, request: Request, start_now: str = Form(""),
            user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist. It may have been deleted.")
    try:
        message = _publish_now(db, auction, user, request, start_now == "on")
    except ActionError as exc:
        return redirect(f"/auctions/{auction.id}", str(exc), kind="error")
    return redirect(f"/auctions/{auction.id}", message)


@router.post("/{auction_id}/go-live")
def go_live(auction_id: int, request: Request, user: User = Depends(buyer_only),
            db: Session = Depends(get_db)):
    """Open a scheduled auction ahead of its start time."""
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist.")
    if auction.status != AuctionStatus.SCHEDULED:
        return redirect(f"/auctions/{auction.id}",
                        "Only a scheduled auction can be started early.", kind="error")
    now = datetime.utcnow()
    if auction.end_at <= now:
        return redirect(f"/auctions/{auction.id}",
                        "The closing time has already passed. Edit the auction and push the "
                        "closing time out first.", kind="error")
    auction.start_at = now
    auction.status = AuctionStatus.LIVE
    auction.started_at = now
    record(db, action="auction.start_early", entity_type="auction", entity_id=auction.id,
           actor=user, auction_id=auction.id, ip=client_ip(request))
    db.commit()
    notify.auction_started(db, auction)
    return redirect(f"/auctions/{auction.id}", "Bidding is open — every bidder has been emailed.")


@router.post("/{auction_id}/cancel")
def cancel(auction_id: int, request: Request, reason: str = Form(""),
           user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist.")
    if auction.status in (AuctionStatus.AWARDED, AuctionStatus.CANCELLED):
        return redirect(f"/auctions/{auction.id}",
                        "This auction has already finished, so there is nothing to cancel.",
                        kind="error")
    # A draft nobody was ever told about should not announce itself on the way
    # out: cancelling one used to email every prospective bidder the title of
    # an auction they had never been invited to.
    was_published = auction.published
    auction.status = AuctionStatus.CANCELLED
    auction.cancelled_at = datetime.utcnow()
    auction.cancel_reason = reason
    record(db, action="auction.cancel", entity_type="auction", entity_id=auction.id, actor=user,
           auction_id=auction.id, ip=client_ip(request),
           detail={"reason": reason, "was_published": was_published})
    db.commit()
    if not was_published:
        return redirect(f"/auctions/{auction.id}",
                        "Draft cancelled. Nobody was emailed, because this auction had never "
                        "been published.")
    notify.auction_cancelled(db, auction, reason)
    return redirect(f"/auctions/{auction.id}", "Auction cancelled and everyone notified.")


@router.post("/{auction_id}/close-now")
def close_now(auction_id: int, request: Request, user: User = Depends(buyer_only),
              db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist.")
    if auction.status != AuctionStatus.LIVE:
        return redirect(f"/auctions/{auction.id}",
                        "Only a live auction can be closed.", kind="error")
    auction.status = AuctionStatus.CLOSED
    auction.closed_at = auction.end_at = datetime.utcnow()
    record(db, action="auction.close_manual", entity_type="auction", entity_id=auction.id,
           actor=user, auction_id=auction.id, ip=client_ip(request))
    db.commit()
    notify.auction_closed(db, auction)
    return redirect(f"/auctions/{auction.id}", "Bidding closed. You can award it now.")


@router.post("/{auction_id}/more-time")
async def give_more_time(auction_id: int, request: Request, end_at: str = Form(""),
                         user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    """Move a live auction's closing time later.

    A live auction cannot be edited, which used to leave the buyer with no way
    to give bidders longer - only "close bidding now". The clock can only ever
    move outwards from here: cutting bidding short without warning is what the
    Close button is for, and everyone is told either way.
    """
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist.")
    back = f"/auctions/{auction.id}"
    if auction.status != AuctionStatus.LIVE:
        return redirect(back, "Only a live auction's clock can be moved.", kind="error")
    try:
        new_end = from_local_string(end_at)
    except ValueError:
        return redirect(back, "That was not a date and time. Use the calendar icon in the box.",
                        kind="error")
    now = datetime.utcnow()
    if new_end <= auction.end_at:
        return redirect(back, f"That is not later than the current closing time of "
                              f"{fmt_dt(auction.end_at)}. To finish early, use "
                              "“Close bidding now” instead.", kind="error")
    if new_end - now > timedelta(days=30):
        return redirect(back, "Thirty days is as far out as the clock can go. Pick a nearer "
                              "closing time.", kind="error")
    was = auction.end_at
    auction.end_at = new_end
    # The closing reminder has to fire again for the new time.
    auction.ending_soon_notified = False
    record(db, action="auction.more_time", entity_type="auction", entity_id=auction.id,
           actor=user, auction_id=auction.id, ip=client_ip(request),
           detail={"was": was.isoformat(), "now": new_end.isoformat()})
    db.commit()
    notify.auction_changed(db, auction, [],
                           [f"Bidding now closes {fmt_dt(new_end)} "
                            f"(it was {fmt_dt(was)})"])
    return redirect(back, f"Bidding now closes {fmt_dt(new_end)}. Every bidder has been "
                          "emailed the new time.")


# ------------------------------------------------------------------ detail
@router.get("/{auction_id}")
def detail(auction_id: int, request: Request, tab: str = "bids",
           user: User = Depends(current_user), db: Session = Depends(get_db)):
    auction = visible_auction(db, auction_id, user)
    context = build_detail_context(db, auction, user)
    # The audit trail is the buyer's record: it names every bidder, their
    # prices, their email addresses and their IPs. The tab was hidden from
    # bidders but the page behind it was not, so ?tab=history handed a
    # competitor the lot.
    if tab == "history" and not user.is_buyer_side:
        raise HTTPException(403, "The history and audit trail is only for the buyer.")
    # A tab name the page does not know - an old bookmark, a typo, a link from
    # before a tab was renamed - used to draw the tab strip with nothing at all
    # underneath it. Fall back to the bidding view.
    if tab not in TABS:
        tab = "bids"
    context["tab"] = tab
    if tab == "history":
        context["logs"] = audit.for_auction(db, auction.id)
    help_key = "auction_detail_buyer" if user.is_buyer_side else "auction_detail_vendor"
    return render(request, "auction_detail.html", context, user=user, db=db, help_key=help_key)


def build_detail_context(db: Session, auction: Auction, user: User) -> dict:
    lines = []
    my_adders = engine.adders_for(db, auction, user.vendor_id if user.is_vendor else None)
    docs = (db.query(Attachment).filter(Attachment.auction_id == auction.id)
              .order_by(Attachment.created_at.asc()).all())
    for line in auction.lines:
        ranked = engine.best_per_vendor(db, line.id)
        window = engine.bid_window(db, auction, line,
                                   user.vendor_id if user.is_vendor else None)
        mine = engine.vendor_best(db, line.id, user.vendor_id) if user.is_vendor else None
        lines.append({
            "line": line, "label": engine.line_label(line), "ranked": ranked, "window": window,
            "mine": mine,
            "docs": [d for d in docs
                     if d.item_id == line.item_id and _may_see_doc(d, user)],
            "my_rank": engine.vendor_rank(db, line.id, user.vendor_id) if user.is_vendor else None,
            "best": ranked[0] if ranked else None,
            # Every bid, withdrawn ones included: the panels that use this are
            # headed "Every bid on this item" and carry a withdrawn badge.
            "history": engine.all_line_bids(db, line.id),
            "result": engine.line_result(db, line),
        })
    messages_q = db.query(Message).filter(Message.auction_id == auction.id)
    if user.is_vendor:
        messages_q = messages_q.filter(Message.vendor_id == user.vendor_id)
    summary = engine.auction_summary(db, auction)
    awards = db.query(Award).filter(Award.auction_id == auction.id).all()
    return {
        "auction": auction, "lines": lines, "summary": summary,
        "aliases": engine.alias_map(db, auction),
        "display_name": lambda vendor: engine.display_name(db, auction, vendor, user),
        "messages": messages_q.order_by(Message.created_at.asc()).all(),
        "overall": engine.overall_ranking(db, auction),
        "vendor_by_id": {p.vendor_id: p.vendor for p in auction.participants},
        "awards": awards,
        "awards_by_line": _group_awards(awards),
        "recipients_for": lambda vendor_id: [r.email for r in
                                             notify.vendor_recipients(db, vendor_id, auction)
                                             if r.email],
        "cc_list": parse_emails(auction.cc_emails),
        "my_bids": (db.query(Bid).filter(Bid.auction_id == auction.id,
                                         Bid.vendor_id == user.vendor_id)
                      .order_by(Bid.created_at.desc()).all() if user.is_vendor else []),
        "seconds_left": max(0, int((auction.end_at - datetime.utcnow()).total_seconds())),
        "AuctionStatus": AuctionStatus,
        # --- delivered cost
        "my_adders": my_adders,
        "adders_for": lambda vendor_id: engine.adders_for(db, auction, vendor_id),
        "compare_price": engine.compare_price,
        # --- documents
        "docs": [d for d in docs if _may_see_doc(d, user)],
        "auction_docs": [d for d in docs
                         if d.item_id is None and _may_see_doc(d, user)],
        "my_docs": [d for d in docs if user.is_vendor and d.vendor_id == user.vendor_id],
    }


def _may_see_doc(doc: Attachment, user: User) -> bool:
    """A buyer sees every document on the auction. A bidder sees the buyer's
    documents and their own — never another supplier's paperwork."""
    if user.is_buyer_side:
        return True
    if doc.audience == "bidders" and doc.vendor_id is None:
        return True
    return doc.vendor_id is not None and doc.vendor_id == user.vendor_id


def _group_awards(awards) -> dict:
    grouped: dict[int, list] = {}
    for award in awards:
        grouped.setdefault(award.line_id, []).append(award)
    return grouped


@router.get("/{auction_id}/live")
def live_fragment(auction_id: int, request: Request, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    """Polled every few seconds by the auction page to refresh prices and the clock."""
    auction = visible_auction(db, auction_id, user)
    context = build_detail_context(db, auction, user)
    from ..security import csrf_token_for
    from ..web import templates
    # The refreshed board contains the bid forms, so it has to carry the token
    # those forms post back, or bidding would stop working after one refresh.
    return templates.TemplateResponse(
        request, "partials/live_board.html",
        {**context, "user": user, "request": request,
         "csrf_token": csrf_token_for(request)})
