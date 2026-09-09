from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from .. import audit, engine, notify
from ..audit import record
from ..errors import ActionError, FormError
from ..db import get_db
from ..emails_util import EmailError, describe, normalise, parse as parse_emails, validate
from ..models import (Approval, ApprovalStatus, Auction, AuctionLine, AuctionStatus, Award, Bid,
                      DecrementType, Item, Message, Participant, Unit, User, Vendor)
from ..security import buyer_only, current_user
from ..utils import alias_for, fmt_dt, from_local_string
from ..web import client_ip, redirect, render

router = APIRouter(prefix="/auctions")

OPEN_TO_VENDOR = (AuctionStatus.SCHEDULED, AuctionStatus.LIVE, AuctionStatus.CLOSED,
                  AuctionStatus.AWARDED, AuctionStatus.CANCELLED)


# ------------------------------------------------------------------ helpers
def next_reference(db: Session) -> str:
    year = datetime.utcnow().year
    count = db.query(Auction).count() + 1
    while db.query(Auction).filter(Auction.reference == f"RA-{year}-{count:04d}").first():
        count += 1
    return f"RA-{year}-{count:04d}"


def visible_auction(db: Session, auction_id: int, user: User) -> Auction:
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    if user.is_buyer_side:
        return auction
    part = db.query(Participant).filter_by(auction_id=auction.id,
                                           vendor_id=user.vendor_id).first()
    if not part or auction.status not in OPEN_TO_VENDOR:
        raise HTTPException(403, "This auction is not open to you.")
    return auction


def parse_lines(form, db: Session) -> list[dict]:
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
            if price <= 0:
                raise FormError(f"Item {position}: a starting price has to be more than zero. "
                                "Leave it empty if you do not want a ceiling at all.",
                                "line_price")
        item = db.get(Item, int(item_id))
        if not item:
            raise FormError(f"Item {position} no longer exists. Pick a different one.",
                            "line_item_id")
        rows.append({"item_id": int(item_id),
                     "unit_id": int(units[index]) if index < len(units) and units[index] else None,
                     "qty": qty, "starting_price": price,
                     "specification": specs[index] if index < len(specs) else ""})
    if not rows:
        raise FormError("Add at least one item — an auction needs something to bid on.",
                        "line_item_id")
    return rows


def form_context(db: Session) -> dict:
    return {
        "items": db.query(Item).filter(Item.is_active.is_(True)).order_by(Item.name).all(),
        "units": db.query(Unit).order_by(Unit.code).all(),
        "vendors": db.query(Vendor).filter(Vendor.is_active.is_(True)).order_by(Vendor.name).all(),
    }


# ------------------------------------------------------------------ list
@router.get("")
def list_auctions(request: Request, status: str = "", q: str = "",
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = db.query(Auction)
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
        "auto_extend": form.get("auto_extend") == "on",
        "lines": lines,
        "vendor_ids": [int(v) for v in form.getlist("vendor_ids") if v],
        "overrides": {int(v): form.get(f"notify_emails_{v}", "")
                      for v in form.getlist("vendor_ids") if v},
    }


def _form_screen(request: Request, db: Session, user: User, auction: Auction | None,
                 *, error: FormError | None = None, form=None):
    """The create/edit screen, with an error banner and the typed values kept."""
    context = form_context(db)
    start = datetime.utcnow() + timedelta(hours=1)
    context.update({
        "auction": auction,
        "default_start": start, "default_end": start + timedelta(hours=2),
        "lines": auction.lines if auction else [],
        "selected_vendors": [p.vendor_id for p in auction.participants] if auction else [],
        "overrides": {p.vendor_id: p.notify_emails for p in auction.participants} if auction else {},
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
        auction = Auction(reference=next_reference(db), creator_id=user.id, title=title,
                          description=form.get("description", ""), terms=form.get("terms", ""))
        _apply_settings(auction, form)
        lines = parse_lines(form, db)
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


def _number(form, name: str, label: str, default: float = 0.0) -> float:
    raw = (form.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise FormError(f"{label} has to be a number — “{raw}” is not.", name)
    if value < 0:
        raise FormError(f"{label} cannot be negative.", name)
    return value


def _apply_settings(auction: Auction, form) -> None:
    for name, label in (("start_at", "the opening time"), ("end_at", "the closing time")):
        if not (form.get(name) or "").strip():
            raise FormError(f"Please set {label} for the auction.", name)
    try:
        auction.start_at = from_local_string(form.get("start_at", ""))
        auction.end_at = from_local_string(form.get("end_at", ""))
    except ValueError:
        raise FormError("The dates did not come through properly. Click the calendar icon in "
                        "each date box and pick a date and a time.", "start_at")
    auction.original_end_at = auction.end_at
    if auction.end_at <= auction.start_at:
        raise FormError("The auction closes before it opens. Set the closing time later than "
                        "the opening time.", "end_at")
    if (auction.end_at - auction.start_at).total_seconds() < 60:
        raise FormError("Give bidders at least a minute — set the closing time further out.",
                        "end_at")
    auction.decrement_type = DecrementType(form.get("decrement_type", "absolute"))
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
    auction.auto_extend = form.get("auto_extend") == "on"
    auction.extend_trigger_seconds = int(_number(form, "extend_trigger_minutes",
                                                 "The extension trigger", 2) * 60)
    auction.extend_by_seconds = int(_number(form, "extend_by_minutes",
                                            "The extension length", 3) * 60)
    auction.max_extensions = int(_number(form, "max_extensions", "The number of extensions", 5))
    if auction.auto_extend and auction.max_extensions and auction.extend_by_seconds <= 0:
        raise FormError("Auto-extension is on, so each extension needs to add some time.",
                        "extend_by_minutes")
    auction.requires_approval = False
    try:
        auction.cc_emails = "\n".join(validate(form.get("cc_emails", ""),
                                               field="email address"))
    except EmailError as exc:
        raise FormError(str(exc), "cc_emails")


def _participants(db: Session, auction: Auction) -> list[Participant]:
    return (db.query(Participant).filter(Participant.auction_id == auction.id)
              .order_by(Participant.id).all())


def _sync_participants(db: Session, auction: Auction, form) -> None:
    """Invite the ticked vendors, and record any per-auction address override."""
    wanted = {int(v) for v in form.getlist("vendor_ids") if v}
    if not wanted:
        raise FormError("Tick at least one bidder — only invited vendors can see the auction.",
                        "vendor_ids")
    existing = {p.vendor_id: p for p in _participants(db, auction)}
    for vendor_id in wanted - set(existing):
        db.add(Participant(auction_id=auction.id, vendor_id=vendor_id))
    for vendor_id in set(existing) - wanted:
        db.delete(existing[vendor_id])
    db.flush()
    # Read back from the database: on a brand-new auction the in-memory
    # ``auction.participants`` collection is still empty at this point.
    for index, part in enumerate(_participants(db, auction)):
        part.alias = alias_for(index)
        typed = form.get(f"notify_emails_{part.vendor_id}", "")
        try:
            part.notify_emails = "\n".join(validate(typed, field="email address"))
        except EmailError as exc:
            vendor = db.get(Vendor, part.vendor_id)
            raise FormError(f"{exc} (in the box under "
                            f"{vendor.name if vendor else 'one of the bidders'})", "vendor_ids")


# ------------------------------------------------------------------ edit
def _editable_auction(db: Session, auction_id: int) -> Auction:
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist. It may have been deleted.")
    if not auction.editable:
        raise ActionError("Bidding has already started, so the auction can no longer be "
                          "edited. You can still cancel it if it is wrong.")
    return auction


@router.get("/{auction_id}/edit")
def edit_auction(auction_id: int, request: Request, user: User = Depends(buyer_only),
                 db: Session = Depends(get_db)):
    try:
        auction = _editable_auction(db, auction_id)
    except ActionError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    return _form_screen(request, db, user, auction)


@router.post("/{auction_id}/edit")
async def update_auction(auction_id: int, request: Request, user: User = Depends(buyer_only),
                         db: Session = Depends(get_db)):
    try:
        auction = _editable_auction(db, auction_id)
    except ActionError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    form = await request.form()
    before = {"title": auction.title, "start": auction.start_at.isoformat(),
              "end": auction.end_at.isoformat()}
    try:
        title = (form.get("title") or "").strip()
        if not title:
            raise FormError("Give the auction a title, so bidders know what it is for.", "title")
        auction.title = title
        auction.description = form.get("description", "")
        auction.terms = form.get("terms", "")
        _apply_settings(auction, form)
        rows = parse_lines(form, db)
        for line in list(auction.lines):
            db.delete(line)
        db.flush()
        for row in rows:
            db.add(AuctionLine(auction_id=auction.id, **row))
        _sync_participants(db, auction, form)
        record(db, action="auction.update", entity_type="auction", entity_id=auction.id,
               actor=user, auction_id=auction.id, ip=client_ip(request),
               detail={"before": before, "after": {"title": auction.title,
                                                   "start": auction.start_at.isoformat(),
                                                   "end": auction.end_at.isoformat()}})
        db.commit()
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
    return redirect(f"/auctions/{auction.id}", "Changes saved.")


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
    if not auction:
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
    if not auction:
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
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    if auction.status in (AuctionStatus.AWARDED, AuctionStatus.CANCELLED):
        return redirect(f"/auctions/{auction.id}",
                        "This auction has already finished, so there is nothing to cancel.",
                        kind="error")
    auction.status = AuctionStatus.CANCELLED
    auction.cancelled_at = datetime.utcnow()
    auction.cancel_reason = reason
    record(db, action="auction.cancel", entity_type="auction", entity_id=auction.id, actor=user,
           auction_id=auction.id, ip=client_ip(request), detail={"reason": reason})
    db.commit()
    notify.auction_cancelled(db, auction, reason)
    return redirect(f"/auctions/{auction.id}", "Auction cancelled and everyone notified.")


@router.post("/{auction_id}/close-now")
def close_now(auction_id: int, request: Request, user: User = Depends(buyer_only),
              db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction:
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


# ------------------------------------------------------------------ detail
@router.get("/{auction_id}")
def detail(auction_id: int, request: Request, tab: str = "bids",
           user: User = Depends(current_user), db: Session = Depends(get_db)):
    auction = visible_auction(db, auction_id, user)
    context = build_detail_context(db, auction, user)
    context["tab"] = tab
    if tab == "history":
        context["logs"] = audit.for_auction(db, auction.id)
    help_key = "auction_detail_buyer" if user.is_buyer_side else "auction_detail_vendor"
    return render(request, "auction_detail.html", context, user=user, db=db, help_key=help_key)


def build_detail_context(db: Session, auction: Auction, user: User) -> dict:
    lines = []
    for line in auction.lines:
        ranked = engine.best_per_vendor(db, line.id)
        window = engine.bid_window(db, auction, line)
        mine = engine.vendor_best(db, line.id, user.vendor_id) if user.is_vendor else None
        lines.append({
            "line": line, "label": engine.line_label(line), "ranked": ranked, "window": window,
            "mine": mine,
            "my_rank": engine.vendor_rank(db, line.id, user.vendor_id) if user.is_vendor else None,
            "best": ranked[0] if ranked else None,
            "history": engine.line_bids(db, line.id),
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
        "approval": (db.query(Approval).filter(Approval.auction_id == auction.id)
                       .order_by(Approval.id.desc()).first()),
        "ApprovalStatus": ApprovalStatus, "AuctionStatus": AuctionStatus,
    }


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
    from ..web import templates
    return templates.TemplateResponse(request, "partials/live_board.html",
                                      {**context, "user": user, "request": request})
