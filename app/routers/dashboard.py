from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import engine
from ..db import get_db
from ..models import Auction, AuctionStatus, Award, Bid, Participant, User, Vendor
from ..security import current_user
from ..utils import TZ
from ..web import render

router = APIRouter()

#: The statuses a bidder may see. A draft is nobody's business but the buyer's.
VENDOR_VISIBLE = (AuctionStatus.SCHEDULED, AuctionStatus.LIVE, AuctionStatus.CLOSED,
                  AuctionStatus.AWARDED, AuctionStatus.CANCELLED)


@router.get("/")
def home(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.is_vendor:
        return _vendor_home(request, user, db)
    return _buyer_home(request, user, db)


def _buyer_home(request: Request, user: User, db: Session):
    auctions = (db.query(Auction).filter(Auction.org_id == user.org_id)
                  .order_by(Auction.start_at.desc()).all())
    summaries = [engine.auction_summary(db, a) for a in auctions
                 if a.status in (AuctionStatus.CLOSED, AuctionStatus.AWARDED)]
    awarded = [s for s in summaries if s["auction"].status == AuctionStatus.AWARDED]
    baseline = sum(s["baseline"] for s in awarded)
    final = sum(s["final_value"] for s in awarded)

    now = datetime.utcnow()
    metrics = {
        "savings": baseline - final,
        "savings_pct": ((baseline - final) / baseline * 100) if baseline else 0.0,
        "baseline": baseline,
        "spend": final,
        "live": sum(1 for a in auctions if a.status == AuctionStatus.LIVE),
        "scheduled": sum(1 for a in auctions if a.status == AuctionStatus.SCHEDULED),
        "awaiting_award": sum(1 for a in auctions if a.status == AuctionStatus.CLOSED),
        "total": len(auctions),
        "bids": (db.query(Bid).join(Auction, Auction.id == Bid.auction_id)
                   .filter(Bid.withdrawn.is_(False),
                           Auction.org_id == user.org_id).count()),
        "vendors": db.query(Vendor).filter(Vendor.is_active.is_(True),
                                           Vendor.org_id == user.org_id).count(),
    }
    trend = _monthly_savings(db, user.org_id, months=6)
    live = [a for a in auctions if a.status in (AuctionStatus.LIVE, AuctionStatus.SCHEDULED)]
    closing = sorted([a for a in auctions if a.status == AuctionStatus.LIVE],
                     key=lambda a: a.end_at)[:5]
    recent = auctions[:8]
    summaries = {a.id: engine.auction_summary(db, a) for a in recent}
    return render(request, "dashboard.html",
                  {"metrics": metrics, "recent": recent, "summaries": summaries, "live": live,
                   "closing": closing, "trend": trend, "now": now,
                   "top": sorted(awarded, key=lambda s: s["savings"], reverse=True)[:5]},
                  user=user, db=db, help_key="dashboard")


def _naive_utc(value: datetime) -> datetime:
    """A local, tz-aware instant as the naive UTC the database stores."""
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _monthly_savings(db: Session, org_id: int | None, months: int = 6):
    first = datetime.now(TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    starts = [first]
    for _ in range(months - 1):
        starts.append((starts[-1] - timedelta(days=1)).replace(day=1))
    buckets = []
    for start in reversed(starts):
        end = (start + timedelta(days=32)).replace(day=1)
        rows = (db.query(Auction).filter(Auction.org_id == org_id,
                                         Auction.status == AuctionStatus.AWARDED,
                                         Auction.awarded_at >= _naive_utc(start),
                                         Auction.awarded_at < _naive_utc(end)).all())
        total = sum(engine.auction_summary(db, a)["savings"] for a in rows)
        buckets.append({"label": start.strftime("%b"), "value": total, "count": len(rows)})
    return buckets


def _vendor_home(request: Request, user: User, db: Session):
    # Only auctions the bidder is actually allowed to open. Without the status
    # filter a draft the buyer had not published yet showed up here - title,
    # dates and all - and then refused to open.
    auctions = (db.query(Auction).join(Participant, Participant.auction_id == Auction.id)
                  .filter(Auction.org_id == user.org_id,
                          Participant.vendor_id == user.vendor_id,
                          Auction.status.in_(VENDOR_VISIBLE))
                  .order_by(Auction.start_at.desc()).all())
    live, upcoming, finished = [], [], []
    for auction in auctions:
        ranks = [engine.vendor_rank(db, l.id, user.vendor_id) for l in auction.lines]
        ranks = [r for r in ranks if r]
        row = {"auction": auction, "best_rank": min(ranks) if ranks else None,
               "lines_bid": len(ranks), "lines": len(auction.lines)}
        if auction.status == AuctionStatus.LIVE:
            live.append(row)
        elif auction.status == AuctionStatus.SCHEDULED:
            upcoming.append(row)
        else:
            finished.append(row)
    wins = (db.query(Award).join(Auction, Auction.id == Award.auction_id)
              .filter(Award.vendor_id == user.vendor_id,
                      Auction.org_id == user.org_id).all())
    metrics = {
        "live": len(live), "upcoming": len(upcoming),
        "won_value": sum(a.total for a in wins), "won_lines": len(wins),
        "l1_now": sum(1 for row in live if row["best_rank"] == 1),
        "bids": (db.query(Bid).join(Auction, Auction.id == Bid.auction_id)
                   .filter(Bid.vendor_id == user.vendor_id, Bid.withdrawn.is_(False),
                           Auction.org_id == user.org_id).count()),
    }
    return render(request, "dashboard_vendor.html",
                  {"metrics": metrics, "live": live, "upcoming": upcoming,
                   "finished": finished[:8]},
                  user=user, db=db, help_key="auction_detail_vendor")
