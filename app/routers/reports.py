from __future__ import annotations

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import reporting
from ..db import get_db
from ..models import Auction, AuctionStatus, User
from ..security import buyer_side
from ..utils import TZ
from ..web import render

router = APIRouter(prefix="/reports")

#: Far enough out that no real report needs more, and clear of the ends of the
#: calendar, where converting a local midnight to UTC overflows.
EARLIEST = date(1900, 1, 1)
LATEST = date(9000, 12, 31)


def _to_utc(value: datetime) -> datetime:
    from datetime import timezone
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _window(date_from: str, date_to: str) -> tuple[datetime, datetime, str, str]:
    """Turn the two date boxes into a UTC range, plus the local dates to show back.

    Returns (start_utc, end_utc, from_label, to_label). A date that will not
    parse falls back to the default month rather than crashing the page.
    """
    today = datetime.now(TZ).date()

    def parse(value: str, fallback):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date() if value else fallback
        except ValueError:
            return fallback

    start_date = parse(date_from, today.replace(day=1))
    end_date = parse(date_to, today)
    # A date box happily accepts year 0001 or 9999, and converting either to
    # UTC runs off the end of what a datetime can hold - which crashed the
    # reports page instead of falling back the way the docstring promises.
    start_date = min(max(start_date, EARLIEST), LATEST)
    end_date = min(max(end_date, EARLIEST), LATEST)
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    start = datetime.combine(start_date, time.min, tzinfo=TZ)
    end = datetime.combine(end_date, time.max, tzinfo=TZ)
    return (_to_utc(start), _to_utc(end),
            start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))


@router.get("")
def reports_home(request: Request, date_from: str = "", date_to: str = "",
                 include_closed: str = "", user: User = Depends(buyer_side),
                 db: Session = Depends(get_db)):
    start, end, from_label, to_label = _window(date_from, date_to)
    statuses = [AuctionStatus.AWARDED]
    if include_closed:
        statuses.append(AuctionStatus.CLOSED)
    data = reporting.total_savings(db, start, end, tuple(statuses))
    auctions = (db.query(Auction)
                  .filter(Auction.status.in_([AuctionStatus.CLOSED, AuctionStatus.AWARDED,
                                              AuctionStatus.LIVE]))
                  .order_by(Auction.start_at.desc()).limit(100).all())
    return render(request, "reports.html",
                  {"data": data, "auctions": auctions,
                   "date_from": from_label, "date_to": to_label,
                   "include_closed": bool(include_closed)},
                  user=user, db=db, help_key="reports")


@router.get("/savings.{fmt}")
def savings_download(fmt: str, date_from: str = "", date_to: str = "",
                     include_closed: str = "", user: User = Depends(buyer_side),
                     db: Session = Depends(get_db)):
    start, end, from_label, to_label = _window(date_from, date_to)
    statuses = [AuctionStatus.AWARDED] + ([AuctionStatus.CLOSED] if include_closed else [])
    data = reporting.total_savings(db, start, end, tuple(statuses))
    stamp = f"{from_label.replace('-', '')}-{to_label.replace('-', '')}"
    if fmt == "csv":
        return Response(reporting.savings_csv(data), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="total-savings-{stamp}.csv"'})
    if fmt == "pdf":
        return Response(reporting.savings_pdf(data), media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'attachment; filename="total-savings-{stamp}.pdf"'})
    raise HTTPException(404, "Choose csv or pdf.")


@router.get("/auction/{auction_id}")
def auction_report(auction_id: int, request: Request, user: User = Depends(buyer_side),
                   db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    data = reporting.auction_summary_report(db, auction)
    return render(request, "report_auction.html", {"data": data, "auction": auction},
                  user=user, db=db, help_key="reports")


@router.get("/auction/{auction_id}/export/{fmt}")
def auction_download(auction_id: int, fmt: str, user: User = Depends(buyer_side),
                     db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    if not auction:
        raise HTTPException(404, "That auction does not exist.")
    data = reporting.auction_summary_report(db, auction)
    if fmt == "csv":
        return Response(reporting.auction_csv(db, data), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{auction.reference}-summary.csv"'})
    if fmt == "pdf":
        return Response(reporting.auction_pdf(data), media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{auction.reference}-summary.pdf"'})
    raise HTTPException(404, "Choose csv or pdf.")
