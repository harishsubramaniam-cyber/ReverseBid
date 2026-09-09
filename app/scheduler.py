"""Background clock: opens auctions, closes them, and sends time-based alerts."""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timedelta

from . import config, notify
from .audit import record
from .db import SessionLocal
from .models import Auction, AuctionStatus


def _safely(what: str, action) -> bool:
    """Run one auction's step, and let the rest of the tick carry on if it fails.

    Without this, a single failure - a locked database, a mail server hiccup -
    stopped the whole pass, so auctions later in the list were never opened or
    closed and their alerts were lost.
    """
    try:
        action()
        return True
    except Exception:
        print(f"Scheduler could not {what}; will try again on the next tick.")
        traceback.print_exc()
        return False


def tick(now: datetime | None = None) -> dict:
    """One pass of the clock. Safe to call directly from tests or a cron job."""
    now = now or datetime.utcnow()
    stats = {"started": 0, "closed": 0, "starting_soon": 0, "ending_soon": 0}
    db = SessionLocal()
    try:
        soon = now + timedelta(minutes=config.STARTING_SOON_MINUTES)
        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.SCHEDULED,
                Auction.start_at <= soon,
                Auction.start_at > now,
                Auction.starting_soon_notified.is_(False)).all():
            def send_starting(auction=auction):
                # Send first, then remember we did. The other order loses the
                # alert for good if the send fails.
                notify.auction_starting_soon(db, auction)
                auction.starting_soon_notified = True
                db.commit()
            if _safely(f"warn bidders that {auction.reference} starts soon", send_starting):
                stats["starting_soon"] += 1
            else:
                db.rollback()

        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.SCHEDULED,
                Auction.start_at <= now).all():
            def open_it(auction=auction):
                auction.status = AuctionStatus.LIVE
                auction.started_at = now
                record(db, action="auction.start", entity_type="auction",
                       entity_id=auction.id, auction_id=auction.id,
                       detail="Opened automatically by the scheduler")
                db.commit()
                notify.auction_started(db, auction)
            if _safely(f"open {auction.reference}", open_it):
                stats["started"] += 1
            else:
                db.rollback()

        warn_at = now + timedelta(minutes=config.ENDING_SOON_MINUTES)
        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.LIVE,
                Auction.end_at <= warn_at,
                Auction.end_at > now,
                Auction.ending_soon_notified.is_(False)).all():
            def send_ending(auction=auction):
                notify.ending_soon(db, auction)
                auction.ending_soon_notified = True
                db.commit()
            if _safely(f"warn bidders that {auction.reference} closes soon", send_ending):
                stats["ending_soon"] += 1
            else:
                db.rollback()

        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.LIVE,
                Auction.end_at <= now).all():
            def close_it(auction=auction):
                # A bid may have extended the clock between the query and now, in
                # another session. Re-read before closing, or an auto-extension
                # would be silently thrown away.
                db.refresh(auction)
                if auction.status != AuctionStatus.LIVE or auction.end_at > datetime.utcnow():
                    return False
                auction.status = AuctionStatus.CLOSED
                auction.closed_at = now
                record(db, action="auction.close", entity_type="auction",
                       entity_id=auction.id, auction_id=auction.id,
                       detail="Closed automatically when the clock ran out")
                db.commit()
                notify.auction_closed(db, auction)
                return True
            closed = []
            if _safely(f"close {auction.reference}",
                       lambda auction=auction: closed.append(close_it(auction))):
                if closed and closed[0]:
                    stats["closed"] += 1
            else:
                db.rollback()
        return stats
    finally:
        db.close()


async def run_forever() -> None:  # pragma: no cover - background loop
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(config.SCHEDULER_INTERVAL_SECONDS)
