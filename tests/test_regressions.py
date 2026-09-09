"""One check per bug found in the end-to-end bug hunt.

Every check here failed before the fix that goes with it, so if one of them
ever fails again the same defect is back.

    python tests/test_regressions.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
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
    print("\n28. Signup keeps what a supplier typed")
    s = Client(app, base_url="http://test", headers=BROWSER)
    s.get("/signup")
    r = s.post("/signup", data={"name": "Asha Rao", "email": "asha@supplier.co",
                                "password": "abc", "account_type": "vendor",
                                "company": "Rao Packaging Pvt Ltd"})
    check("the supplier choice survives the error",
          'value="vendor"\n               checked' in r.text
          or ('value="vendor"' in r.text and "checked" in r.text.split('value="vendor"')[1][:60]),
          "vendor still selected")
    check("the company name survives too", "Rao Packaging Pvt Ltd" in r.text)

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
