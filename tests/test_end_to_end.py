"""End-to-end walk through a whole reverse auction, emails included.

Run with:  python -m pytest -q      (or simply: python tests/test_end_to_end.py)

It uses a throwaway database, so it never touches your real data.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-test-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ.pop("RA_SMTP_HOST", None)          # force dev-outbox mode
os.environ["RA_TIMEZONE"] = "UTC"

from fastapi.testclient import TestClient           # noqa: E402

from app import mailer, scheduler                   # noqa: E402
from app.db import Base, SessionLocal, engine       # noqa: E402
from app.main import app                            # noqa: E402
from app.models import (Auction, AuctionStatus, Award, Bid, EmailMessage, Item, Role,  # noqa: E402
                        Unit, User, Vendor)
from app.security import hash_password              # noqa: E402

Base.metadata.create_all(bind=engine)
PASSWORD = "test1234"
FAILS: list[str] = []


def check(label: str, condition: bool, extra: str = "") -> None:
    print(("  ✓ " if condition else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not condition:
        FAILS.append(label)


def client_for(email: str) -> TestClient:
    c = TestClient(app, base_url="http://test")
    r = c.post("/login", data={"email": email, "password": PASSWORD, "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def local(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def emails(event: str = "") -> list[EmailMessage]:
    db = SessionLocal()
    try:
        q = db.query(EmailMessage)
        if event:
            q = q.filter(EmailMessage.event == event)
        return q.all()
    finally:
        db.close()


def main() -> int:
    db = SessionLocal()

    print("\n1. Accounts and masters")
    buyer = User(name="Test Buyer", email="buyer@test.local", role=Role.BUYER,
                 password_hash=hash_password(PASSWORD))
    db.add(buyer)
    unit = Unit(code="NOS", name="Numbers")
    db.add(unit)
    db.flush()
    vendors, vendor_users = [], []
    for i in (1, 2, 3):
        vendor = Vendor(name=f"Vendor {i}", email=f"v{i}@test.local",
                        extra_emails=f"sales{i}@test.local")
        db.add(vendor)
        db.flush()
        user = User(name=f"Bidder {i}", email=f"v{i}@test.local", role=Role.VENDOR,
                    vendor_id=vendor.id, password_hash=hash_password(PASSWORD))
        db.add(user)
        vendors.append(vendor)
        vendor_users.append(user)
    item = Item(name="Test widget", default_unit_id=unit.id)
    db.add(item)
    db.commit()
    check("buyer, 3 vendors, item and unit created", db.query(User).count() == 4)

    buyer_c = client_for("buyer@test.local")
    check("buyer dashboard loads", buyer_c.get("/").status_code == 200)
    check("masters page loads", buyer_c.get("/masters").status_code == 200)

    print("\n2. Inline (Odoo-style) create from the auction form")
    r = buyer_c.post("/masters/quick/vendor",
                     data={"name": "Inline Vendor", "email": "inline@test.local"})
    check("inline vendor create returns json", r.status_code == 200 and "id" in r.json())
    r = buyer_c.post("/masters/quick/unit", data={"code": "kg"})
    check("inline unit create upper-cases the code", r.json()["label"] == "KG")

    print("\n3. Create and publish an auction")
    start = datetime.utcnow() - timedelta(minutes=1)
    end = datetime.utcnow() + timedelta(minutes=30)
    form = {
        "title": "E2E widgets", "description": "test", "terms": "",
        "start_at": local(start), "end_at": local(end),
        "decrement_type": "absolute", "min_decrement": "5", "max_decrement": "200",
        "show_rank": "on", "show_lowest_bid": "on", "hide_bidder_names": "on",
        "auto_extend": "on", "extend_trigger_minutes": "2", "extend_by_minutes": "3",
        "max_extensions": "2",
        "line_item_id": str(item.id), "line_unit_id": str(unit.id),
        "line_qty": "100", "line_price": "1000", "line_spec": "",
        "vendor_ids": [str(v.id) for v in vendors],
        "cc_emails": "finance@buyer.local, boss@buyer.local",
        f"notify_emails_{vendors[2].id}": "tender.desk@v3.local",
    }
    r = buyer_c.post("/auctions/new", data=form, follow_redirects=False)
    check("auction created", r.status_code == 303, r.headers.get("location", ""))
    auction = db.query(Auction).order_by(Auction.id.desc()).first()
    db.refresh(auction)
    check("three bidders invited", len(auction.participants) == 3)
    check("copy list saved", len(auction.cc_emails.split()) == 2, auction.cc_emails.replace("\n", " "))
    override = [p for p in auction.participants if p.vendor_id == vendors[2].id][0]
    check("per-auction address override saved",
          override.notify_emails.strip() == "tender.desk@v3.local")
    check("baseline is qty x starting price", auction.baseline_value == 100_000)

    r = buyer_c.post(f"/auctions/{auction.id}/publish", follow_redirects=False)
    check("published", r.status_code == 303)
    mailer.flush()
    invited = emails("invited")
    to = sorted(m.to_email for m in invited)
    # v1 and v2: primary + their extra contact. v3: the override replaces both.
    # Plus the two people on the buyer's copy list and the creator.
    check("vendor's extra contact was emailed too", "sales1@test.local" in to, ", ".join(to))
    check("per-auction override replaced the vendor's own addresses",
          "tender.desk@v3.local" in to and "v3@test.local" not in to)
    check("the buyer's copy list was told the auction is published",
          "finance@buyer.local" in to and "boss@buyer.local" in to)
    check("invitations went to every address", len(invited) == 8, f"{len(invited)} sent")

    print("\n4. The scheduler opens the auction")
    scheduler.tick()
    db.refresh(auction)
    check("auction went live automatically", auction.status == AuctionStatus.LIVE)
    mailer.flush()
    check("'bidding is open' emailed to all five bidder addresses",
          len(emails("started")) == 5, f"{len(emails('started'))} sent")

    print("\n5. Bidding rules")
    line = auction.lines[0]
    v1 = client_for("v1@test.local")
    v2 = client_for("v2@test.local")
    v3 = client_for("v3@test.local")

    def bid(c, price):
        return c.post(f"/auctions/{auction.id}/bid",
                      data={"line_id": line.id, "unit_price": str(price)},
                      follow_redirects=False)

    bid(v1, 1200)
    check("bid above the ceiling is rejected", db.query(Bid).count() == 0)
    bid(v1, 990)
    check("first bid at/below the ceiling is accepted", db.query(Bid).count() == 1)
    bid(v2, 988)
    check("bid not beating the minimum decrement is rejected", db.query(Bid).count() == 1)
    bid(v2, 985)
    check("bid meeting the decrement is accepted", db.query(Bid).count() == 2)
    bid(v3, 700)
    check("drop larger than the maximum decrement is rejected", db.query(Bid).count() == 2)
    bid(v1, 991)
    check("a bidder cannot raise their own price", db.query(Bid).count() == 2)
    bid(v3, 970)
    check("third bidder accepted", db.query(Bid).count() == 3)

    from app.engine import best_per_vendor, vendor_rank
    ranked = best_per_vendor(db, line.id)
    check("L1 is the lowest price", ranked[0].unit_price == 970)
    check("ranks are ascending by price",
          [b.unit_price for b in ranked] == sorted(b.unit_price for b in ranked))
    check("vendor 1 sits at L3", vendor_rank(db, line.id, vendors[0].id) == 3)

    mailer.flush()
    outbid_to = {m.to_email for m in emails("outbid")}
    check("outbid alerts were emailed", len(emails("outbid")) >= 2,
          f"{len(emails('outbid'))} sent")
    check("outbid alert reached the vendor's second contact",
          "sales1@test.local" in outbid_to, ", ".join(sorted(outbid_to)))
    check("bid confirmations were emailed", len(emails("bid_received")) == 3)

    print("\n6. Visibility rules")
    page = v2.get(f"/auctions/{auction.id}").text
    check("bidder does not see other bidders' company names", "Vendor 3" not in page)
    check("bidder sees the current lowest price", "970" in page)
    buyer_page = buyer_c.get(f"/auctions/{auction.id}").text
    check("buyer does see real bidder names", "Vendor 3" in buyer_page)

    print("\n7. Withdraw, conversation, auto-extension")
    last = db.query(Bid).order_by(Bid.id.desc()).first()
    v3.post(f"/auctions/{auction.id}/bids/{last.id}/withdraw",
            data={"reason": "typo"}, follow_redirects=False)
    db.expire_all()
    ranked = best_per_vendor(db, line.id)
    check("withdrawn bid drops out of the ranking", ranked[0].unit_price == 985)

    v1.post(f"/auctions/{auction.id}/messages", data={"body": "Is 3-ply acceptable?"},
            follow_redirects=False)
    mailer.flush()
    check("message emailed to the buyer", len(emails("message")) == 1)

    auction.end_at = datetime.utcnow() + timedelta(seconds=60)   # inside the trigger window
    db.commit()
    before = auction.end_at
    bid(v1, 980)
    db.refresh(auction)
    check("late bid pushed the finish line back", auction.end_at > before,
          f"+{(auction.end_at - before).total_seconds():.0f}s")
    check("extension counted", auction.extensions_used == 1)
    mailer.flush()
    check("extension emailed to everyone", len(emails("extended")) >= 4)

    print("\n8. Close and award")
    buyer_c.post(f"/auctions/{auction.id}/close-now", follow_redirects=False)
    db.refresh(auction)
    check("auction closed", auction.status == AuctionStatus.CLOSED)
    mailer.flush()
    closed_to = {m.to_email for m in emails("closed")}
    check("closure emailed", len(emails("closed")) >= 4)
    check("copy list told when bidding closed", "finance@buyer.local" in closed_to)

    check("award screen loads", buyer_c.get(f"/auctions/{auction.id}/award").status_code == 200)
    # split the line: 60 to the L1 bidder, 40 to the runner-up
    ranked = best_per_vendor(db, line.id)
    r = buyer_c.post(f"/auctions/{auction.id}/award", follow_redirects=False, data={
        "award_line_id": [str(line.id), str(line.id)],
        "award_vendor_id": [str(ranked[0].vendor_id), str(ranked[1].vendor_id)],
        "award_qty": ["60", "40"],
        "award_price": [str(ranked[0].unit_price), str(ranked[1].unit_price)],
        "award_note": ["", ""],
    })
    check("award accepted", r.status_code == 303, r.headers.get("location", ""))
    db.expire_all()
    awards = db.query(Award).all()
    check("line split across two vendors", len(awards) == 2)
    check("awarded quantity equals the line quantity", sum(a.qty for a in awards) == 100)
    db.refresh(auction)
    check("auction marked as awarded", auction.status == AuctionStatus.AWARDED)

    r = buyer_c.post(f"/auctions/{auction.id}/award", follow_redirects=False, data={
        "award_line_id": [str(line.id)], "award_vendor_id": [str(ranked[0].vendor_id)],
        "award_qty": ["500"], "award_price": ["900"], "award_note": [""]})
    check("over-awarding is refused", r.status_code == 400)

    mailer.flush()
    awarded_to = {m.to_email for m in emails("awarded")}
    check("winners emailed at every one of their addresses", len(awarded_to) >= 4,
          ", ".join(sorted(awarded_to)))
    check("copy list got the award summary", "boss@buyer.local" in awarded_to)
    check("the bidder who lost was emailed too", len(emails("not_awarded")) >= 1)

    print("\n8b. Editing the recipients afterwards")
    r = buyer_c.post(f"/masters/vendors/{vendors[0].id}/emails", follow_redirects=False,
                     data={"email": "v1@test.local",
                           "extra_emails": "sales1@test.local\nowner1@test.local"})
    check("vendor recipient list can be edited", r.status_code == 303)
    db.expire_all()
    from app.notify import vendor_recipients
    addresses = {r.email for r in vendor_recipients(db, vendors[0].id) if r.email}
    check("all three addresses now on the vendor",
          addresses == {"v1@test.local", "sales1@test.local", "owner1@test.local"},
          ", ".join(sorted(addresses)))
    r = buyer_c.post(f"/masters/vendors/{vendors[0].id}/emails", follow_redirects=False,
                     data={"email": "v1@test.local", "extra_emails": "not-an-email"})
    db.expire_all()
    still = {r.email for r in vendor_recipients(db, vendors[0].id) if r.email}
    check("a typo in an address is refused, list unchanged", still == addresses)

    print("\n9. Savings and reports")
    from app.engine import auction_summary
    summary = auction_summary(db, auction)
    expected = 100_000 - (60 * ranked[0].unit_price + 40 * ranked[1].unit_price)
    check("savings computed from the awarded prices",
          abs(summary["savings"] - expected) < 0.01, f"{summary['savings']:.2f}")
    check("savings percentage is sensible", 0 < summary["savings_pct"] < 100)

    today = datetime.utcnow().strftime("%Y-%m-%d")
    span = f"?date_from={(datetime.utcnow() - timedelta(days=2)):%Y-%m-%d}&date_to={today}"
    check("reports page loads", buyer_c.get("/reports" + span).status_code == 200)
    r = buyer_c.get("/reports/savings.pdf" + span)
    check("total savings PDF downloads",
          r.status_code == 200 and r.content[:4] == b"%PDF", f"{len(r.content)} bytes")
    r = buyer_c.get("/reports/savings.csv" + span)
    check("total savings CSV downloads",
          r.status_code == 200 and b"Total Savings" in r.content)
    r = buyer_c.get(f"/reports/auction/{auction.id}/export/pdf")
    check("auction summary PDF downloads",
          r.status_code == 200 and r.content[:4] == b"%PDF", f"{len(r.content)} bytes")
    r = buyer_c.get(f"/reports/auction/{auction.id}/export/csv")
    check("auction summary CSV lists every bid", r.status_code == 200 and
          r.content.count(b"\n") > 8)
    check("auction summary page loads",
          buyer_c.get(f"/reports/auction/{auction.id}").status_code == 200)

    print("\n10. Emails, audit trail and the rest of the app")
    mailer.flush()
    total = emails()
    check("every email was delivered to the outbox",
          all(m.status == "outbox" for m in total), f"{len(total)} messages")
    check(".eml files written to disk",
          len(list((Path(TMP) / "outbox").glob("*.eml"))) == len(total))
    check("outbox page loads", buyer_c.get("/outbox").status_code == 200)
    check("outbox message renders", buyer_c.get(f"/outbox/{total[0].id}").status_code == 200)

    from app.audit import for_auction
    logs = for_auction(db, auction.id)
    actions = {log.action for log in logs}
    check("audit trail recorded create/publish/bid/award",
          {"auction.create", "auction.publish", "bid.place", "auction.award"} <= actions,
          ", ".join(sorted(actions)))
    check("history tab loads",
          buyer_c.get(f"/auctions/{auction.id}?tab=history").status_code == 200)
    check("vendor dashboard loads", v1.get("/").status_code == 200)
    check("vendor cannot open the award screen",
          v1.get(f"/auctions/{auction.id}/award").status_code == 403)
    check("assistant answers a question",
          "lowest" in buyer_c.post("/assistant/ask",
                                   data={"question": "what is a reverse auction?"}).text.lower())
    check("health check reports outbox mode",
          buyer_c.get("/healthz").json()["email_mode"] == "outbox")

    db.close()
    print("\n" + "-" * 60)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All checks passed.")
    return 0


def test_end_to_end():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
