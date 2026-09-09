"""End-to-end walk through a whole reverse auction, emails and errors included.

    python tests/test_end_to_end.py        (or: python -m pytest -q)

It builds a throwaway database, so it never touches your real data.
"""
from __future__ import annotations

import os
import re
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
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"

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


class Client(TestClient):
    """A test client that behaves like a browser on one point: it sends back the
    CSRF token the app gave it. Every real form carries it in a hidden field."""

    def post(self, url, **kwargs):
        token = self.cookies.get("ra_csrf")
        if token:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", token)
            kwargs["headers"] = headers
        return super().post(url, **kwargs)


def client_for(email: str) -> TestClient:
    c = Client(app, base_url="http://test")
    c.get("/login")                      # picks up the CSRF cookie, as a browser would
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


def flash_of(response) -> str:
    """The message the next page would show, straight out of the flash cookie."""
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


def main() -> int:
    db = SessionLocal()

    # ------------------------------------------------------------------ 1
    print("\n1. Accounts and masters")
    buyer = User(name="Test Buyer", email="buyer@test.local", role=Role.BUYER,
                 password_hash=hash_password(PASSWORD))
    db.add(buyer)
    unit = Unit(code="NOS", name="Numbers")
    db.add(unit)
    db.flush()
    vendors = []
    for i in (1, 2, 3):
        vendor = Vendor(name=f"Vendor {i}", email=f"v{i}@test.local",
                        extra_emails=f"sales{i}@test.local")
        db.add(vendor)
        db.flush()
        db.add(User(name=f"Bidder {i}", email=f"v{i}@test.local", role=Role.VENDOR,
                    vendor_id=vendor.id, password_hash=hash_password(PASSWORD)))
        vendors.append(vendor)
    item = Item(name="Test widget", default_unit_id=unit.id)
    open_item = Item(name="Open-priced widget", default_unit_id=unit.id)
    db.add_all([item, open_item])
    db.commit()
    check("buyer, 3 vendors and 2 items created", db.query(User).count() == 4)

    buyer_c = client_for("buyer@test.local")
    check("buyer dashboard loads", buyer_c.get("/").status_code == 200)
    check("masters page loads", buyer_c.get("/masters").status_code == 200)
    check("the approvals page is gone", buyer_c.get("/approvals").status_code == 404)

    # ------------------------------------------------------------------ 2
    print("\n2. Inline (Odoo-style) create from the auction form")
    r = buyer_c.post("/masters/quick/vendor",
                     data={"name": "Inline Vendor", "email": "inline@test.local"})
    check("inline vendor create returns json", r.status_code == 200 and "id" in r.json())
    r = buyer_c.post("/masters/quick/unit", data={"code": "kg"})
    check("inline unit create upper-cases the code", r.json()["label"] == "KG")
    r = buyer_c.post("/masters/quick/vendor", data={"name": "No Email", "email": ""})
    check("inline create refuses with a readable message",
          r.status_code == 400 and "email" in r.json().get("error", "").lower(),
          r.json().get("error", "")[:60])

    # ------------------------------------------------------------------ 3
    print("\n3. Every form mistake comes back on the form, never as raw JSON")
    start = datetime.utcnow() - timedelta(minutes=1)
    end = datetime.utcnow() + timedelta(minutes=30)
    base_form = {
        "title": "E2E widgets", "description": "test", "terms": "",
        "start_at": local(start), "end_at": local(end),
        "decrement_type": "absolute", "min_decrement": "5", "max_decrement": "200",
        "show_rank": "on", "show_lowest_bid": "on", "hide_bidder_names": "on",
        "auto_extend": "on", "extend_trigger_minutes": "2", "extend_by_minutes": "3",
        "max_extensions": "2",
        "line_item_id": [str(item.id), str(open_item.id)],
        "line_unit_id": [str(unit.id), str(unit.id)],
        "line_qty": ["100", "10"],
        "line_price": ["1000", ""],            # second line has NO ceiling
        "line_spec": ["", ""],
        "vendor_ids": [str(v.id) for v in vendors],
        "cc_emails": "finance@buyer.local, boss@buyer.local",
        f"notify_emails_{vendors[2].id}": "tender.desk@v3.local",
    }

    def bad(changes: dict, label: str, expect: str, drop: list[str] = ()):
        payload = dict(base_form)
        payload.update(changes)
        for key in drop:
            payload.pop(key, None)
        r = buyer_c.post("/auctions/new", data=payload, follow_redirects=False)
        html = r.text
        ok = (r.status_code == 200 and "text/html" in r.headers["content-type"]
              and expect.lower() in html.lower() and "{\"error\"" not in html[:200])
        kept = "E2E widgets" in html or payload.get("title", "") in html
        check(label, ok and kept, f"HTTP {r.status_code}")

    bad({"end_at": local(start - timedelta(hours=1))},
        "closing before opening is explained on the form", "closes before it opens")
    bad({"title": "  "}, "a missing title is explained on the form", "give the auction a title")
    bad({"line_qty": ["0", "10"]}, "a zero quantity is explained on the form",
        "quantity greater than zero")
    bad({"line_price": ["abc", ""]}, "a nonsense price is explained on the form",
        "is not a number")
    bad({"min_decrement": "300", "max_decrement": "10"},
        "an impossible decrement pair is explained", "smaller than the minimum")
    bad({"cc_emails": "not-an-address"}, "a bad copy-list address is explained",
        "does not look like an")
    bad({}, "no bidders ticked is explained on the form", "tick at least one bidder",
        drop=["vendor_ids"])
    bad({"line_item_id": [], "line_qty": [], "line_price": [], "line_unit_id": [],
         "line_spec": []}, "an auction with no items is explained", "at least one item")

    check("nothing was saved by the rejected submissions", db.query(Auction).count() == 0)

    # ------------------------------------------------------------------ 4
    print("\n4. Create, then publish straight to the bidders")
    r = buyer_c.post("/auctions/new", data=base_form, follow_redirects=False)
    check("auction created", r.status_code == 303, r.headers.get("location", ""))
    auction = db.query(Auction).order_by(Auction.id.desc()).first()
    db.refresh(auction)
    check("both items saved", len(auction.lines) == 2)
    check("the starting price is optional", auction.lines[1].starting_price is None)
    check("the priced line kept its ceiling", auction.lines[0].starting_price == 1000)
    check("three bidders invited", len(auction.participants) == 3)
    check("copy list saved", len(auction.cc_emails.split()) == 2)
    override = [p for p in auction.participants if p.vendor_id == vendors[2].id][0]
    check("per-auction address override saved",
          override.notify_emails.strip() == "tender.desk@v3.local")
    check("it starts life as a draft", auction.status == AuctionStatus.DRAFT)

    r = buyer_c.post(f"/auctions/{auction.id}/publish", follow_redirects=False)
    check("publish goes straight out — no approval step", r.status_code == 303)
    db.refresh(auction)
    check("the opening time had passed, so it went live at once",
          auction.status == AuctionStatus.LIVE, auction.status.value)
    check("the flash says bidding is open", "open now" in flash_of(r).lower(), flash_of(r))
    mailer.flush()
    invited_to = sorted(m.to_email for m in emails("invited"))
    check("vendor's extra contact was invited", "sales1@test.local" in invited_to)
    check("per-auction override replaced that vendor's own addresses",
          "tender.desk@v3.local" in invited_to and "v3@test.local" not in invited_to)
    # The buyer's own confirmation is filed as "published", not as an invitation.
    published_to = sorted(m.to_email for m in emails("published"))
    check("the buyer's copy list was told", "finance@buyer.local" in published_to,
          ", ".join(published_to))
    check("the creator is not told they are on their own copy list",
          all("copy list" not in m.html_body
              for m in emails("published") if m.to_email == "buyer@test.local"))
    check("'bidding is open' went out too", len(emails("started")) >= 5,
          f"{len(emails('started'))} sent")

    # ------------------------------------------------------------------ 5
    print("\n5. Bidding rules")
    priced, open_line = auction.lines[0], auction.lines[1]
    v1, v2, v3 = (client_for(f"v{i}@test.local") for i in (1, 2, 3))

    def bid(c, line, price):
        return c.post(f"/auctions/{auction.id}/bid",
                      data={"line_id": line.id, "unit_price": str(price)},
                      follow_redirects=False)

    def bid_error(response) -> str:
        return flash_of(response)

    r = bid(v1, priced, 1200)
    check("a bid above the ceiling is refused, in plain words",
          db.query(Bid).count() == 0 and "above the starting price" in bid_error(r),
          bid_error(r)[:52])
    bid(v1, priced, 990)
    check("first bid at or below the ceiling is accepted", db.query(Bid).count() == 1)
    r = bid(v2, priced, 988)
    check("a bid that ignores the minimum decrement is refused",
          db.query(Bid).count() == 1 and "at least" in bid_error(r))
    bid(v2, priced, 985)
    check("a bid meeting the decrement is accepted", db.query(Bid).count() == 2)
    r = bid(v3, priced, 700)
    check("a drop bigger than the maximum decrement is refused",
          db.query(Bid).count() == 2 and "one step" in bid_error(r))
    r = bid(v1, priced, 991)
    check("a bidder cannot go back up",
          db.query(Bid).count() == 2 and bid_error(r) != "", bid_error(r)[:52])
    bid(v3, priced, 970)
    check("third bidder accepted", db.query(Bid).count() == 3)

    r = bid(v1, open_line, 7500)
    check("with no ceiling, the opening bid can be any price",
          db.query(Bid).count() == 4, bid_error(r)[:60])
    r = bid(v2, open_line, 7500)
    check("after that first bid the decrement applies as usual",
          db.query(Bid).count() == 4 and "at least" in bid_error(r))
    bid(v2, open_line, 7400)
    check("a lower bid on the open line is accepted", db.query(Bid).count() == 5)

    from app.engine import auction_summary, best_per_vendor, line_baseline, vendor_rank
    ranked = best_per_vendor(db, priced.id)
    check("L1 is the lowest price", ranked[0].unit_price == 970)
    check("vendor 1 sits at L3", vendor_rank(db, priced.id, vendors[0].id) == 3)
    check("with no ceiling, the baseline is the highest bid received",
          line_baseline(db, open_line) == 10 * 7500, str(line_baseline(db, open_line)))

    mailer.flush()
    outbid_to = {m.to_email for m in emails("outbid")}
    check("outbid alerts were emailed", len(emails("outbid")) >= 2)
    check("the outbid alert reached the second contact too", "sales1@test.local" in outbid_to)

    # ------------------------------------------------------------------ 6
    print("\n6. What each side can see")
    page = v2.get(f"/auctions/{auction.id}").text
    check("a bidder never sees another bidder's name", "Vendor 3" not in page)
    check("a bidder does see the current lowest price", "970" in page)
    check("a bidder sees the quantity they are pricing", "100" in page)
    buyer_page = buyer_c.get(f"/auctions/{auction.id}").text
    check("the buyer sees real bidder names", "Vendor 3" in buyer_page)

    # ------------------------------------------------------------------ 7
    print("\n7. Withdraw, conversation, auto-extension")
    last = db.query(Bid).filter(Bid.line_id == priced.id).order_by(Bid.id.desc()).first()
    v3.post(f"/auctions/{auction.id}/bids/{last.id}/withdraw", data={"reason": "typo"},
            follow_redirects=False)
    db.expire_all()
    check("a withdrawn bid drops out of the ranking",
          best_per_vendor(db, priced.id)[0].unit_price == 985)

    v1.post(f"/auctions/{auction.id}/messages", data={"body": "Is 3-ply acceptable?"},
            follow_redirects=False)
    mailer.flush()
    check("the message was emailed to the buyer", len(emails("message")) >= 1)

    auction.end_at = datetime.utcnow() + timedelta(seconds=60)
    db.commit()
    before = auction.end_at
    bid(v1, priced, 980)
    db.refresh(auction)
    check("a late bid pushes the finish line back", auction.end_at > before,
          f"+{(auction.end_at - before).total_seconds():.0f}s")
    check("the extension was counted", auction.extensions_used == 1)
    mailer.flush()
    check("everyone was told about the extension", len(emails("extended")) >= 4)

    # ------------------------------------------------------------------ 8
    print("\n8. Close and award — one bidder per item")
    r = buyer_c.get(f"/auctions/{auction.id}/award", follow_redirects=False)
    check("awarding before the close is refused with a message",
          r.status_code == 303 and "closed" in flash_of(r).lower(), flash_of(r)[:50])

    buyer_c.post(f"/auctions/{auction.id}/close-now", follow_redirects=False)
    db.refresh(auction)
    check("auction closed", auction.status == AuctionStatus.CLOSED)
    mailer.flush()
    check("the copy list was told bidding closed",
          "finance@buyer.local" in {m.to_email for m in emails("closed")})

    award_page = buyer_c.get(f"/auctions/{auction.id}/award")
    check("the award screen loads", award_page.status_code == 200)
    check("the award screen shows the quantity per line", "100 NOS" in award_page.text)
    check("the award screen offers no way to split a line",
          "split across" not in award_page.text.lower()
          and "award_qty" not in award_page.text)

    ranked_priced = best_per_vendor(db, priced.id)
    ranked_open = best_per_vendor(db, open_line.id)
    r = buyer_c.post(f"/auctions/{auction.id}/award", follow_redirects=False, data={
        f"winner_{priced.id}": str(ranked_priced[0].vendor_id),
        f"price_{priced.id}": str(ranked_priced[0].unit_price),
        f"winner_{open_line.id}": str(ranked_open[0].vendor_id),
        f"price_{open_line.id}": str(ranked_open[0].unit_price),
    })
    check("award accepted", r.status_code == 303, r.headers.get("location", ""))
    db.expire_all()
    awards = db.query(Award).all()
    check("exactly one award per item", len(awards) == 2)
    check("each award covers the whole quantity",
          all(a.qty == a.line.qty for a in awards))
    check("different items may go to different bidders",
          len({a.vendor_id for a in awards}) >= 1)
    db.refresh(auction)
    check("the auction is marked awarded", auction.status == AuctionStatus.AWARDED)

    r = buyer_c.post(f"/auctions/{auction.id}/award", follow_redirects=False,
                     data={f"winner_{priced.id}": "", f"winner_{open_line.id}": ""})
    check("awarding nothing at all is refused on the award screen itself",
          r.status_code == 200 and "at least one item" in r.text
          and "text/html" in r.headers["content-type"], f"HTTP {r.status_code}")
    db.expire_all()
    check("the earlier award survived that mistake", db.query(Award).count() == 2)

    # award the lot to a single bidder instead
    winner = ranked_priced[0].vendor_id
    r = buyer_c.post(f"/auctions/{auction.id}/award", follow_redirects=False, data={
        f"winner_{priced.id}": str(winner),
        f"price_{priced.id}": str(ranked_priced[0].unit_price),
        f"winner_{open_line.id}": str(winner), f"price_{open_line.id}": "7000",
    })
    db.expire_all()
    awards = db.query(Award).all()
    check("the whole auction can go to one bidder",
          r.status_code == 303 and len(awards) == 2
          and {a.vendor_id for a in awards} == {winner})
    check("a manual award price is honoured",
          any(a.unit_price == 7000 for a in awards))
    quoted = flash_of(r)
    expected_saving = (100 * 1000 + 10 * 7500) - (100 * ranked_priced[0].unit_price + 10 * 7000)
    check("the confirmation quotes savings from the awarded prices, not the bids",
          f"{expected_saving:,.2f}" in quoted, quoted[:80])

    mailer.flush()
    awarded_to = {m.to_email for m in emails("awarded")}
    check("the winner was emailed at every address", len(awarded_to) >= 3,
          ", ".join(sorted(awarded_to)))
    check("the copy list got the award summary", "boss@buyer.local" in awarded_to)
    check("bidders who did not win were told", len(emails("not_awarded")) >= 1)

    # ------------------------------------------------------------------ 9
    print("\n9. Savings")
    summary = auction_summary(db, auction)
    expected_baseline = 100 * 1000 + 10 * 7500
    expected_final = 100 * ranked_priced[0].unit_price + 10 * 7000
    check("baseline mixes the ceiling and, where none was set, the highest bid",
          abs(summary["baseline"] - expected_baseline) < 0.01, f"{summary['baseline']:.0f}")
    check("savings = baseline − what was actually awarded",
          abs(summary["savings"] - (expected_baseline - expected_final)) < 0.01,
          f"{summary['savings']:.0f}")
    check("savings percentage is sensible", 0 < summary["savings_pct"] < 100)

    # ------------------------------------------------------------------ 10
    print("\n10. Reports")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    span = f"?date_from={(datetime.utcnow() - timedelta(days=2)):%Y-%m-%d}&date_to={today}"
    check("reports page loads", buyer_c.get("/reports" + span).status_code == 200)
    r = buyer_c.get("/reports/savings.pdf" + span)
    check("total savings PDF downloads", r.status_code == 200 and r.content[:4] == b"%PDF",
          f"{len(r.content)} bytes")
    r = buyer_c.get("/reports/savings.csv" + span)
    check("total savings CSV downloads", r.status_code == 200 and b"Total Savings" in r.content)
    r = buyer_c.get(f"/reports/auction/{auction.id}/export/pdf")
    check("auction summary PDF downloads", r.status_code == 200 and r.content[:4] == b"%PDF",
          f"{len(r.content)} bytes")
    r = buyer_c.get(f"/reports/auction/{auction.id}/export/csv")
    check("auction summary CSV lists every bid", r.status_code == 200 and
          r.content.count(b"\n") > 8)
    check("auction summary page loads",
          buyer_c.get(f"/reports/auction/{auction.id}").status_code == 200)

    # ------------------------------------------------------------------ 11
    print("\n11. A second auction, scheduled then started early")
    later = datetime.utcnow() + timedelta(hours=3)
    form2 = dict(base_form)
    form2.update({"title": "Scheduled widgets", "start_at": local(later),
                  "end_at": local(later + timedelta(hours=2)),
                  "line_item_id": [str(item.id)], "line_unit_id": [str(unit.id)],
                  "line_qty": ["5"], "line_price": ["50"], "line_spec": [""]})
    buyer_c.post("/auctions/new", data=form2, follow_redirects=False)
    second = db.query(Auction).order_by(Auction.id.desc()).first()
    buyer_c.post(f"/auctions/{second.id}/publish", follow_redirects=False)
    db.refresh(second)
    check("a future auction publishes as scheduled", second.status == AuctionStatus.SCHEDULED)
    r = buyer_c.post(f"/auctions/{second.id}/go-live", follow_redirects=False)
    db.refresh(second)
    check("and can be started early on demand", second.status == AuctionStatus.LIVE)
    r = buyer_c.post(f"/auctions/{second.id}/publish", follow_redirects=False)
    check("publishing twice is refused with a message",
          "already been published" in flash_of(r), flash_of(r)[:50])
    scheduler.tick()
    check("the scheduler leaves a live auction alone", second.status == AuctionStatus.LIVE)

    # ------------------------------------------------------------------ 12
    print("\n12. Emails, audit trail, error pages")
    mailer.flush()
    total = emails()
    check("every email reached the outbox", all(m.status == "outbox" for m in total),
          f"{len(total)} messages")
    check(".eml files were written", len(list((Path(TMP) / "outbox").glob("*.eml"))) == len(total))
    check("outbox page loads", buyer_c.get("/outbox").status_code == 200)
    check("an outbox message renders", buyer_c.get(f"/outbox/{total[0].id}").status_code == 200)

    from app.audit import for_auction
    actions = {log.action for log in for_auction(db, auction.id)}
    check("the audit trail has create, publish, bid and award",
          {"auction.create", "auction.publish", "bid.place", "auction.award"} <= actions,
          ", ".join(sorted(actions)))
    check("history tab loads",
          buyer_c.get(f"/auctions/{auction.id}?tab=history").status_code == 200)

    browser = {"accept": "text/html,application/xhtml+xml"}   # what a browser really sends
    r = buyer_c.get("/auctions/999999", headers=browser)
    check("a missing auction gives a friendly HTML page, not JSON",
          r.status_code == 404 and "text/html" in r.headers["content-type"]
          and "does not exist" in r.text)
    r = v1.get(f"/auctions/{auction.id}/award", headers=browser)
    check("a bidder opening the award screen gets a friendly page",
          r.status_code == 403 and "text/html" in r.headers["content-type"]
          and "do not have access" in r.text)
    r = buyer_c.get("/no-such-page", headers=browser)
    check("an unknown address gives the error screen", r.status_code == 404
          and "Go back" in r.text)

    check("vendor dashboard loads", v1.get("/").status_code == 200)
    check("the assistant answers a question",
          "lowest" in buyer_c.post("/assistant/ask",
                                   data={"question": "what is a reverse auction?"}).text.lower())
    check("health check reports outbox mode",
          buyer_c.get("/healthz").json()["email_mode"] == "outbox")

    db.close()
    print("\n" + "-" * 62)
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
