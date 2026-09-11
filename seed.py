"""Create a demo database you can click through immediately.

    python seed.py            # build data/reverse_auction.db
    python seed.py --reset    # wipe it first

Sign in with any of the accounts printed at the end (password: demo1234).
"""
from __future__ import annotations

import argparse
import pathlib
import random
from datetime import datetime, timedelta

from app import config
from app.db import Base, SessionLocal, engine
from app.models import (Auction, AuctionLine, AuctionStatus, Award, Bid, DecrementType,
                        EmailMessage, Item, Message, Notification, Participant, Role, Unit,
                        User, Vendor, AuditLog)
from app.security import hash_password
from app.utils import alias_for

PASSWORD = "demo1234"
random.seed(7)

#: Freight and duty as the buyer has them on file for each supplier. One is
#: local, one is three states away - which is the whole point of comparing on
#: the delivered price.
ADDERS = {
    "Sunrise Packaging Pvt Ltd": {"default_freight": 0.9, "default_freight_basis": "unit"},
    "Deccan Industrial Supplies": {"default_freight": 2.4, "default_freight_basis": "unit",
                                   "default_packaging": 0.35},
    "Nagpur Metal Works": {"default_freight": 3.1, "default_freight_basis": "unit"},
    "Coastal Logistics & Trading": {"default_freight": 1.0, "default_freight_basis": "unit",
                                    "default_duty": 2.0, "default_duty_basis": "percent"},
}


def reset() -> None:
    """Start from nothing. For SQLite we remove the file - dropping the tables
    trips over the mutual users <-> vendors foreign keys."""
    url = str(engine.url)
    if url.startswith("sqlite") and engine.url.database:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            path = pathlib.Path(engine.url.database + suffix)
            if path.exists():
                path.unlink()
        return
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
    db.add(buyer)
    db.flush()

    #  name, login email, contact person, extra people who also get every email
    vendor_specs = [
        ("Sunrise Packaging Pvt Ltd", "vendor1@demo.in", "Ravi Menon",
         "sales@sunrisepack.example\nowner@sunrisepack.example"),
        ("Deccan Industrial Supplies", "vendor2@demo.in", "Farah Sheikh",
         "tenders@deccanind.example"),
        ("Nagpur Metal Works", "vendor3@demo.in", "Vikram Joshi", ""),
        ("Coastal Logistics & Trading", "vendor4@demo.in", "Meera Nair",
         "bids@coastal.example"),
    ]
    vendors, vendor_users = [], []
    for name, email, contact, extra in vendor_specs:
        vendor = Vendor(name=name, email=email, contact_person=contact, extra_emails=extra,
                        code=name.split()[0][:4].upper(), created_by_id=buyer.id,
                        # What it costs to get their goods here. Only used by an
                        # auction that is compared on the delivered price.
                        **ADDERS.get(name, {}))
        db.add(vendor)
        db.flush()
        user = User(name=contact, email=email, role=Role.VENDOR, vendor_id=vendor.id,
                    password_hash=hash_password(PASSWORD), onboarding_done=True)
        db.add(user)
        vendors.append(vendor)
        vendor_users.append(user)

    #  A supplier the buyer has added but who has never signed in - the normal
    #  state of a new vendor. They are invited to the scheduled auction, and
    #  their invitation email carries the link that sets their password.
    not_yet_joined = Vendor(name="Bharat Fasteners", email="sales@bharatfast.example",
                            contact_person="Anil Gupta", code="BHAR",
                            created_by_id=buyer.id, default_freight=1.5)
    db.add(not_yet_joined)
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
        overrides = kwargs.pop("overrides", {})
        kwargs_extra = {"also_invite": kwargs.pop("also_invite", [])}
        auction = Auction(
            reference=f"RA-{start.year}-{counter['n']:04d}", title=title,
            cc_emails=kwargs.pop("cc_emails", ""),
            description=kwargs.pop("description", ""), creator_id=buyer.id, status=status,
            start_at=start, end_at=end, original_end_at=end,
            decrement_type=DecrementType.ABSOLUTE,
            min_decrement=kwargs.pop("min_decrement", 1.0),
            max_decrement=kwargs.pop("max_decrement", 0.0),
            show_rank=True, show_lowest_bid=True, hide_bidder_names=True,
            auto_extend=True, extend_trigger_seconds=120, extend_by_seconds=180,
            max_extensions=5,
            # A draft has not been published, so it must not carry a publish
            # date - the app treats that as "the bidders already know".
            published_at=(None if status == AuctionStatus.DRAFT
                          else start - timedelta(days=1)),
            **kwargs)
        db.add(auction)
        db.flush()
        for item, unit, price, qty in line_specs:
            db.add(AuctionLine(auction_id=auction.id, item_id=item.id, unit_id=unit.id,
                               qty=qty, starting_price=price))
        invited = list(vendors) + kwargs_extra.get("also_invite", [])
        for index, vendor in enumerate(invited):
            part = Participant(auction_id=auction.id, vendor_id=vendor.id,
                               alias=alias_for(index),
                               notify_emails=overrides.get(vendor.id, ""))
            if auction.compare_landed:
                # Each auction keeps its own copy of the delivered costs, taken
                # from the vendor record at the moment they were invited.
                for attr, _ in (("freight", ""), ("duty", ""), ("packaging", ""),
                                ("other", "")):
                    setattr(part, attr, getattr(vendor, f"default_{attr}", 0.0) or 0.0)
                    setattr(part, f"{attr}_basis",
                            getattr(vendor, f"default_{attr}_basis", "unit") or "unit")
            db.add(part)
        db.flush()
        return auction

    def simulate(auction: Auction, rounds: int = 3) -> None:
        """Walk prices down from the ceiling, one round at a time.

        Every bid respects the auction's own minimum decrement, so the demo
        never shows a bid history the engine would have refused.
        """
        step = auction.min_decrement or 0.0
        for line in auction.lines:
            best = line.starting_price
            floor = line.starting_price * 0.7
            bidders = random.sample(vendors, k=random.choice([2, 3, 4]))
            # The clock only ever moves forward, so the saved history reads in
            # the same order the prices actually fell. Random timestamps used
            # to put a higher bid after a lower one, which made the demo look
            # like it had broken its own decrement rule.
            when = auction.start_at + timedelta(minutes=2)
            for round_no in range(rounds):
                for vendor in bidders:
                    if random.random() < 0.25 and round_no:
                        continue
                    drop = max(best * random.uniform(0.012, 0.045), step)
                    price = round(best - drop, 2)
                    if price < floor or price > best - step or price <= 0:
                        continue
                    best = price
                    when += timedelta(seconds=random.randint(40, 400))
                    user = next(u for u in vendor_users if u.vendor_id == vendor.id)
                    db.add(Bid(auction_id=auction.id, line_id=line.id, vendor_id=vendor.id,
                               user_id=user.id, unit_price=price, qty=line.qty,
                               total=round(price * line.qty, 2), created_at=when))
        db.flush()

    def award_lowest(auction: Auction, second_place_first: bool = False) -> None:
        """Award each line to one bidder, for the whole quantity - the house rule.

        The demo used to split the first line between two suppliers, which the
        app itself forbids: the award screen offers no way to do it, and
        re-saving the award silently collapsed it and changed the savings.
        ``second_place_first`` instead shows the other real case - a buyer
        choosing L2 over L1 on one item.
        """
        from app import engine as eng
        for index, line in enumerate(auction.lines):
            ranked = eng.best_per_vendor(db, line.id)
            if not ranked:
                continue
            winner = ranked[1] if (second_place_first and index == 0 and len(ranked) > 1) \
                else ranked[0]
            db.add(Award(auction_id=auction.id, line_id=line.id, vendor_id=winner.vendor_id,
                         bid_id=winner.id, qty=line.qty, unit_price=winner.unit_price,
                         total=round(line.qty * winner.unit_price, 2), awarded_by_id=buyer.id,
                         notes=("Chosen over the lowest bid on delivery lead time."
                                if winner is not ranked[0] else ""),
                         awarded_at=auction.end_at + timedelta(hours=2)))
        auction.status = AuctionStatus.AWARDED
        auction.awarded_at = auction.end_at + timedelta(hours=2)
        auction.closed_at = auction.end_at
        db.flush()

    # ------------------------------------------------------- 1 & 2: awarded history
    for weeks_ago, title, picks, second_first in [
        (6, "Corrugated packaging — Q1 volumes", [0, 1], True),
        (2, "MS angles and structural steel — March", [2], False),
    ]:
        begin = now - timedelta(weeks=weeks_ago)
        auction = make_auction(title, AuctionStatus.CLOSED, begin,
                               begin + timedelta(hours=2), [items[i] for i in picks],
                               description="Rate contract for the coming quarter.")
        simulate(auction, rounds=3)
        award_lowest(auction, second_place_first=second_first)

    # ------------------------------------------------------- 3: live right now
    live = make_auction("Corrugated boxes and stretch film — live demo",
                        AuctionStatus.LIVE, now - timedelta(minutes=25),
                        now + timedelta(hours=3), [items[0], items[1]],
                        description="Bidding is open. Lowest price per unit wins.",
                        min_decrement=0.5,
                        cc_emails="procurement.head@demo.in",
                        overrides={vendors[2].id: "tender.desk@nagpurmetal.example"})
    live.started_at = now - timedelta(minutes=25)
    simulate(live, rounds=2)

    # ------------------------------------------------------- 4: opens later today
    make_auction("Gear oil EP-320 — delivered price, opens later today",
                 AuctionStatus.SCHEDULED,
                 now + timedelta(hours=4), now + timedelta(hours=6), [items[4]],
                 description="Compared on the delivered price: each bidder's freight and duty "
                             "is added to what they bid, so a local supplier and a distant one "
                             "are judged like for like. Bharat Fasteners has been invited but "
                             "has never signed in — their invitation carries the link that "
                             "sets their password.",
                 compare_landed=True, also_invite=[not_yet_joined])

    # ------------------------------------------------------- 5: a draft, with no ceiling set
    draft = make_auction("Hydraulic hoses — draft, no ceiling set", AuctionStatus.DRAFT,
                         now + timedelta(days=2), now + timedelta(days=2, hours=2),
                         [items[3]],
                         description="An example of leaving the starting price empty: bidders "
                                     "open at whatever they like, and savings are measured from "
                                     "the highest bid received.")
    for line in draft.lines:
        line.starting_price = None

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
    for _, email, contact, _extra in vendor_specs:
        print(f"  Bidder     {email:<18} / {PASSWORD}   ({contact})")
    print("\n  Bharat Fasteners (sales@bharatfast.example) has no password yet, on purpose.")
    print("  Open the Outbox as the buyer, find their invitation, and follow the link in it")
    print("  to see how a real supplier gets in.")
    print(f"\nDatabase: {config.DATABASE_URL}\nNow run:  uvicorn app.main:app --reload\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    args = parser.parse_args()
    if args.reset:
        reset()
    build()
