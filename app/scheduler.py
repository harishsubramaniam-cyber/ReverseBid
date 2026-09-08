"""Background clock: opens auctions, closes them, and sends time-based alerts."""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timedelta

from . import config, notify
from .audit import record
from .db import SessionLocal
from .models import Auction, AuctionStatus


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
            auction.starting_soon_notified = True
            db.commit()
            notify.auction_starting_soon(db, auction)
            stats["starting_soon"] += 1

        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.SCHEDULED,
                Auction.start_at <= now).all():
            auction.status = AuctionStatus.LIVE
            auction.started_at = now
            record(db, action="auction.start", entity_type="auction", entity_id=auction.id,
                   auction_id=auction.id, detail="Opened automatically by the scheduler")
            db.commit()
            notify.auction_started(db, auction)
            stats["started"] += 1

        warn_at = now + timedelta(minutes=config.ENDING_SOON_MINUTES)
        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.LIVE,
                Auction.end_at <= warn_at,
                Auction.end_at > now,
                Auction.ending_soon_notified.is_(False)).all():
            auction.ending_soon_notified = True
            db.commit()
            notify.ending_soon(db, auction)
            stats["ending_soon"] += 1

        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.LIVE,
                Auction.end_at <= now).all():
            auction.status = AuctionStatus.CLOSED
            auction.closed_at = now
            record(db, action="auction.close", entity_type="auction", entity_id=auction.id,
                   auction_id=auction.id, detail="Closed automatically when the clock ran out")
            db.commit()
            notify.auction_closed(db, auction)
            stats["closed"] += 1
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
