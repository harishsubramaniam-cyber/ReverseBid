"""Create a demo database you can click through immediately.

    python seed.py            # build data/reverse_auction.db
    python seed.py --reset    # wipe it first

Sign in with any of the accounts printed at the end (password: demo1234).
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta

from app import config
from app.db import Base, SessionLocal, engine
from app.models import (Approval, ApprovalStatus, Auction, AuctionLine, AuctionStatus, Award, Bid,
                        DecrementType, EmailMessage, Item, Message, Notification, Participant,
                        Role, Unit, User, Vendor, AuditLog)
from app.security import hash_password
from app.utils import alias_for

PASSWORD = "demo1234"
random.seed(7)


def reset() -> None:
    Base.metadata.drop_all(bind=engine)


def build() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    if db.query(User).count():
        print("Database already has data — use --reset to start again.")
        db.close()
        return

    # ---------------------------------------------------------------- people
    buyer = User(name="Priya Raman", email="buyer@demo.in", role=Role.BUYER,
                 password_hash=hash_password(PASSWORD), onboarding_done=True)
    approver = User(name="Anand Kulkarni", email="approver@demo.in", role=Role.APPROVER,
                    password_hash=hash_password(PASSWORD), onboarding_done=True)
    db.add_all([buyer, approver])
    db.flush()

    vendor_specs = [
        ("Sunrise Packaging Pvt Ltd", "vendor1@demo.in", "Ravi Menon"),
        ("Deccan Industrial Supplies", "vendor2@demo.in", "Farah Sheikh"),
        ("Nagpur Metal Works", "vendor3@demo.in", "Vikram Joshi"),
        ("Coastal Logistics & Trading", "vendor4@demo.in", "Meera Nair"),
    ]
    vendors, vendor_users = [], []
    for name, email, contact in vendor_specs:
        vendor = Vendor(name=name, email=email, contact_person=contact,
                        code=name.split()[0][:4].upper(), created_by_id=buyer.id)
        db.add(vendor)
        db.flush()
        user = User(name=contact, email=email, role=Role.VENDOR, vendor_id=vendor.id,
                    password_hash=hash_password(PASSWORD), onboarding_done=True)
        db.add(user)
        vendors.append(vendor)
        vendor_users.append(user)
    db.flush()

    # ---------------------------------------------------------------- masters
    units = {}
    for code, name in [("NOS", "Numbers"), ("KG", "Kilogram"), ("MT", "Metric tonne"),
                       ("MTR", "Metre"), ("LTR", "Litre")]:
        unit = Unit(code=code, name=name)
        db.add(unit)
        units[code] = unit
    db.flush()

    item_specs = [
        ("Corrugated box 400×300×250 mm, 5-ply", "NOS", "Packaging", 42.0, 25000),
        ("Stretch wrap film, 23 micron", "KG", "Packaging", 168.0, 1200),
        ("MS angle 50×50×6 mm", "MT", "Steel", 62000.0, 40),
        ("Hydraulic hose assembly, 1 inch", "NOS", "Spares", 2350.0, 180),
        ("Industrial gear oil EP-320", "LTR", "Consumables", 285.0, 2400),
        ("Cotton wiping cloth", "KG", "Consumables", 95.0, 800),
    ]
    items = []
    for name, unit_code, category, price, qty in item_specs:
        item = Item(name=name, category=category, default_unit_id=units[unit_code].id,
                    created_by_id=buyer.id)
        db.add(item)
        db.flush()
        items.append((item, units[unit_code], price, qty))
    db.commit()

    now = datetime.utcnow()
    counter = {"n": 0}

    def make_auction(title, status, start, end, line_specs, **kwargs) -> Auction:
        counter["n"] += 1
        auction = Auction(
            reference=f"RA-{start.year}-{counter['n']:04d}", title=title,
            description=kwargs.pop("description", ""), creator_id=buyer.id, status=status,
            start_at=start, end_at=end, original_end_at=end,
            decrement_type=DecrementType.ABSOLUTE,
            min_decrement=kwargs.pop("min_decrement", 1.0),
            max_decrement=kwargs.pop("max_decrement", 0.0),
            show_rank=True, show_lowest_bid=True, hide_bidder_names=True,
            auto_extend=True, extend_trigger_seconds=120, extend_by_seconds=180,
            max_extensions=5, published_at=start - timedelta(days=1), **kwargs)
        db.add(auction)
        db.flush()
        for item, unit, price, qty in line_specs:
            db.add(AuctionLine(auction_id=auction.id, item_id=item.id, unit_id=unit.id,
                               qty=qty, starting_price=price))
        for index, vendor in enumerate(vendors):
            db.add(Participant(auction_id=auction.id, vendor_id=vendor.id,
                               alias=alias_for(index)))
        db.flush()
        return auction

    def simulate(auction: Auction, rounds: int = 3) -> None:
        """Walk prices down from the ceiling, one round at a time."""
        base = auction.start_at + timedelta(minutes=2)
        for line in auction.lines:
            best = line.starting_price
            bidders = random.sample(vendors, k=random.choice([2, 3, 4]))
            for round_no in range(rounds):
                for vendor in bidders:
                    if random.random() < 0.25 and round_no:
                        continue
                    drop = best * random.uniform(0.012, 0.045)
                    price = round(max(best - drop, line.starting_price * 0.7), 2)
                    if price >= best:
                        continue
                    best = price
                    user = next(u for u in vendor_users if u.vendor_id == vendor.id)
                    db.add(Bid(auction_id=auction.id, line_id=line.id, vendor_id=vendor.id,
                               user_id=user.id, unit_price=price, qty=line.qty,
                               total=round(price * line.qty, 2),
                               created_at=base + timedelta(minutes=round_no * 12 +
                                                           random.randint(0, 9))))
        db.flush()

    def award_lowest(auction: Auction, split_first: bool = False) -> None:
        from app import engine as eng
        for index, line in enumerate(auction.lines):
            ranked = eng.best_per_vendor(db, line.id)
            if not ranked:
                continue
            if split_first and index == 0 and len(ranked) > 1:
                halves = [(ranked[0], line.qty * 0.6), (ranked[1], line.qty * 0.4)]
            else:
                halves = [(ranked[0], line.qty)]
            for bid, qty in halves:
                db.add(Award(auction_id=auction.id, line_id=line.id, vendor_id=bid.vendor_id,
                             bid_id=bid.id, qty=qty, unit_price=bid.unit_price,
                             total=round(qty * bid.unit_price, 2), awarded_by_id=buyer.id,
                             awarded_at=auction.end_at + timedelta(hours=2)))
        auction.status = AuctionStatus.AWARDED
        auction.awarded_at = auction.end_at + timedelta(hours=2)
        auction.closed_at = auction.end_at
        db.flush()

    # ------------------------------------------------------- history (awarded)
    for weeks_ago, title, picks, split in [
        (14, "Corrugated packaging — Q1 volumes", [0, 1], True),
        (9, "MS angles and structural steel — March", [2], False),
        (6, "Hydraulic spares — annual rate contract", [3], False),
        (3, "Lubricants and consumables — Q2", [4, 5], True),
        (1, "Packaging top-up — May", [0, 1], False),
    ]:
        start = now - timedelta(weeks=weeks_ago)
        auction = make_auction(title, AuctionStatus.CLOSED, start,
                               start + timedelta(hours=2),
                               [items[i] for i in picks],
                               description="Rate contract for the coming quarter.")
        simulate(auction, rounds=random.choice([2, 3, 4]))
        award_lowest(auction, split_first=split)

    # ------------------------------------------------------- live right now
    live = make_auction("Corrugated boxes and stretch film — live demo",
                        AuctionStatus.LIVE, now - timedelta(minutes=25),
                        now + timedelta(hours=3), [items[0], items[1]],
                        description="Bidding is open. Lowest price per unit wins.",
                        min_decrement=0.5)
    live.started_at = now - timedelta(minutes=25)
    simulate(live, rounds=2)

    # ------------------------------------------------------- scheduled
    make_auction("Gear oil EP-320 — starts shortly", AuctionStatus.SCHEDULED,
                 now + timedelta(hours=4), now + timedelta(hours=6), [items[4]],
                 description="Opens in a few hours.")

    # ------------------------------------------------------- awaiting approval
    pending = make_auction("Wiping cloth — needs sign-off", AuctionStatus.PENDING_APPROVAL,
                           now + timedelta(days=1), now + timedelta(days=1, hours=2),
                           [items[5]], requires_approval=True)
    db.add(Approval(auction_id=pending.id, requested_by_id=buyer.id,
                    status=ApprovalStatus.PENDING))

    # ------------------------------------------------------- a draft
    make_auction("Draft — hydraulic hoses, second half", AuctionStatus.DRAFT,
                 now + timedelta(days=3), now + timedelta(days=3, hours=2), [items[3]])

    # a conversation on the live auction
    db.add(Message(auction_id=live.id, vendor_id=vendors[0].id,
                   sender_id=vendor_users[0].id,
                   body="Is the 5-ply specification firm, or would 3-ply be acceptable?"))
    db.add(Message(auction_id=live.id, vendor_id=vendors[0].id, sender_id=buyer.id,
                   body="5-ply only, please — it has to survive stacking four high."))

    db.commit()
    db.close()

    print("\nDemo data ready.\n")
    print(f"  Buyer      buyer@demo.in      / {PASSWORD}")
    print(f"  Approver   approver@demo.in   / {PASSWORD}")
    for _, email, contact in vendor_specs:
        print(f"  Bidder     {email:<18} / {PASSWORD}   ({contact})")
    print(f"\nDatabase: {config.DATABASE_URL}\nNow run:  uvicorn app.main:app --reload\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    args = parser.parse_args()
    if args.reset:
        reset()
    build()
