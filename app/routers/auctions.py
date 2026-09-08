from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from .. import audit, engine, notify
from ..audit import record
from ..db import get_db
from ..models import (Approval, ApprovalStatus, Auction, AuctionLine, AuctionStatus, Award, Bid,
                      DecrementType, Item, Message, Participant, Unit, User, Vendor)
from ..security import buyer_only, current_user
from ..utils import alias_for, from_local_string
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
    items = form.getlist("line_item_id")
    units = form.getlist("line_unit_id")
    qtys = form.getlist("line_qty")
    prices = form.getlist("line_price")
    specs = form.getlist("line_spec")
    rows: list[dict] = []
    for index, item_id in enumerate(items):
        if not item_id:
            continue
        try:
            qty = float(qtys[index] or 0)
            price = float(prices[index] or 0)
        except ValueError:
            raise HTTPException(400, "Quantity and starting price must be numbers.")
        if qty <= 0:
            raise HTTPException(400, "Every item needs a quantity greater than zero.")
        if price <= 0:
            raise HTTPException(400, "Every item needs a starting price greater than zero.")
        if not db.get(Item, int(item_id)):
            raise HTTPException(400, "One of the items no longer exists.")
        rows.append({"item_id": int(item_id),
                     "unit_id": int(units[index]) if index < len(units) and units[index] else None,
                     "qty": qty, "starting_price": price,
                     "specification": specs[index] if index < len(specs) else ""})
    if not rows:
        raise HTTPException(400, "Add at least one item to the auction.")
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
        query = query.filter(Auction.status == AuctionStatus(status))
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
@router.get("/new")
def new_auction(request: Request, user: User = Depends(buyer_only),
                db: Session = Depends(get_db)):
    start = datetime.utcnow() + timedelta(hours=1)
    context = form_context(db)
    context.update({"auction": None, "default_start": start,
                    "default_end": start + timedelta(hours=2), "lines": []})
    return render(request, "auction_form.html", context, user=user, db=db,
                  help_key="auction_new")


@router.post("/new")
async def create_auction(request: Request, user: User = Depends(buyer_only),
                         db: Session = Depends(get_db)):
    form = await request.form()
    auction = Auction(reference=next_reference(db), creator_id=user.id,
                      title=(form.get("title") or "").strip(),
                      description=form.get("description", ""), terms=form.get("terms", ""))
    if not auction.title:
        raise HTTPException(400, "Give the auction a title so people know what it is.")
    _apply_settings(auction, form)
    lines = parse_lines(form, db)
    db.add(auction)
    db.flush()
    for row in lines:
        db.add(AuctionLine(auction_id=auction.id, **row))
    _sync_participants(db, auction, form.getlist("vendor_ids"))
    record(db, action="auction.create", entity_type="auction", entity_id=auction.id, actor=user,
           auction_id=auction.id, ip=client_ip(request),
           detail={"title": auction.title, "lines": len(lines)})
    db.commit()
    return redirect(f"/auctions/{auction.id}",
                    "Draft saved. Review it, then publish to invite your bidders.")


def _apply_settings(auction: Auction, form) -> None:
    auction.start_at = from_local_string(form.get("start_at", ""))
    auction.end_at = from_local_string(form.get("end_at", ""))
    auction.original_end_at = auction.end_at
    if auction.end_at <= auction.start_at:
        raise HTTPException(400, "The end time has to be after the start time.")
    auction.decrement_type = DecrementType(form.get("decrement_type", "absolute"))
    auction.min_decrement = float(form.get("min_decrement") or 0)
    auction.max_decrement = float(form.get("max_decrement") or 0)
    if auction.max_decrement and auction.max_decrement < auction.min_decrement:
        raise HTTPException(400, "The maximum decrement cannot be smaller than the minimum.")
    auction.show_rank = form.get("show_rank") == "on"
    auction.show_lowest_bid = form.get("show_lowest_bid") == "on"
    auction.hide_bidder_names = form.get("hide_bidder_names") == "on"
    auction.auto_extend = form.get("auto_extend") == "on"
    auction.extend_trigger_seconds = int(float(form.get("extend_trigger_minutes") or 2) * 60)
    auction.extend_by_seconds = int(float(form.get("extend_by_minutes") or 3) * 60)
    auction.max_extensions = int(form.get("max_extensions") or 0)
    auction.requires_approval = form.get("requires_approval") == "on"


def _sync_participants(db: Session, auction: Auction, vendor_ids) -> None:
    wanted = {int(v) for v in vendor_ids if v}
    if not wanted:
        raise HTTPException(400, "Invite at least one vendor — only invited vendors can bid.")
    existing = {p.vendor_id: p for p in auction.participants}
    for vendor_id in wanted - set(existing):
        db.add(Participant(auction_id=auction.id, vendor_id=vendor_id))
    for vendor_id in set(existing) - wanted:
        db.delete(existing[vendor_id])
    db.flush()
    for index, part in enumerate(sorted(auction.participants, key=lambda p: p.id)):
        part.alias = alias_for(index)


# ------------------------------------------------------------------ edit
@router.get("/{auction_id}/edit")
def edit_auction(auction_id: int, request: Request, user: User = Depends(buyer_only),
                 db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    if not auction.editable:
        raise HTTPException(403, "Bidding has started, so this auction can no longer be edited. "
                                 "You can still cancel it.")
    context = form_context(db)
    context.update({"auction": auction, "lines": auction.lines,
                    "selected_vendors": [p.vendor_id for p in auction.participants]})
    return render(request, "auction_form.html", context, user=user, db=db,
                  help_key="auction_new")


@router.post("/{auction_id}/edit")
async def update_auction(auction_id: int, request: Request, user: User = Depends(buyer_only),
                         db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction or not auction.editable:
        raise HTTPException(403, "This auction can no longer be edited.")
    form = await request.form()
    before = {"title": auction.title, "start": auction.start_at.isoformat(),
              "end": auction.end_at.isoformat()}
    auction.title = (form.get("title") or "").strip()
    auction.description = form.get("description", "")
    auction.terms = form.get("terms", "")
    _apply_settings(auction, form)
    rows = parse_lines(form, db)
    for line in list(auction.lines):
        db.delete(line)
    db.flush()
    for row in rows:
        db.add(AuctionLine(auction_id=auction.id, **row))
    _sync_participants(db, auction, form.getlist("vendor_ids"))
    record(db, action="auction.update", entity_type="auction", entity_id=auction.id, actor=user,
           auction_id=auction.id, ip=client_ip(request),
           detail={"before": before, "after": {"title": auction.title,
                                               "start": auction.start_at.isoformat(),
                                               "end": auction.end_at.isoformat()}})
    db.commit()
    return redirect(f"/auctions/{auction.id}", "Changes saved.")


# ------------------------------------------------------------------ lifecycle
@router.post("/{auction_id}/publish")
def publish(auction_id: int, request: Request, user: User = Depends(buyer_only),
            db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    if auction.status not in (AuctionStatus.DRAFT, AuctionStatus.REWORK):
        raise HTTPException(400, "Only a draft can be published.")
    if not auction.participants:
        raise HTTPException(400, "Invite at least one vendor first.")

    if auction.requires_approval:
        db.add(Approval(auction_id=auction.id, requested_by_id=user.id))
        auction.status = AuctionStatus.PENDING_APPROVAL
        record(db, action="auction.submit_for_approval", entity_type="auction",
               entity_id=auction.id, actor=user, auction_id=auction.id, ip=client_ip(request))
        db.commit()
        notify.approval_requested(db, auction, user)
        return redirect(f"/auctions/{auction.id}",
                        "Sent for approval. The approvers have been emailed.")

    auction.status = AuctionStatus.SCHEDULED
    auction.published_at = datetime.utcnow()
    record(db, action="auction.publish", entity_type="auction", entity_id=auction.id,
           actor=user, auction_id=auction.id, ip=client_ip(request),
           detail={"vendors": len(auction.participants)})
    db.commit()
    sent = notify.auction_invited(db, auction)
    return redirect(f"/auctions/{auction.id}",
                    f"Published. Invitations emailed to {sent} bidder contact(s).")


@router.post("/{auction_id}/cancel")
def cancel(auction_id: int, request: Request, reason: str = Form(""),
           user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    if auction.status in (AuctionStatus.AWARDED, AuctionStatus.CANCELLED):
        raise HTTPException(400, "This auction is already finished.")
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
    if not auction or auction.status != AuctionStatus.LIVE:
        raise HTTPException(400, "Only a live auction can be closed.")
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
