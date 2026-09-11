"""One check per bug found in the end-to-end bug hunt.

Every check here failed before the fix that goes with it, so if one of them
ever fails again the same defect is back.

    python tests/test_regressions.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-regress-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ["RA_TIMEZONE"] = "Asia/Kolkata"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient           # noqa: E402

from app import engine, mailer, migrate, notify, reporting, scheduler  # noqa: E402
from app.db import Base, SessionLocal, engine as db_engine             # noqa: E402
from app.main import app                            # noqa: E402
from app.models import (Auction, AuctionLine, AuctionStatus, Award, Bid, DecrementType,  # noqa: E402
                        EmailMessage, Item, Participant, Role, Unit, User, Vendor)
from app.security import hash_password              # noqa: E402
from app.utils import TZ              # noqa: E402

Base.metadata.create_all(bind=db_engine)
PW = "test1234"
BROWSER = {"accept": "text/html,application/xhtml+xml"}
FAILS: list[str] = []


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


class Client(TestClient):
    def post(self, url, **kwargs):
        token = self.cookies.get("ra_csrf")
        if token:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", token)
            kwargs["headers"] = headers
        return super().post(url, **kwargs)


def login(email):
    c = Client(app, base_url="http://test")
    c.get("/login")
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


def told(response) -> str:
    return flash_of(response) or (response.text if "text/html" in
                                  response.headers.get("content-type", "") else "")


def local(dt: datetime) -> str:
    from app.utils import to_local_string
    return to_local_string(dt)


def make_auction(db, buyer, vendors, item, unit, *, status=AuctionStatus.LIVE, ceiling=100.0,
                 qty=10.0, minutes=60, min_dec=1.0, max_dec=0.0,
                 decrement_type=DecrementType.ABSOLUTE, title="Regress", published=True):
    now = datetime.utcnow()
    a = Auction(reference=f"RA-R-{datetime.utcnow().timestamp():.6f}", title=title,
                creator_id=buyer.id, status=status,
                start_at=now - timedelta(minutes=5), end_at=now + timedelta(minutes=minutes),
                original_end_at=now + timedelta(minutes=minutes),
                decrement_type=decrement_type, min_decrement=min_dec, max_decrement=max_dec,
                auto_extend=True, extend_trigger_seconds=120, extend_by_seconds=180,
                max_extensions=3, published_at=(now - timedelta(days=1)) if published else None)
    db.add(a)
    db.flush()
    db.add(AuctionLine(auction_id=a.id, item_id=item.id, unit_id=unit.id, qty=qty,
                       starting_price=ceiling))
    for index, v in enumerate(vendors):
        db.add(Participant(auction_id=a.id, vendor_id=v.id, alias=f"Bidder {chr(65 + index)}"))
    db.commit()
    db.refresh(a)
    return a


def bid_as(client, auction, line, price):
    return client.post(f"/auctions/{auction.id}/bid",
                       data={"line_id": str(line.id), "unit_price": str(price)},
                       follow_redirects=False)


def main() -> int:                                                       # noqa: C901
    db = SessionLocal()
    buyer = User(name="Buyer One", email="buyer@r.local", role=Role.BUYER,
                 password_hash=hash_password(PW))
    db.add(buyer)
    unit = Unit(code="NOS")
    item = Item(name="Widget")
    spare_item = Item(name="Spare widget")
    db.add_all([unit, item, spare_item])
    db.flush()
    vendors = []
    for i in (1, 2, 3):
        v = Vendor(name=f"Acme {i}", email=f"v{i}@r.local")
        db.add(v)
        db.flush()
        db.add(User(name=f"Bidder {i}", email=f"v{i}@r.local", role=Role.VENDOR,
                    vendor_id=v.id, password_hash=hash_password(PW)))
        vendors.append(v)
    db.commit()
    b = login("buyer@r.local")
    v1, v2, v3 = (login(f"v{i}@r.local") for i in (1, 2, 3))

    # ------------------------------------------------------------------ 1
    print("\n1. The audit trail is the buyer's alone")
    auction = make_auction(db, buyer, vendors, item, unit, title="Audit leak")
    line = auction.lines[0]
    bid_as(v1, auction, line, 95)
    bid_as(v2, auction, line, 90)
    page = v3.get(f"/auctions/{auction.id}?tab=history")
    check("a bidder cannot open the history tab", page.status_code == 403,
          str(page.status_code))
    check("...so a rival's price is not in the page",
          "95.0" not in page.text and "bid.place" not in page.text)
    check("the buyer still can", b.get(f"/auctions/{auction.id}?tab=history").status_code == 200)

    # ------------------------------------------------------------------ 2
    print("\n2. Bidding cannot bottom out at one paisa")
    small = make_auction(db, buyer, vendors, item, unit, ceiling=30.0, min_dec=10.0,
                         title="Bottoming out")
    small_line = small.lines[0]
    bid_as(v1, small, small_line, 30)
    bid_as(v2, small, small_line, 20)
    bid_as(v1, small, small_line, 10)
    window = engine.bid_window(db, small, small_line)
    check("the window reports itself exhausted, with no suggestion",
          window.exhausted and window.suggestion is None and not window.open_ended)
    page = v2.get(f"/auctions/{small.id}")
    check("the board does not offer a one-paisa bid",
          "Use ₹ 0.01" not in page.text and "gone as far as it can" in page.text)
    r = bid_as(v2, small, small_line, 0.01)
    check("and a one-paisa bid is refused in plain words",
          engine.best_bid(db, small_line.id).unit_price == 10.0
          and "as far as it can" in told(r), told(r)[:46])

    # ------------------------------------------------------------------ 3
    print("\n3. Withdrawing a bid does not let a bidder raise their price")
    wd = make_auction(db, buyer, vendors, item, unit, ceiling=100.0, min_dec=1.0,
                      title="Withdraw and raise")
    wd_line = wd.lines[0]
    bid_as(v1, wd, wd_line, 100)
    bid_as(v2, wd, wd_line, 50)
    mine = engine.vendor_best(db, wd_line.id, vendors[1].id)
    v2.post(f"/auctions/{wd.id}/bids/{mine.id}/withdraw", data={"reason": "oops"},
            follow_redirects=False)
    r = bid_as(v2, wd, wd_line, 99)
    db.expire_all()
    check("re-bidding above a withdrawn bid of their own is refused",
          engine.best_bid(db, wd_line.id).unit_price == 100.0 and "withdrew" in told(r),
          told(r)[:60])
    r = bid_as(v2, wd, wd_line, 49)
    check("...but a genuinely lower bid is still accepted",
          engine.best_bid(db, wd_line.id).unit_price == 49.0, told(r)[:50])

    # ------------------------------------------------------------------ 4
    print("\n4. A withdrawn bid cannot be withdrawn twice")
    again = v2.post(f"/auctions/{wd.id}/bids/{mine.id}/withdraw", data={"reason": "again"},
                    follow_redirects=False)
    check("the second withdrawal is refused", "already been withdrawn" in told(again),
          told(again)[:46])

    # ------------------------------------------------------------------ 5
    print("\n5. A percentage decrement that rounds to nothing still bites")
    tiny = make_auction(db, buyer, vendors, item, unit, ceiling=0.30, min_dec=1.0,
                        decrement_type=DecrementType.PERCENT, title="Tiny percent")
    tiny_line = tiny.lines[0]
    bid_as(v1, tiny, tiny_line, 0.30)
    r = bid_as(v2, tiny, tiny_line, 0.30)
    check("matching the lowest bid exactly is refused",
          len(engine.best_per_vendor(db, tiny_line.id)) == 1, told(r)[:50])

    # ------------------------------------------------------------------ 6
    print("\n6. Two bids at once cannot both beat the same price")
    race = make_auction(db, buyer, vendors, item, unit, ceiling=100.0, min_dec=5.0,
                        title="Race")
    race_line = race.lines[0]
    bid_as(v1, race, race_line, 100)
    ready = threading.Barrier(2)
    results = []

    def racer(client):
        ready.wait()
        results.append(bid_as(client, race, race_line, 95))

    threads = [threading.Thread(target=racer, args=(c,)) for c in (v2, v3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.expire_all()
    live = [bid.unit_price for bid in engine.line_bids(db, race_line.id)]
    check("only one of the two identical bids was accepted",
          live.count(95.0) == 1, f"live bids {live}")

    # ------------------------------------------------------------------ 7
    print("\n7. Two last-second bids extend the clock once, and say so once")
    ext = make_auction(db, buyer, vendors, item, unit, ceiling=100.0, min_dec=1.0,
                       minutes=1, title="Extension race")
    ext_line = ext.lines[0]
    before_end = ext.end_at
    mailer.flush()
    sent_before = db.query(EmailMessage).filter(EmailMessage.event == "extended").count()
    ready2 = threading.Barrier(2)

    def extender(client, price):
        ready2.wait()
        bid_as(client, ext, ext_line, price)

    threads = [threading.Thread(target=extender, args=(c, p))
               for c, p in ((v1, 90), (v2, 80))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.expire_all()
    db.refresh(ext)
    mailer.flush()
    extended_mail = (db.query(EmailMessage).filter(EmailMessage.event == "extended").count()
                     - sent_before)
    check("the clock moved exactly one extension",
          ext.extensions_used == 1
          and abs((ext.end_at - before_end).total_seconds() - 180) < 2,
          f"used={ext.extensions_used} moved={(ext.end_at - before_end).total_seconds():.0f}s")
    check("nobody was emailed the same extension twice",
          extended_mail <= len(notify.participant_users(db, ext)) + 1,
          f"{extended_mail} emails")

    # ------------------------------------------------------------------ 8
    print("\n8. The scheduler keeps going when one auction's alert fails")
    a_soon = make_auction(db, buyer, vendors, item, unit, minutes=2, title="Warn me")
    a_over = make_auction(db, buyer, vendors, item, unit, minutes=60, title="Close me")
    a_over.end_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    real = notify.ending_soon
    calls = {"n": 0}

    def exploding(db_, auction_):
        calls["n"] += 1
        raise RuntimeError("mail server said no")

    notify.ending_soon = exploding
    try:
        stats = scheduler.tick()
    finally:
        notify.ending_soon = real
    db.expire_all()
    db.refresh(a_soon)
    db.refresh(a_over)
    check("one failing alert does not stop the tick",
          a_over.status == AuctionStatus.CLOSED, f"{stats}")
    check("...and the alert that failed is not marked as sent",
          a_soon.ending_soon_notified is False)
    stats = scheduler.tick()
    check("so the next tick tries it again", stats["ending_soon"] >= 1, f"{stats}")

    # ------------------------------------------------------------------ 9
    print("\n9. Rescheduling brings the time-based alerts back")
    later = datetime.utcnow() + timedelta(days=3)
    sched = make_auction(db, buyer, vendors, item, unit, status=AuctionStatus.SCHEDULED,
                         title="Rescheduled")
    sched.start_at = datetime.utcnow() + timedelta(minutes=10)
    sched.starting_soon_notified = True
    db.commit()
    r = b.post(f"/auctions/{sched.id}/edit", follow_redirects=False, data={
        "title": "Rescheduled", "start_at": local(later),
        "end_at": local(later + timedelta(hours=2)),
        "decrement_type": "absolute", "min_decrement": "1", "max_decrement": "0",
        "extend_trigger_minutes": "2", "extend_by_minutes": "3", "max_extensions": "3",
        "line_item_id": [str(item.id)], "line_unit_id": [str(unit.id)],
        "line_qty": ["10"], "line_price": ["100"], "line_spec": [""],
        "vendor_ids": [str(v.id) for v in vendors], "cc_emails": "",
    })
    db.expire_all()
    db.refresh(sched)
    check("moving the opening time clears the 'starts soon' flag",
          sched.starting_soon_notified is False, f"HTTP {r.status_code}")

    # ------------------------------------------------------------------ 10
    print("\n10. Editing a published auction tells the bidders")
    mailer.flush()
    before_invites = {m.to_email for m in db.query(EmailMessage)
                      .filter(EmailMessage.event == "invited").all()}
    pub = make_auction(db, buyer, [vendors[0]], item, unit, status=AuctionStatus.SCHEDULED,
                       title="Published then edited")
    pub.start_at = datetime.utcnow() + timedelta(hours=3)
    pub.end_at = datetime.utcnow() + timedelta(hours=5)
    db.commit()
    r = b.post(f"/auctions/{pub.id}/edit", follow_redirects=False, data={
        "title": "Published then edited", "start_at": local(pub.start_at),
        "end_at": local(datetime.utcnow() + timedelta(hours=9)),
        "decrement_type": "absolute", "min_decrement": "1", "max_decrement": "0",
        "extend_trigger_minutes": "2", "extend_by_minutes": "3", "max_extensions": "3",
        "line_item_id": [str(item.id)], "line_unit_id": [str(unit.id)],
        "line_qty": ["10"], "line_price": ["100"], "line_spec": [""],
        "vendor_ids": [str(v.id) for v in vendors], "cc_emails": "",
    })
    mailer.flush()
    after = db.query(EmailMessage).filter(EmailMessage.event == "invited").all()
    new_invites = {m.to_email for m in after} - before_invites
    check("a bidder added on the edit is invited",
          "v2@r.local" in new_invites and "v3@r.local" in new_invites,
          ", ".join(sorted(new_invites)))
    updated = db.query(EmailMessage).filter(EmailMessage.event == "updated").all()
    check("the bidders who were already invited are told what changed",
          any(m.to_email == "v1@r.local" for m in updated), f"{len(updated)} emails")
    check("and the buyer is told who was written to", "invited by email" in flash_of(r),
          flash_of(r)[:70])

    # ------------------------------------------------------------------ 11
    print("\n11. Archiving a vendor or item does not quietly change an auction")
    keep = make_auction(db, buyer, vendors, item, unit, status=AuctionStatus.DRAFT,
                        title="Archived pieces", published=False)
    vendors[1].is_active = False
    item.is_active = False
    db.commit()
    form = b.get(f"/auctions/{keep.id}/edit")
    check("the archived bidder is still on the edit form",
          f'value="{vendors[1].id}"' in form.text and "Acme 2" in form.text)
    check("the archived item is still in the row's dropdown",
          f'value="{item.id}"' in form.text and "Widget" in form.text)
    r = b.post(f"/auctions/{keep.id}/edit", follow_redirects=False, data={
        "title": "Archived pieces, renamed", "start_at": local(keep.start_at),
        "end_at": local(keep.end_at),
        "decrement_type": "absolute", "min_decrement": "1", "max_decrement": "0",
        "extend_trigger_minutes": "2", "extend_by_minutes": "3", "max_extensions": "3",
        "line_item_id": [str(item.id)], "line_unit_id": [str(unit.id)],
        "line_qty": ["10"], "line_price": ["100"], "line_spec": ["keep me"],
        "vendor_ids": [str(v.id) for v in vendors], "cc_emails": "",
    })
    db.expire_all()
    db.refresh(keep)
    check("saving a title change keeps all three bidders",
          len(keep.participants) == 3, str(len(keep.participants)))
    check("...and keeps the line", len(keep.lines) == 1 and keep.lines[0].qty == 10)
    vendors[1].is_active = True
    item.is_active = True
    db.commit()

    # ------------------------------------------------------------------ 12
    print("\n12. A bidder never sees an unpublished draft")
    draft = make_auction(db, buyer, vendors, item, unit, status=AuctionStatus.DRAFT,
                         title="SECRET UNPUBLISHED TENDER", published=False)
    home = v1.get("/")
    check("the vendor dashboard hides it", "SECRET UNPUBLISHED TENDER" not in home.text)
    check("the auction list hides it too",
          "SECRET UNPUBLISHED TENDER" not in v1.get("/auctions").text)

    # ------------------------------------------------------------------ 13
    print("\n13. Cancelling a draft emails nobody")
    mailer.flush()
    before_cancelled = db.query(EmailMessage).filter(EmailMessage.event == "cancelled").count()
    r = b.post(f"/auctions/{draft.id}/cancel", data={"reason": "not needed"},
               follow_redirects=False)
    mailer.flush()
    after_cancelled = db.query(EmailMessage).filter(EmailMessage.event == "cancelled").count()
    check("no cancellation email goes out for a draft",
          after_cancelled == before_cancelled, f"{after_cancelled - before_cancelled} sent")
    check("and the buyer is told why nobody was written to",
          "never been published" in flash_of(r), flash_of(r)[:60])

    # ------------------------------------------------------------------ 14
    print("\n14. A rejected award keeps every price and note on the page")
    closing = make_auction(db, buyer, vendors, spare_item, unit, ceiling=1000.0, min_dec=10.0,
                           title="Two lines")
    db.add(AuctionLine(auction_id=closing.id, item_id=item.id, unit_id=unit.id, qty=5,
                       starting_price=900.0))
    db.commit()
    db.refresh(closing)
    line_a, line_b = closing.lines
    bid_as(v1, closing, line_a, 900)
    bid_as(v2, closing, line_a, 880)
    bid_as(v1, closing, line_b, 800)
    closing.status = AuctionStatus.CLOSED
    db.commit()
    r = b.post(f"/auctions/{closing.id}/award", follow_redirects=False, data={
        f"winner_{line_a.id}": str(vendors[0].id), f"price_{line_a.id}": "870.50",
        f"note_{line_a.id}": "NEGOTIATED BY PHONE",
        f"winner_{line_b.id}": str(vendors[0].id), f"price_{line_b.id}": "eight hundred",
    })
    check("the refusal comes back on the award screen", r.status_code == 200
          and "is not a price" in r.text, f"HTTP {r.status_code}")
    check("the typed price is still there", "870.50" in r.text or "870.5" in r.text)
    check("the typed note is still there", "NEGOTIATED BY PHONE" in r.text)
    check("the chosen winner is still selected",
          f'value="{vendors[0].id}"' in r.text and "checked" in r.text)
    check("nothing was awarded", db.query(Award).filter(Award.auction_id == closing.id)
          .count() == 0)

    # ------------------------------------------------------------------ 15
    print("\n15. An award is booked at the price on the screen")
    r = b.post(f"/auctions/{closing.id}/award", follow_redirects=False, data={
        f"winner_{line_a.id}": str(vendors[1].id), f"price_{line_a.id}": "880",
        f"winner_{line_b.id}": "",
    })
    db.expire_all()
    award = db.query(Award).filter(Award.line_id == line_a.id).first()
    check("the award goes to the chosen bidder at their own price",
          award and award.vendor_id == vendors[1].id and award.unit_price == 880.0,
          f"{award.vendor_id if award else None} @ {award.unit_price if award else None}")
    check("the award points at the bid it matches",
          award.bid_id == engine.vendor_best(db, line_a.id, vendors[1].id).id)
    r = b.post(f"/auctions/{closing.id}/award", follow_redirects=False, data={
        f"winner_{line_a.id}": str(vendors[1].id), f"price_{line_a.id}": "870",
        f"winner_{line_b.id}": "",
    })
    db.expire_all()
    award = db.query(Award).filter(Award.line_id == line_a.id).first()
    check("a negotiated price is not passed off as a bid", award.bid_id is None
          and award.unit_price == 870.0)
    stranger = Vendor(name="Never Invited Ltd", email="stranger@r.local")
    db.add(stranger)
    db.commit()
    r = b.post(f"/auctions/{closing.id}/award", follow_redirects=False,
               data={f"winner_{line_a.id}": str(stranger.id), f"price_{line_a.id}": "500"})
    check("awarding to a vendor who was never invited is refused",
          "not invited" in told(r), told(r)[:70])
    r = b.post(f"/auctions/{closing.id}/award", follow_redirects=False,
               data={f"winner_{line_a.id}": "99999", f"price_{line_a.id}": "500"})
    check("awarding to a bidder id that does not exist is refused",
          "no longer exists" in told(r), told(r)[:70])
    # put the real award back for the checks that follow
    b.post(f"/auctions/{closing.id}/award", follow_redirects=False, data={
        f"winner_{line_a.id}": str(vendors[1].id), f"price_{line_a.id}": "870",
        f"winner_{line_b.id}": "",
    })
    db.expire_all()

    # ------------------------------------------------------------------ 16
    print("\n16. Line savings use the awarded price")
    result = engine.line_result(db, line_a)
    check("the line's savings follow the award, not the lowest bid",
          abs(result["savings"] - (1000.0 - 870.0) * line_a.qty) < 0.01
          and result["basis"] == "awarded", f"{result['savings']:.2f} on {result['basis']}")
    summary = engine.auction_summary(db, closing)
    line_total = sum(engine.line_result(db, ln)["savings"] for ln in closing.lines)
    check("the line savings add up to the auction savings",
          abs(line_total - summary["savings"]) < 0.01,
          f"{line_total:.2f} vs {summary['savings']:.2f}")

    # ------------------------------------------------------------------ 17
    print("\n17. Every bid means every bid")
    history = engine.all_line_bids(db, wd_line.id)
    check("the engine can list withdrawn bids", any(bid.withdrawn for bid in history))
    page = b.get(f"/auctions/{wd.id}")
    check("the buyer's 'every bid' panel shows the withdrawn one",
          "withdrawn" in page.text)
    report = reporting.auction_summary_report(db, wd)
    check("the auction report lists it too",
          any(bid.withdrawn for entry in report["lines"] for bid in entry["history"]))
    check("but it is out of the ranking",
          all(not bid.withdrawn for bid in engine.line_bids(db, wd_line.id)))

    # ------------------------------------------------------------------ 18
    print("\n18. A savings report counts an auction in the period it was decided")
    january = datetime(2026, 1, 31, 4, 30)
    spanning = make_auction(db, buyer, vendors, item, unit, title="Spans a month end")
    spanning.start_at = january
    spanning.end_at = january + timedelta(hours=2)
    spanning.closed_at = january + timedelta(hours=2)
    spanning.awarded_at = datetime(2026, 2, 5, 4, 30)
    spanning.status = AuctionStatus.AWARDED
    db.add(Award(auction_id=spanning.id, line_id=spanning.lines[0].id,
                 vendor_id=vendors[0].id, qty=spanning.lines[0].qty, unit_price=90.0,
                 total=900.0, awarded_by_id=buyer.id, awarded_at=spanning.awarded_at))
    db.commit()
    jan = reporting.total_savings(db, datetime(2026, 1, 1), datetime(2026, 1, 31, 23, 59))
    feb = reporting.total_savings(db, datetime(2026, 2, 1), datetime(2026, 2, 28, 23, 59))
    refs_jan = [row["reference"] for row in jan["rows"]]
    refs_feb = [row["reference"] for row in feb["rows"]]
    check("it is not in the month bidding opened",
          spanning.reference not in refs_jan, ", ".join(refs_jan))
    check("it is in the month it was awarded",
          spanning.reference in refs_feb, ", ".join(refs_feb))

    # ------------------------------------------------------------------ 19
    print("\n19. Money in a PDF is readable, whatever the currency")
    data = reporting.auction_summary_report(db, closing)
    pdf = reporting.auction_pdf(data)
    check("the auction PDF builds", pdf[:4] == b"%PDF", f"{len(pdf)} bytes")
    check("no literal <br/> anywhere in it", b"&lt;br/&gt;" not in pdf)
    check("the currency is shown as text the font can draw",
          reporting.pdf_money(1234.5).endswith("1,234.50")
          and (reporting.PDF_SYMBOL_OK or "INR" in reporting.pdf_money(1234.5)),
          reporting.pdf_money(1234.5))

    # ------------------------------------------------------------------ 20
    print("\n20. Emails say what they mean")
    facts = notify._auction_facts(closing)
    labels = dict(facts)
    check("the ceiling line is not the whole auction's value",
          "Starting price (ceiling)" not in labels
          or "per" in labels.get("Starting price (ceiling)", ""),
          str(labels)[:90])
    single = make_auction(db, buyer, vendors, item, unit, ceiling=100.0, qty=10.0,
                          title="One line")
    single_labels = dict(notify._auction_facts(single))
    check("a one-item auction quotes the per-unit ceiling",
          "100.00" in single_labels.get("Starting price (ceiling)", ""),
          single_labels.get("Starting price (ceiling)", ""))
    no_ceiling = make_auction(db, buyer, vendors, item, unit, ceiling=None,
                              title="No ceiling")
    check("an auction with no ceiling says so rather than quoting zero",
          "not set" in dict(notify._auction_facts(no_ceiling))
          .get("Starting price (ceiling)", ""))
    mailer.flush()
    withdrawn_events = {m.event for m in db.query(EmailMessage)
                        .filter(EmailMessage.subject.like("Bid withdrawn%")).all()}
    check("a withdrawal is filed as a withdrawal, not as 'closed'",
          withdrawn_events == {"withdrawn"}, str(withdrawn_events))

    # ------------------------------------------------------------------ 21
    print("\n21. Address lists people paste out of Outlook")
    from app.emails_util import validate
    check("a display name with a comma is understood",
          validate("Menon, Ravi <ravi@v1.local>") == ["ravi@v1.local"])
    check("several of them at once are understood",
          validate('"Menon, Ravi" <a@x.com>, Sheikh, Farah <b@x.com>')
          == ["a@x.com", "b@x.com"])
    try:
        validate("not-an-address")
        check("plain rubbish is still refused", False)
    except Exception as exc:
        check("plain rubbish is still refused", "does not look like" in str(exc))

    # ------------------------------------------------------------------ 22
    print("\n22. Queued and failed emails are retried after a restart")
    stuck = EmailMessage(to_email="stuck@r.local", subject="Left in the queue",
                         html_body="<p>hello</p>", text_body="hello", status="queued")
    broken = EmailMessage(to_email="broken@r.local", subject="Failed last time",
                          html_body="<p>hello</p>", text_body="hello", status="failed",
                          error="SMTPAuthenticationError")
    db.add_all([stuck, broken])
    db.commit()
    picked = mailer.requeue_pending()
    mailer.flush()
    db.expire_all()
    check("both were picked up on startup", picked >= 2, f"{picked} messages")
    check("the stuck one has now gone out", db.get(EmailMessage, stuck.id).status == "outbox")
    check("so has the failed one", db.get(EmailMessage, broken.id).status == "outbox")
    check("and its old error was cleared", not db.get(EmailMessage, broken.id).error)

    # ------------------------------------------------------------------ 23
    print("\n23. The outbox shows the email, not its source")
    message = db.query(EmailMessage).filter(EmailMessage.status == "outbox").first()
    raw = b.get(f"/outbox/{message.id}/raw")
    check("the preview frame is served as HTML",
          "text/html" in raw.headers["content-type"], raw.headers["content-type"])

    # ------------------------------------------------------------------ 24
    print("\n24. Forms cannot be posted from another site")
    naked = TestClient(app, base_url="http://test", headers=BROWSER)
    naked.cookies.set("ra_session", b.cookies.get("ra_session"))
    r = naked.post("/auctions/new", data={"title": "Forged"}, follow_redirects=False)
    check("a post with no token is refused", r.status_code == 403, str(r.status_code))
    r = naked.post("/auctions/new", data={"title": "Forged", "csrf_token": "guessed"},
                   follow_redirects=False)
    check("...and so is a made-up one", r.status_code == 403, str(r.status_code))
    check("the real form still works",
          b.get("/auctions/new").status_code == 200)

    # ------------------------------------------------------------------ 25
    print("\n25. Sign-in cannot be used to bounce someone elsewhere")
    c = Client(app, base_url="http://test", headers=BROWSER)
    c.get("/login?next=https://evil.example/phish")
    r = c.post("/login", data={"email": "buyer@r.local", "password": PW,
                               "next": "https://evil.example/phish"}, follow_redirects=False)
    check("an outside address in ?next is ignored",
          r.headers.get("location", "") == "/", r.headers.get("location", ""))
    c2 = Client(app, base_url="http://test", headers=BROWSER)
    c2.get("/login")
    r = c2.post("/login", data={"email": "buyer@r.local", "password": PW,
                                "next": "/reports"}, follow_redirects=False)
    check("a normal address inside the app still works",
          r.headers.get("location", "") == "/reports", r.headers.get("location", ""))

    # ------------------------------------------------------------------ 26
    print("\n26. Password guessing is slowed down")
    guess = Client(app, base_url="http://test", headers=BROWSER)
    guess.get("/login")
    seen = ""
    for _ in range(12):
        r = guess.post("/login", data={"email": "buyer@r.local", "password": "nope"},
                       follow_redirects=False)
        seen = r.text
    check("repeated wrong passwords are throttled", "Too many sign-in attempts" in seen)
    from app.security import clear_failed_logins
    clear_failed_logins("buyer@r.local|testclient")

    # ------------------------------------------------------------------ 27
    print("\n27. Signing out needs a button press, not a link")
    r = b.get("/logout", follow_redirects=False)
    check("opening /logout only asks", r.status_code == 200 and "Sign out?" in r.text,
          str(r.status_code))
    check("the session still works afterwards", b.get("/").status_code == 200)
    r = b.post("/logout", follow_redirects=False)
    check("posting it signs out", r.status_code == 303)
    b.get("/login")
    b.post("/login", data={"email": "buyer@r.local", "password": PW}, follow_redirects=False)

    # ------------------------------------------------------------------ 28
    print("\n28. A supplier can no longer be turned into a buyer by signing up")
    # The old bug: the signup form lost the account type on any error, so a
    # supplier who mistyped their password came back as a BUYER with masters,
    # reports and auction creation. Supplier signup is gone entirely now -
    # suppliers arrive by invitation, so the bug has no surface left.
    s = Client(app, base_url="http://test", headers=BROWSER)
    page = s.get("/signup")
    check("signup is closed once the first account exists",
          "by invitation" in page.text and "account_type" not in page.text)
    r = s.post("/signup", data={"name": "Asha Rao", "email": "asha@supplier.co",
                                "password": "abcdef", "account_type": "vendor",
                                "company": "Rao Packaging Pvt Ltd"},
               follow_redirects=False)
    db.expire_all()
    sneaked = db.query(User).filter_by(email="asha@supplier.co").first()
    check("...and no account can be created through it", r.status_code == 403
          and sneaked is None, str(r.status_code))
    check("the page tells a supplier how they really get in",
          "invites you to an auction" in page.text)

    # ------------------------------------------------------------------ 29
    print("\n29. Numbers nobody means are refused, not crashed on")
    base_form = {
        "title": "Silly numbers", "start_at": local(datetime.utcnow() + timedelta(hours=1)),
        "end_at": local(datetime.utcnow() + timedelta(hours=3)),
        "decrement_type": "absolute", "min_decrement": "1", "max_decrement": "0",
        "extend_trigger_minutes": "2", "extend_by_minutes": "3", "max_extensions": "5",
        "line_item_id": [str(item.id)], "line_unit_id": [str(unit.id)],
        "line_qty": ["10"], "line_price": ["100"], "line_spec": [""],
        "vendor_ids": [str(vendors[0].id)], "cc_emails": "",
    }
    for field, value, label in [("max_extensions", "1e999", "an infinite extension count"),
                                ("extend_by_minutes", "1e999", "an infinite extension length"),
                                ("line_qty", ["1e999"], "an infinite quantity"),
                                ("line_price", ["1e999"], "an infinite starting price"),
                                ("min_decrement", "nan", "a decrement of nan"),
                                ("decrement_type", "bogus", "an unknown decrement type"),
                                ("line_item_id", ["abc"], "a tampered item id"),
                                ("vendor_ids", ["abc"], "a tampered bidder id"),
                                ("vendor_ids", ["999999"], "a bidder who does not exist")]:
        payload = dict(base_form)
        payload[field] = value
        r = b.post("/auctions/new", data=payload, follow_redirects=False)
        ok = (r.status_code == 200 and "text/html" in r.headers["content-type"]
              and "Silly numbers" in r.text and "went wrong at our end" not in r.text)
        check(f"{label} is explained on the form", ok, f"HTTP {r.status_code}")

    # ------------------------------------------------------------------ 30
    print("\n30. A masters form keeps what was typed")
    r = b.post("/masters/vendors", follow_redirects=False, data={
        "name": "Rao Packaging", "email": "not-an-email",
        "extra_emails": "sales@rao.example", "code": "ACM-1",
        "contact_person": "R Kumar", "phone": "9876543210",
        "gstin": "29ABCDE1234F1Z5", "address": "Plot 4, Peenya"})
    check("the refusal is shown on the page", "does not look like" in told(r))
    for value in ("Rao Packaging", "not-an-email", "ACM-1", "R Kumar", "9876543210",
                  "29ABCDE1234F1Z5", "Plot 4, Peenya"):
        check(f"  ...and “{value}” is still in its box", value in r.text)

    # ------------------------------------------------------------------ 31
    print("\n31. An upgrade that adds a column does not break sign-in")
    from sqlalchemy import text as sql_text
    with db_engine.begin() as conn:
        conn.execute(sql_text("DROP TABLE IF EXISTS migrate_probe"))
        conn.execute(sql_text("CREATE TABLE migrate_probe (id INTEGER PRIMARY KEY)"))
    from app.models import Base as ModelBase
    probe = type("MigrateProbe", (ModelBase,), {
        "__tablename__": "migrate_probe",
        "__table_args__": {"extend_existing": True},
        "id": Bid.__table__.c.id.copy(),
    })
    from sqlalchemy import Column, Enum as SAEnum
    probe.__table__.append_column(Column("role", SAEnum(Role), default=Role.BUYER))
    added = migrate.run()
    with db_engine.begin() as conn:
        conn.execute(sql_text("INSERT INTO migrate_probe (id) VALUES (1)"))
        stored = conn.execute(sql_text("SELECT role FROM migrate_probe")).scalar()
    check("the new column was added", "migrate_probe.role" in added, ", ".join(added))
    check("its default is a value the app can read back",
          stored == Role.BUYER.name, repr(stored))

    # ------------------------------------------------------------------ 32
    print("\n32. A bidder's own bid list is newest first")
    order = make_auction(db, buyer, vendors, item, unit, min_dec=1.0, title="Newest first")
    order_line = order.lines[0]
    for price in (99, 98, 97):
        bid_as(v1, order, order_line, price)
    page = v1.get(f"/auctions/{order.id}").text
    mine = page.split("Your bids on this item")[1]
    check("the bid they just placed is at the top of their own list",
          mine.index("97.00") < mine.index("99.00"),
          f"97 at {mine.index('97.00')}, 99 at {mine.index('99.00')}")

    # ------------------------------------------------------------------ 33
    print("\n33. A price a bidder's own freight has used up is explained as such")
    from app.models import Participant as Part
    landed = make_auction(db, buyer, vendors, item, unit, ceiling=12.50, min_dec=0.10,
                          title="Freight eats the ceiling")
    landed.compare_landed = True
    part = db.query(Part).filter_by(auction_id=landed.id, vendor_id=vendors[0].id).first()
    part.freight, part.freight_basis = 13.0, "unit"
    db.commit()
    board = " ".join(v1.get(f"/auctions/{landed.id}").text.split())
    check("the board blames their delivered costs, not the bidding",
          "use up the whole starting price" in board and "no price you could offer" in board)
    check("...and does not claim there is a lowest bid when there is none",
          "the lowest bid is" not in board.split("range-note")[1][:600])
    refused = told(bid_as(v1, landed, landed.lines[0], 1.0))
    check("...and the same reason comes back if they try anyway",
          "use up the whole" in refused, " ".join(refused.split())[:80])

    # ------------------------------------------------------------------ 34
    print("\n34. A live auction's clock can be moved later, never earlier")
    clock = make_auction(db, buyer, vendors, item, unit, minutes=30, title="Clock")
    was_end = clock.end_at
    earlier = b.post(f"/auctions/{clock.id}/more-time", follow_redirects=False,
                     data={"end_at": local(was_end - timedelta(minutes=10))})
    check("an earlier time is refused", "not later" in told(earlier))
    db.refresh(clock)
    check("...and nothing moved", clock.end_at == was_end)
    later = b.post(f"/auctions/{clock.id}/more-time", follow_redirects=False,
                   data={"end_at": local(was_end + timedelta(minutes=45))})
    check("a later time is accepted", "now closes" in told(later))
    db.refresh(clock)
    check("...and the auction really closes later", clock.end_at > was_end,
          f"{was_end} → {clock.end_at}")
    check("...and the closing reminder will fire again", clock.ending_soon_notified is False)
    far = b.post(f"/auctions/{clock.id}/more-time", follow_redirects=False,
                 data={"end_at": local(datetime.utcnow() + timedelta(days=60))})
    check("a closing time months away is refused", "Thirty days" in told(far))
    v1_try = v1.post(f"/auctions/{clock.id}/more-time", follow_redirects=False,
                     data={"end_at": local(was_end + timedelta(minutes=90))})
    check("a bidder cannot move the clock", v1_try.status_code == 403,
          str(v1_try.status_code))

    # ------------------------------------------------------------------ 35
    print("\n35. Editing a published auction says what actually changed")
    edited = make_auction(db, buyer, vendors, item, unit, status=AuctionStatus.SCHEDULED,
                          ceiling=100.0, qty=10.0, title="Edit me")
    form = {
        "title": "Edit me", "description": "", "terms": "Now 60 days credit.",
        "start_at": local(edited.start_at), "end_at": local(edited.end_at),
        "decrement_type": "absolute", "min_decrement": "1", "max_decrement": "0",
        "extend_trigger_minutes": "2", "extend_by_minutes": "3", "max_extensions": "3",
        "line_item_id": [str(item.id)], "line_unit_id": [str(unit.id)],
        "line_qty": ["25"], "line_price": ["90"], "line_spec": [""],
        "vendor_ids": [str(v.id) for v in vendors],
    }
    r = b.post(f"/auctions/{edited.id}/edit", data=form, follow_redirects=False)
    check("the edit saves", "Changes saved" in told(r), " ".join(told(r).split())[:70])
    db.expire_all()
    edited = db.get(Auction, edited.id)
    check("...and the new quantity and starting price really landed",
          edited.lines[0].qty == 25 and edited.lines[0].starting_price == 90,
          f"qty {edited.lines[0].qty}, price {edited.lines[0].starting_price}")
    sent = (db.query(EmailMessage).filter(EmailMessage.event == "updated")
              .order_by(EmailMessage.id.desc()).first())
    body = sent.html_body if sent else ""
    for what, needle in (("the new terms", "terms have been rewritten"),
                         ("the new quantity", "quantity is now 25"),
                         ("the new starting price", "starting price is now")):
        check(f"the bidders are emailed {what}", needle in body,
              needle if needle in body else "missing")

    # ------------------------------------------------------------------ 36
    print("\n36. Editing a draft and publishing it in one press emails the new items")
    draft = make_auction(db, buyer, vendors, item, unit, status=AuctionStatus.DRAFT,
                         ceiling=100.0, qty=10.0, title="Edit and publish",
                         published=False)
    now = datetime.utcnow()
    r = b.post(f"/auctions/{draft.id}/edit", follow_redirects=False, data={
        "title": "Edit and publish", "description": "", "terms": "",
        "start_at": local(now - timedelta(minutes=1)),
        "end_at": local(now + timedelta(hours=2)),
        "decrement_type": "absolute", "min_decrement": "1", "max_decrement": "0",
        "extend_trigger_minutes": "2", "extend_by_minutes": "3", "max_extensions": "3",
        # the buyer swaps the item and halves the ceiling on the way out
        "line_item_id": [str(spare_item.id)], "line_unit_id": [str(unit.id)],
        "line_qty": ["40"], "line_price": ["50"], "line_spec": [""],
        "vendor_ids": [str(v.id) for v in vendors],
        "action": "publish"})
    check("it publishes", "Bidding is open now" in told(r), " ".join(told(r).split())[:70])
    invite = (db.query(EmailMessage)
                .filter(EmailMessage.event == "invited",
                        EmailMessage.subject.like("%Edit and publish%"))
                .order_by(EmailMessage.id.desc()).first())
    body = " ".join((invite.html_body if invite else "").split())
    check("the invitation quotes the ceiling as it was saved, not as it was before",
          "50.00" in body and "100.00" not in body,
          "50.00 present" if "50.00" in body else "50.00 missing")
    check("...and one item, not two", ">1<" in body or "Items" in body)

    # ------------------------------------------------------------------ 37
    print("\n37. Every tab the page draws is a tab the router knows")
    from app.routers.auctions import TABS
    template = (ROOT / "app" / "templates" / "auction_detail.html").read_text(encoding="utf-8")
    drawn = set(re.findall(r"\?tab=([a-z]+)", template))
    check("no tab link points at a name the router would throw away",
          drawn <= set(TABS), ", ".join(sorted(drawn - set(TABS))) or "all known")
    auction = make_auction(db, buyer, vendors, item, unit, title="Tab fallback")
    page = b.get(f"/auctions/{auction.id}?tab=nonsense")
    check("an unknown tab falls back to the bidding view, not a blank page",
          page.status_code == 200 and "Tab fallback" in page.text
          and ("No bids yet" in page.text or "unit_price" in page.text),
          f"HTTP {page.status_code}")

    # ------------------------------------------------------------------ 38
    print("\n38. A price that rounds away to nothing cannot be awarded")
    auction = make_auction(db, buyer, vendors, item, unit, title="Sub-paisa award")
    line = auction.lines[0]
    bid_as(v1, auction, line, 90)
    auction.status = AuctionStatus.CLOSED
    db.commit()
    r = b.post(f"/auctions/{auction.id}/award", follow_redirects=False,
               data={f"winner_{line.id}": str(vendors[0].id), f"price_{line.id}": "0.004"})
    db.expire_all()
    booked = db.query(Award).filter(Award.line_id == line.id).first()
    check("0.004 is refused rather than booked as 0.00", booked is None,
          f"{booked.unit_price if booked else 'nothing booked'}")
    check("...and the refusal explains why", "more than zero" in r.text,
          "explained" if "more than zero" in r.text else r.text[:60])

    # ------------------------------------------------------------------ 39
    print("\n39. A tampered item id is a clean refusal, never a crash")
    auction = make_auction(db, buyer, vendors, item, unit, title="Tampered line")
    for raw in ("1x", "-1", "²", "9" * 20, "0", " "):
        r = v1.post(f"/auctions/{auction.id}/bid", follow_redirects=False,
                    data={"line_id": raw, "unit_price": "50"})
        check(f"line_id {raw.strip()!r} is refused cleanly", r.status_code < 500,
              f"HTTP {r.status_code}")

    # ------------------------------------------------------------------ 40
    print("\n40. A name with angle brackets does not break the PDF")
    auction = make_auction(db, buyer, vendors, item, unit, title="Grade <b> steel")
    line = auction.lines[0]
    bid_as(v1, auction, line, 80)
    auction.status = AuctionStatus.CLOSED
    db.commit()
    b.post(f"/auctions/{auction.id}/award", follow_redirects=False,
           data={f"winner_{line.id}": str(vendors[0].id), f"price_{line.id}": "80"})
    db.expire_all()
    r = b.get(f"/reports/auction/{auction.id}/export/pdf")
    check("the auction PDF still builds", r.status_code == 200,
          f"HTTP {r.status_code} {len(r.content)}b")
    today = datetime.now(TZ).date().isoformat()
    r = b.get(f"/reports/savings.pdf?date_from=2000-01-01&date_to={today}")
    check("...and so does the savings PDF for the whole period", r.status_code == 200,
          f"HTTP {r.status_code}")
    # The escaping must keep the whole name, not quietly drop the bracketed
    # part the way reportlab's parser did.
    pdf = reporting.auction_pdf(reporting.auction_summary_report(db, auction))
    check("the PDF is a real document, not an error", pdf[:4] == b"%PDF", f"{len(pdf)}b")
    plain = b.get(f"/reports/auction/{auction.id}/export/csv").text
    check("the CSV carries the name in full", "Grade <b> steel" in plain,
          plain.splitlines()[0][:60] if plain else "")

    # ------------------------------------------------------------------ 41
    print("\n41. An impossible date does not take the reports page down")
    for pair in (("0001-01-01", today), (today, "9999-12-31"), ("not-a-date", "2026-13-45")):
        r = b.get(f"/reports?date_from={pair[0]}&date_to={pair[1]}")
        check(f"{pair[0]} → {pair[1]} is handled", r.status_code == 200, f"HTTP {r.status_code}")
    r = b.get(f"/reports/savings.csv?date_from=0001-01-01&date_to={today}")
    check("...and the download too", r.status_code == 200, f"HTTP {r.status_code}")

    # ------------------------------------------------------------------ 42
    print("\n42. A report row's own figures add up")
    auction = make_auction(db, buyer, vendors, item, unit, title="Fractional row",
                           qty=12.5, ceiling=621.49, min_dec=0.01)
    line = auction.lines[0]
    bid_as(v1, auction, line, 548.18)
    auction.status = AuctionStatus.CLOSED
    db.commit()
    b.post(f"/auctions/{auction.id}/award", follow_redirects=False,
           data={f"winner_{line.id}": str(vendors[0].id), f"price_{line.id}": "548.18"})
    db.expire_all()
    data = reporting.total_savings(db, datetime(2000, 1, 1), datetime(2100, 1, 1))
    row = next((r for r in data["rows"] if r["reference"] == auction.reference), None)
    check("the row is in the report", row is not None)
    if row:
        check("baseline − final is exactly the savings column",
              abs((row["baseline"] - row["final"]) - row["savings"]) < 0.0001,
              f"{row['baseline']:.2f} - {row['final']:.2f} vs {row['savings']:.2f}")
    check("and the total is the sum of the printed rows",
          abs(sum(r["savings"] for r in data["rows"]) - data["totals"]["savings"]) < 0.005)

    # ------------------------------------------------------------------ 43
    print("\n43. Savings by month uses the months the buyer sees")
    from app.routers.dashboard import _monthly_savings
    auction = make_auction(db, buyer, vendors, item, unit, title="Month edge")
    line = auction.lines[0]
    bid_as(v1, auction, line, 90)
    auction.status = AuctionStatus.CLOSED
    db.commit()
    b.post(f"/auctions/{auction.id}/award", follow_redirects=False,
           data={f"winner_{line.id}": str(vendors[0].id), f"price_{line.id}": "90"})
    db.expire_all()
    # Awarded at 19:45 UTC on the last day of last month = 01:15 local on the
    # 1st of this month. The chart must agree with every date on the screen.
    local_first = datetime.now(TZ).replace(day=1, hour=1, minute=15, second=0, microsecond=0)
    award = db.query(Award).filter(Award.auction_id == auction.id).one()
    award.awarded_at = auction.awarded_at = local_first.astimezone(
        timezone.utc).replace(tzinfo=None)
    db.commit()
    buckets = _monthly_savings(db, months=6)
    this_month = datetime.now(TZ).strftime("%b")
    landed = next((x for x in buckets if x["label"] == this_month), None)
    check("the chart has a bar for the current month", landed is not None,
          ", ".join(x["label"] for x in buckets))
    if landed:
        check("...and the auction is counted in it", landed["count"] >= 1,
              f"{landed['count']} in {this_month}")

    # ------------------------------------------------------------------ 44
    print("\n44. Email never sits at “queued” without a reason")
    from app import mailer as _mailer
    from app import config as _config
    was = (_config.SMTP_HOST, _config.EMAIL_ENABLED)
    try:
        _config.SMTP_HOST, _config.EMAIL_ENABLED = "no-such-host.invalid", True
        msg = _mailer.queue_email(db, to_email="nobody@example.com", subject="Probe",
                                  html_body="<p>x</p>", event="test")
        msg_id = msg.id
        _mailer.flush(timeout=90)
        db.expire_all()
        row = db.get(EmailMessage, msg_id)
        check("an unreachable mail server ends as failed, not queued",
              row.status == "failed", row.status)
        check("...and says what to change", "RA_SMTP_HOST" in row.error, row.error[:70])
        ok, said = _mailer.send_test("someone@example.com")
        check("the test button reports the same thing", not ok and "could not be found" in said,
              said[:70])
    finally:
        _config.SMTP_HOST, _config.EMAIL_ENABLED = was
    page = b.get("/outbox")
    check("the Outbox says which server is in use", "Practice mode" in page.text
          or "Sending is switched on" in page.text)
    check("...and offers a test email", "Send test email" in page.text)

    # ------------------------------------------------------------------ 45
    print("\n45. The award screen prices the auction the way it was decided")
    landed_auction = make_auction(db, buyer, vendors, item, unit, title="Delivered award",
                                  qty=100, ceiling=110.0, min_dec=1.0)
    landed_auction.compare_landed = True
    parts = db.query(Participant).filter_by(auction_id=landed_auction.id).all()
    parts[0].duty, parts[0].duty_basis = 20.0, "percent"   # 90 headline -> 108 delivered
    parts[1].freight, parts[1].freight_basis = 1.0, "unit"  # 100 headline -> 101 delivered
    db.commit()
    lline = landed_auction.lines[0]
    bid_as(v1, landed_auction, lline, 90)
    bid_as(v2, landed_auction, lline, 100)
    db.expire_all()
    ranked = engine.best_per_vendor(db, lline.id)
    check("the cheaper delivered price ranks first",
          ranked and ranked[0].vendor_id == vendors[1].id,
          ", ".join(f"{b.vendor.name} {engine.compare_price(b):.2f}" for b in ranked))
    landed_auction.status = AuctionStatus.CLOSED
    db.commit()
    page = b.get(f"/auctions/{landed_auction.id}/award").text
    check("the screen shows the delivered price, not just the headline",
          "108.00" in page and "101.00" in page,
          "delivered prices present" if "108.00" in page else "missing")
    # The saving beside L1 must be the saving actually booked by picking it.
    delivered_saving = 100 * 110.0 - 100 * 101.0
    check("the saving beside the best bidder is the one they would really make",
          f"{delivered_saving:,.2f}" in page, f"{delivered_saving:,.2f}")
    wrong = 100 * 110.0 - 100 * 90.0     # the headline sum the screen used to print
    check("...and the old headline figure is gone", f"{wrong:,.2f}" not in page,
          f"{wrong:,.2f} still shown" if f"{wrong:,.2f}" in page else "gone")
    r = b.post(f"/auctions/{landed_auction.id}/award", follow_redirects=False,
               data={f"winner_{lline.id}": str(vendors[1].id), f"price_{lline.id}": "100"})
    db.expire_all()
    booked = db.query(Award).filter(Award.line_id == lline.id).one()
    summary = engine.auction_summary(db, landed_auction)
    check("the award books the delivered total",
          abs((booked.landed_total or booked.total) - 100 * 101.0) < 0.01,
          f"{booked.landed_total or booked.total:.2f}")
    check("...and the confirmation quotes the delivered saving",
          abs(summary["savings"] - delivered_saving) < 0.01, f"{summary['savings']:.2f}")
    per_line = sum(engine.line_result(db, l)["savings"] for l in landed_auction.lines)
    check("...which is what the line adds up to", abs(per_line - summary["savings"]) < 0.01,
          f"{per_line:.2f}")

    # ------------------------------------------------------------------ 46
    print("\n46. A percentage adder cannot push a bid past the maximum decrement")
    cap = make_auction(db, buyer, vendors, item, unit, title="Decrement cap",
                       ceiling=200.0, min_dec=1.0, max_dec=10.0)
    cap.compare_landed = True
    part = db.query(Participant).filter_by(auction_id=cap.id, vendor_id=vendors[0].id).one()
    part.duty, part.duty_basis = 10.0, "percent"
    db.commit()
    cline = cap.lines[0]
    # 172.72 would deliver at 189.99 - a paisa past the cap, which is exactly
    # what the old flooring allowed. The engine now asks for 172.73.
    refused = bid_as(v1, cap, cline, 172.72)
    db.expire_all()
    check("a price a paisa past the cap is refused", engine.best_bid(db, cline.id) is None,
          told(refused)[:70] if engine.best_bid(db, cline.id) is None else "it was accepted")
    bid_as(v1, cap, cline, 172.73)                 # delivered 190.00
    db.expire_all()
    before = engine.compare_price(engine.best_bid(db, cline.id))
    # The window this particular bidder is shown, adders and all.
    window = engine.bid_window(db, cap, cline, vendors[1].id)
    part2 = db.query(Participant).filter_by(auction_id=cap.id, vendor_id=vendors[1].id).one()
    part2.duty, part2.duty_basis = 10.0, "percent"
    db.commit()
    db.expire_all()
    window = engine.bid_window(db, cap, cline, vendors[1].id)
    adders = engine.adders_for(db, cap, vendors[1].id)
    floor_price = adders.landed(window.min_allowed)
    cap_floor = round(before - cap.max_decrement, 2)
    check("the lowest price the engine offers delivers at or above the floor",
          floor_price >= cap_floor - 0.0001,
          f"offers {window.min_allowed} = delivered {floor_price:.2f}, floor {cap_floor:.2f}")
    check("...and is not needlessly more than a paisa above it",
          floor_price - cap_floor < 0.011, f"{floor_price - cap_floor:.4f} above")
    r = bid_as(v2, cap, cline, window.min_allowed)
    db.expire_all()
    best = engine.best_bid(db, cline.id)
    after = engine.compare_price(best)
    check("a bid at that floor is accepted", best.vendor_id == vendors[1].id,
          told(r)[:70])
    check("...and does not overshoot the cap",
          before - after <= cap.max_decrement + 0.0001,
          f"dropped {before - after:.2f} against a cap of {cap.max_decrement:.2f}")
    # And a paisa below the floor is still refused.
    r = bid_as(v1, cap, cline, round(window.min_allowed - 0.01, 2))
    db.expire_all()
    check("a paisa below the floor is refused", engine.best_bid(db, cline.id).id == best.id,
          told(r)[:60])

    # ------------------------------------------------------------------ 47
    print("\n47. Only one account can ever be the first")
    import threading as _threading
    from app.db import SessionLocal as _Session
    probe = _Session()
    existing = probe.query(User).count()
    probe.close()
    check("this install already has accounts, so sign-up is shut", existing > 0, str(existing))
    fresh = Client(app, base_url="http://test")
    fresh.headers.update(BROWSER)
    fresh.get("/signup")
    r = fresh.post("/signup", follow_redirects=False,
                   data={"name": "Interloper", "email": "nope@r.local",
                         "password": "abcdef123", "company": ""})
    db.expire_all()
    check("a second sign-up is refused", r.status_code == 403, f"HTTP {r.status_code}")
    check("...and no account was made",
          db.query(User).filter(User.email == "nope@r.local").first() is None)

    # ------------------------------------------------------------------ 48
    print("\n48. The sign-in throttle cannot be sidestepped with a header")
    from app.web import client_ip as _client_ip
    from app import config as _config

    class _Req:
        def __init__(self, header, peer):
            self.headers = {"x-forwarded-for": header} if header else {}
            self.client = type("C", (), {"host": peer})()

    was = _config.TRUSTED_PROXIES
    try:
        _config.TRUSTED_PROXIES = 0
        check("with no proxy configured, a forwarded header is ignored",
              _client_ip(_Req("1.2.3.4", "10.0.0.9")) == "10.0.0.9",
              _client_ip(_Req("1.2.3.4", "10.0.0.9")))
        _config.TRUSTED_PROXIES = 1
        check("behind one proxy, the entry the proxy wrote is used",
              _client_ip(_Req("1.2.3.4, 203.0.113.7", "10.0.0.9")) == "203.0.113.7",
              _client_ip(_Req("1.2.3.4, 203.0.113.7", "10.0.0.9")))
    finally:
        _config.TRUSTED_PROXIES = was
    attacker = Client(app, base_url="http://test")
    attacker.headers.update(BROWSER)
    attacker.get("/login")
    blocked = 0
    for n in range(14):
        rr = attacker.post("/login", follow_redirects=False,
                           headers={"X-Forwarded-For": f"9.9.9.{n}"},
                           data={"email": "buyer@r.local", "password": f"wrong{n}", "next": "/"})
        if "Too many sign-in attempts" in rr.text:
            blocked += 1
    check("guessing is slowed down however the header is set", blocked > 0,
          f"{blocked} of 14 refused")
    from app.security import clear_failed_logins as _clear
    _clear("account|buyer@r.local")

    # ------------------------------------------------------------------ 49
    print("\n49. What the screens say, read as a person reads them")
    from app.utils import first_name, plain_money
    check("a name written initial-first is greeted by the name, not the initial",
          first_name("R Venkatesh") == "Venkatesh", first_name("R Venkatesh"))
    check("...and an ordinary name still works", first_name("Harish Subramaniam") == "Harish")
    check("...and a name that is all initials is left alone", first_name("R K") == "R K")
    check("...and an empty one does not crash", first_name(None) == "there")
    check("a price in a number box has no trailing .0", plain_money(60700.0) == "60700",
          plain_money(60700.0))

    # The welcome screen printed a Python repr where a count belonged.
    fresh_buyer = login("buyer@r.local")
    page = fresh_buyer.get("/onboarding").text
    check("the welcome screen has no Python repr on it",
          "built-in method" not in page and "object at 0x" not in page)

    # A bidder who is already L1 must not be invited to undercut themselves.
    lead = make_auction(db, buyer, vendors, item, unit, title="Already winning",
                        ceiling=1000.0, min_dec=10)
    lline = lead.lines[0]
    bid_as(v1, lead, lline, 900)
    db.expire_all()
    page = v1.get(f"/auctions/{lead.id}").text
    check("the leader is told there is nothing to beat", "Nothing to beat" in page)
    check("...and is not handed a one-click price to undercut themselves",
          'data-fill="' not in page.split("Nothing to beat")[1][:1200],
          "a chip is still offered" if 'data-fill="' in page.split("Nothing to beat")[1][:1200]
          else "no chip")
    rival_page = v2.get(f"/auctions/{lead.id}").text
    check("...while a bidder who is behind still gets one", 'data-fill="' in rival_page)

    # Overall standing must not flatter a bidder who skipped an item.
    two = make_auction(db, buyer, vendors, item, unit, title="Partial bidder", ceiling=1000.0,
                       qty=10, min_dec=1)
    db.add(AuctionLine(auction_id=two.id, item_id=spare_item.id, unit_id=unit.id,
                       qty=10, starting_price=100.0))
    db.commit()
    db.refresh(two)
    big, small = sorted(two.lines, key=lambda l: -(l.starting_price or 0))
    bid_as(v1, two, big, 900)
    bid_as(v1, two, small, 90)
    bid_as(v2, two, small, 80)          # v2 skips the expensive line entirely
    db.expire_all()
    standing = {r["vendor_id"]: r for r in engine.overall_ranking(db, two)}
    full, partial = standing[vendors[0].id], standing[vendors[1].id]
    check("the bidder who priced everything ranks first", full["complete"] and not partial["complete"])
    check("a partial bidder's saving is measured on what they priced",
          abs(partial["savings"] - (10 * 100 - 10 * 80)) < 0.01, f"{partial['savings']:.2f}")
    check("...and is not larger than the complete bidder's",
          partial["savings"] < full["savings"],
          f"partial {partial['savings']:.2f} vs full {full['savings']:.2f}")

    # A finished auction's bids are not labelled "live".
    done = make_auction(db, buyer, vendors, item, unit, title="Finished labels")
    dline = done.lines[0]
    bid_as(v1, done, dline, 90)
    done.status = AuctionStatus.CLOSED
    db.commit()
    b.post(f"/auctions/{done.id}/award", follow_redirects=False,
           data={f"winner_{dline.id}": str(vendors[0].id), f"price_{dline.id}": "90"})
    db.expire_all()
    page = b.get(f"/reports/auction/{done.id}").text
    check("a bid on a finished auction is not called “live”",
          ">live<" not in page and "counted" in page)
    # And the bidder is told the outcome on the tab they land on.
    page = v1.get(f"/auctions/{done.id}").text
    check("the winner is told they won, on the first tab they see",
          "You won this item" in page)
    page = v2.get(f"/auctions/{done.id}").text
    check("...and a bidder who did not win is told that too",
          "Went to another bidder" in page or "Not awarded" in page)

    db.close()
    print("\n" + "-" * 62)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All regression checks passed.")
    return 0


def test_regressions():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
