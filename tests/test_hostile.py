"""Adversarial pass: every screen, every role, and the inputs a person can get wrong.

Nothing here should ever produce a 500 or leak another bidder's identity.

    python tests/test_hostile.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-hostile-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_TIMEZONE"] = "Asia/Kolkata"      # deliberately not UTC
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient           # noqa: E402

from app import mailer, scheduler                   # noqa: E402
from app.db import Base, SessionLocal, engine       # noqa: E402
from app.main import app                            # noqa: E402
from app.models import (Auction, AuctionLine, AuctionStatus, Award, Bid, Item, Participant,  # noqa: E402
                        Role, Unit, User, Vendor)
from app.security import hash_password              # noqa: E402

Base.metadata.create_all(bind=engine)
PW = "test1234"
FAILS: list[str] = []
BROWSER = {"accept": "text/html,application/xhtml+xml"}


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def login(email):
    c = TestClient(app, base_url="http://test")
    c.post("/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False)
    c.headers.update(BROWSER)
    return c


def flash_of(response) -> str:
    import json
    from http.cookies import SimpleCookie
    header = response.headers.get("set-cookie", "")
    if "ra_flash" not in header:
        return ""
    jar = SimpleCookie()
    jar.load(header)
    try:
        return json.loads(jar["ra_flash"].value)["m"]
    except Exception:
        return ""


def build_world(db):
    buyer = User(name="Buyer One", email="buyer@t.local", role=Role.BUYER,
                 password_hash=hash_password(PW))
    db.add(buyer)
    unit = Unit(code="NOS")
    item = Item(name="Widget")
    db.add_all([unit, item])
    db.flush()
    vendors, users = [], []
    for i in (1, 2, 3):
        v = Vendor(name=f"Acme {i}", email=f"v{i}@t.local")
        db.add(v)
        db.flush()
        u = User(name=f"Bidder {i}", email=f"v{i}@t.local", role=Role.VENDOR,
                 vendor_id=v.id, password_hash=hash_password(PW))
        db.add(u)
        vendors.append(v)
        users.append(u)
    db.commit()
    return buyer, vendors, item, unit


def make_auction(db, buyer, vendors, item, unit, *, status=AuctionStatus.LIVE,
                 ceiling=100.0, minutes=60, invited=None, title="Hostile"):
    now = datetime.utcnow()
    a = Auction(reference=f"RA-H-{datetime.utcnow().timestamp():.6f}", title=title,
                creator_id=buyer.id, status=status,
                start_at=now - timedelta(minutes=5), end_at=now + timedelta(minutes=minutes),
                original_end_at=now + timedelta(minutes=minutes), min_decrement=1,
                max_decrement=0, auto_extend=True, extend_trigger_seconds=120,
                extend_by_seconds=180, max_extensions=3)
    db.add(a)
    db.flush()
    db.add(AuctionLine(auction_id=a.id, item_id=item.id, unit_id=unit.id, qty=10,
                       starting_price=ceiling))
    for v in (invited if invited is not None else vendors):
        db.add(Participant(auction_id=a.id, vendor_id=v.id))
    db.commit()
    db.refresh(a)
    return a


def main() -> int:
    db = SessionLocal()
    buyer, vendors, item, unit = build_world(db)
    b = login("buyer@t.local")
    v1, v2, v3 = (login(f"v{i}@t.local") for i in (1, 2, 3))

    # ------------------------------------------------------------------ 1
    print("\n1. Every screen loads for every role")
    buyer_pages = ["/", "/auctions", "/auctions/new", "/masters", "/masters?tab=items",
                   "/masters?tab=units", "/reports", "/outbox", "/notifications"]
    for path in buyer_pages:
        r = b.get(path)
        check(f"buyer GET {path}", r.status_code == 200, str(r.status_code))
    for path in ["/", "/auctions", "/notifications"]:
        r = v1.get(path)
        check(f"bidder GET {path}", r.status_code == 200, str(r.status_code))
    for path in ["/masters", "/reports", "/outbox", "/auctions/new"]:
        r = v1.get(path)
        check(f"bidder is kept out of {path}", r.status_code == 403, str(r.status_code))
    r = TestClient(app, headers=BROWSER).get("/", follow_redirects=False)
    check("a signed-out visitor is sent to sign in",
          r.status_code == 303 and "/login" in r.headers.get("location", ""),
          str(r.status_code))
    r = TestClient(app).get("/")          # an API-style caller, no HTML accepted
    check("...and an API caller gets JSON, not a redirect loop",
          r.status_code == 401 and r.json().get("error"))

    # ------------------------------------------------------------------ 2
    print("\n2. Junk in the URL never breaks a page")
    junk = ["/auctions?status=Draft", "/auctions?status=<script>", "/auctions?q=%27%20OR%201=1",
            "/reports?date_from=31-02-2026", "/reports?date_from=hello&date_to=2026",
            "/reports?date_from=2026-12-01&date_to=2026-01-01",
            "/reports/savings.csv?date_from=nonsense",
            "/reports/auction/99999", "/reports/auction/abc",
            "/auctions/0", "/auctions/-1", "/auctions/99999999999",
            "/outbox/999999", "/masters?tab=nope"]
    for path in junk:
        r = b.get(path)
        check(f"GET {path}", r.status_code in (200, 303, 404, 422), str(r.status_code))
        check(f"  ...and it is a page, not a stack trace", "Traceback" not in r.text)

    # ------------------------------------------------------------------ 3
    print("\n3. Bidding: the nasty numbers")
    auction = make_auction(db, buyer, vendors, item, unit)
    line = auction.lines[0]

    def bid(client, price, line_id=None):
        return client.post(f"/auctions/{auction.id}/bid",
                           data={"line_id": str(line_id or line.id), "unit_price": str(price)},
                           follow_redirects=False)

    for price, label in [("inf", "infinity"), ("Infinity", "Infinity"), ("nan", "not-a-number"),
                         ("-5", "a negative price"), ("0", "zero"), ("abc", "letters"),
                         ("", "an empty box"), ("1e309", "an overflowing exponent"),
                         ("99999999999999999999", "an absurd number"),
                         ("１２３", "full-width digits")]:
        r = bid(v1, price)
        rejected = db.query(Bid).count() == 0
        check(f"{label} is refused, in words", rejected and flash_of(r) != "",
              flash_of(r)[:46] or f"HTTP {r.status_code}")

    r = bid(v1, "95.005")
    check("a fractional paisa is accepted and rounded", db.query(Bid).count() == 1,
          str(db.query(Bid).first().unit_price if db.query(Bid).count() else ""))

    r = bid(v1, "95.00")
    check("bidding the same price again is refused",
          db.query(Bid).count() == 1 and flash_of(r) != "", flash_of(r)[:46])

    # ------------------------------------------------------------------ 4
    print("\n4. Bidding: who may do what")
    outsider = make_auction(db, buyer, vendors, item, unit, invited=[vendors[0]],
                            title="Invite only")
    r = v2.post(f"/auctions/{outsider.id}/bid",
                data={"line_id": str(outsider.lines[0].id), "unit_price": "50"},
                follow_redirects=False)
    check("an uninvited bidder cannot bid", db.query(Bid).filter_by(
        auction_id=outsider.id).count() == 0)
    r = v2.get(f"/auctions/{outsider.id}")
    check("an uninvited bidder cannot even see the auction", r.status_code == 403)
    r = b.post(f"/auctions/{auction.id}/bid",
               data={"line_id": str(line.id), "unit_price": "50"}, follow_redirects=False)
    check("the buyer cannot bid on their own auction", r.status_code == 403)

    mine = db.query(Bid).first()
    r = v2.post(f"/auctions/{auction.id}/bids/{mine.id}/withdraw", follow_redirects=False)
    db.expire_all()
    check("a bidder cannot withdraw someone else's bid",
          db.query(Bid).get(mine.id).withdrawn is False, flash_of(r)[:40])

    r = v1.post(f"/auctions/{auction.id}/bid",
                data={"line_id": str(outsider.lines[0].id), "unit_price": "50"},
                follow_redirects=False)
    check("a line from another auction is refused", r.status_code == 404)

    # ------------------------------------------------------------------ 5
    print("\n5. Anonymity holds")
    page = v2.get(f"/auctions/{auction.id}").text
    check("a bidder never sees a rival's company name", "Acme 1" not in page)
    check("a bidder sees their own name", "Acme 2" in page or "Bidder 2" in page)
    check("the bid history of others is not in the page", "Bidder 1" not in page)

    # ------------------------------------------------------------------ 6
    print("\n6. The auction state machine")
    closed = make_auction(db, buyer, vendors, item, unit, status=AuctionStatus.CLOSED,
                          title="Already closed")
    r = v1.post(f"/auctions/{closed.id}/bid",
                data={"line_id": str(closed.lines[0].id), "unit_price": "10"},
                follow_redirects=False)
    check("bidding on a closed auction is refused", db.query(Bid).filter_by(
        auction_id=closed.id).count() == 0, flash_of(r)[:46])
    r = b.get(f"/auctions/{closed.id}/edit", follow_redirects=False)
    check("a closed auction cannot be edited", r.status_code == 303 and flash_of(r) != "")
    r = b.post(f"/auctions/{closed.id}/publish", follow_redirects=False)
    check("a closed auction cannot be published", "finished" in flash_of(r), flash_of(r)[:46])
    r = b.get(f"/auctions/{auction.id}/award", follow_redirects=False)
    check("a live auction cannot be awarded yet", r.status_code == 303
          and "closed" in flash_of(r).lower())
    r = b.post(f"/auctions/{auction.id}/go-live", follow_redirects=False)
    check("a live auction cannot be started again", "scheduled" in flash_of(r).lower())

    cancelled = make_auction(db, buyer, vendors, item, unit, title="To cancel")
    b.post(f"/auctions/{cancelled.id}/cancel", data={"reason": "no longer needed"},
           follow_redirects=False)
    db.refresh(cancelled)
    check("cancelling works", cancelled.status == AuctionStatus.CANCELLED)
    r = b.post(f"/auctions/{cancelled.id}/cancel", data={"reason": "again"},
               follow_redirects=False)
    check("cancelling twice is refused politely", "already finished" in flash_of(r),
          flash_of(r)[:46])
    r = v1.post(f"/auctions/{cancelled.id}/bid",
                data={"line_id": str(cancelled.lines[0].id), "unit_price": "10"},
                follow_redirects=False)
    check("nobody can bid on a cancelled auction",
          db.query(Bid).filter_by(auction_id=cancelled.id).count() == 0)

    # ------------------------------------------------------------------ 7
    print("\n7. Withdrawing after the clock stops")
    auction.end_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    r = v1.post(f"/auctions/{auction.id}/bids/{mine.id}/withdraw", follow_redirects=False)
    db.expire_all()
    check("a bid cannot be pulled after time is up",
          db.query(Bid).get(mine.id).withdrawn is False, flash_of(r)[:46])
    auction.end_at = datetime.utcnow() + timedelta(minutes=30)
    db.commit()

    # ------------------------------------------------------------------ 8
    print("\n8. Auto-extension and the closing scheduler")
    auction.end_at = datetime.utcnow() + timedelta(seconds=30)
    auction.extensions_used = 0
    db.commit()
    before = auction.end_at
    bid(v2, "90")
    db.refresh(auction)
    check("a last-moment bid extends the clock", auction.end_at > before)
    stats = scheduler.tick()
    db.refresh(auction)
    check("the scheduler does not close an auction it just extended",
          auction.status == AuctionStatus.LIVE, str(stats))
    auction.end_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    scheduler.tick()
    db.refresh(auction)
    check("but it does close one whose time really has run out",
          auction.status == AuctionStatus.CLOSED)
    scheduler.tick()
    check("a second tick changes nothing", db.query(Auction).filter(
        Auction.status == AuctionStatus.CLOSED).count() >= 1)

    # ------------------------------------------------------------------ 9
    print("\n9. Awarding")
    r = b.post(f"/auctions/{auction.id}/award", follow_redirects=False,
               data={f"winner_{line.id}": str(vendors[2].id)})
    check("awarding to a bidder who never bid is refused",
          db.query(Award).count() == 0 and "did not bid" in flash_of(r), flash_of(r)[:46])
    r = b.post(f"/auctions/{auction.id}/award", follow_redirects=False,
               data={f"winner_{line.id}": str(vendors[1].id), f"price_{line.id}": "-4"})
    check("a negative award price is refused", db.query(Award).count() == 0)
    r = b.post(f"/auctions/{auction.id}/award", follow_redirects=False,
               data={f"winner_{line.id}": str(vendors[1].id), f"price_{line.id}": "abc"})
    check("a nonsense award price is refused", db.query(Award).count() == 0)
    r = b.post(f"/auctions/{auction.id}/award", follow_redirects=False,
               data={f"winner_{line.id}": "9999", f"price_{line.id}": "5"})
    check("an unknown bidder id is refused", db.query(Award).count() == 0)
    r = b.post(f"/auctions/{auction.id}/award", follow_redirects=False,
               data={f"winner_{line.id}": str(vendors[1].id), f"price_{line.id}": "90"})
    check("a proper award goes through", db.query(Award).count() == 1, flash_of(r)[:50])
    awarded = db.query(Award).first()
    check("the winner gets the whole line quantity", awarded.qty == line.qty)
    r = v1.post(f"/auctions/{auction.id}/award", follow_redirects=False,
                data={f"winner_{line.id}": str(vendors[0].id)})
    check("a bidder cannot award the auction to themselves", r.status_code == 403)

    # ------------------------------------------------------------------ 10
    print("\n10. Masters and messages")
    r = b.post("/masters/vendors", data={"name": "", "email": "x@y.com"},
               follow_redirects=False)
    check("a vendor with no name is refused", "name" in flash_of(r).lower(), flash_of(r)[:40])
    r = b.post("/masters/vendors", data={"name": "Nameless", "email": "not-an-email"},
               follow_redirects=False)
    check("a vendor with a bad email is refused", "does not look like" in flash_of(r))
    r = b.post("/masters/vendors", data={"name": "Acme 1 renamed", "email": "v1@t.local"},
               follow_redirects=False)
    db.expire_all()
    check("re-using an email updates that vendor instead of dropping the edit",
          db.query(Vendor).filter_by(email="v1@t.local").first().name == "Acme 1 renamed",
          flash_of(r)[:56])
    r = b.post("/masters/items", data={"name": "  "}, follow_redirects=False)
    check("an item with no name is refused", "name" in flash_of(r).lower())
    r = b.post("/masters/units", data={"code": ""}, follow_redirects=False)
    check("a unit with no code is refused", "code" in flash_of(r).lower())

    r = b.post(f"/auctions/{auction.id}/messages",
               data={"body": "hello", "vendor_id": "9999"}, follow_redirects=False)
    check("the buyer cannot message a vendor who is not on the auction",
          "invited" in flash_of(r), flash_of(r)[:46])
    r = v1.post(f"/auctions/{auction.id}/messages", data={"body": "   "},
                follow_redirects=False)
    check("an empty message is refused", "Write a message" in flash_of(r))
    r = v1.post(f"/auctions/{auction.id}/messages", data={"body": "x" * 20000},
                follow_redirects=False)
    check("a very long message does not break anything", r.status_code == 303)

    # ------------------------------------------------------------------ 11
    print("\n11. Reports and exports on awkward data")
    empty = make_auction(db, buyer, vendors, item, unit, status=AuctionStatus.CLOSED,
                         title="Nobody bid")
    for path in [f"/reports/auction/{empty.id}", f"/reports/auction/{empty.id}/export/pdf",
                 f"/reports/auction/{empty.id}/export/csv",
                 f"/auctions/{empty.id}", f"/auctions/{empty.id}/award"]:
        r = b.get(path)
        check(f"an auction with no bids: {path}", r.status_code in (200, 303),
              str(r.status_code))
    no_ceiling = make_auction(db, buyer, vendors, item, unit, ceiling=None,
                              title="No ceiling at all")
    r = b.get(f"/auctions/{no_ceiling.id}")
    check("an auction with no ceiling renders", r.status_code == 200)
    r = b.get(f"/reports/auction/{no_ceiling.id}/export/pdf")
    check("...and its PDF builds", r.status_code == 200 and r.content[:4] == b"%PDF")
    r = b.get("/reports?date_from=2000-01-01&date_to=2099-12-31")
    check("a century-wide report renders", r.status_code == 200)

    # ------------------------------------------------------------------ 12
    print("\n12. Sessions and odds and ends")
    stale = TestClient(app, base_url="http://test", headers=BROWSER)
    stale.cookies.set("ra_session", "rubbish")
    r = stale.get("/", follow_redirects=False)
    check("a forged session cookie just sends you to sign in", r.status_code == 303)
    r = b.post("/login", data={"email": "buyer@t.local", "password": "wrong"},
               follow_redirects=False)
    check("a wrong password does not sign you in",
          r.status_code == 200 and "match an account" in r.text)
    r = b.post("/notifications/read-all", follow_redirects=False)
    check("mark-all-read works", r.status_code == 303)
    r = b.post("/assistant/ask", data={"question": "x" * 5000})
    check("the assistant survives a huge question", r.status_code == 200)
    r = b.post("/assistant/ask", data={"question": ""})
    check("...and an empty one", r.status_code == 200)
    mailer.flush()
    check("no email was left in a failed state",
          all(m.status in ("outbox", "sent") for m in db.query(__import__(
              "app.models", fromlist=["EmailMessage"]).EmailMessage).all()))

    db.close()
    print("\n" + "-" * 62)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All hostile checks passed.")
    return 0


def test_hostile():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
