"""Scenario and arithmetic audit: the auction rules, proved with numbers.

Where the other suites drive the screens, this one pins down the maths and the
state machine — percentage decrements, ties, withdrawals, fractional
quantities, empty lines, re-awards, the clock, and the totals that end up in
front of management.

    python tests/test_scenarios.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-scenarios-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_TIMEZONE"] = "Asia/Kolkata"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient           # noqa: E402

from app import engine as E                         # noqa: E402
from app import mailer, reporting, scheduler        # noqa: E402
from app.db import Base, SessionLocal, engine       # noqa: E402
from app.main import app                            # noqa: E402
from app.models import (Organisation, Auction, AuctionLine, AuctionStatus, Award, Bid, DecrementType,  # noqa: E402
                        Item, Participant, Role, Unit, User, Vendor)
from app.security import hash_password              # noqa: E402

Base.metadata.create_all(bind=engine)
PW = "test1234"
FAILS: list[str] = []
BROWSER = {"accept": "text/html,application/xhtml+xml"}


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def near(a, b, tol=0.005):
    return abs(a - b) < tol


class Client(TestClient):
    """Repeats the CSRF cookie back as the header, exactly as the app's own
    JavaScript does. Without it every POST here would be refused."""

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


db = SessionLocal()
buyer = User(name="Buyer", email="buyer@s.local", role=Role.BUYER,
             password_hash=hash_password(PW))
db.add(buyer)
unit = Unit(code="KG")
db.add(unit)
db.flush()
VENDORS = []
for i in (1, 2, 3, 4):
    v = Vendor(name=f"Supplier {i}", email=f"v{i}@s.local")
    db.add(v)
    db.flush()
    db.add(User(name=f"Rep {i}", email=f"v{i}@s.local", role=Role.VENDOR, vendor_id=v.id,
                password_hash=hash_password(PW)))
    VENDORS.append(v)
ITEM = Item(name="Steel plate")
db.add(ITEM)
db.commit()

BUYER_C = login("buyer@s.local")
CLIENTS = {v.id: login(f"v{i + 1}@s.local") for i, v in enumerate(VENDORS)}
_ref = [0]


def new_auction(*, lines, status=AuctionStatus.LIVE, minutes=60, decrement_type=DecrementType.ABSOLUTE,
                min_dec=1.0, max_dec=0.0, auto_extend=False, trigger=120, extend_by=180,
                max_ext=3, invited=None, title="Scenario"):
    """lines: list of (qty, starting_price or None)."""
    _ref[0] += 1
    now = datetime.utcnow()
    a = Auction(reference=f"RA-S-{_ref[0]:04d}", title=title, creator_id=buyer.id, status=status,
                start_at=now - timedelta(minutes=5), end_at=now + timedelta(minutes=minutes),
                original_end_at=now + timedelta(minutes=minutes),
                decrement_type=decrement_type, min_decrement=min_dec, max_decrement=max_dec,
                auto_extend=auto_extend, extend_trigger_seconds=trigger,
                extend_by_seconds=extend_by, max_extensions=max_ext)
    db.add(a)
    db.flush()
    for qty, price in lines:
        db.add(AuctionLine(auction_id=a.id, item_id=ITEM.id, unit_id=unit.id, qty=qty,
                           starting_price=price))
    for v in (invited if invited is not None else VENDORS):
        db.add(Participant(auction_id=a.id, vendor_id=v.id))
    db.commit()
    db.refresh(a)
    return a


def place(auction, line, vendor, price):
    """Bid over HTTP, exactly as a bidder would. Returns the flash message."""
    r = CLIENTS[vendor.id].post(f"/auctions/{auction.id}/bid",
                                data={"line_id": str(line.id), "unit_price": str(price)},
                                follow_redirects=False)
    db.expire_all()
    return flash_of(r)


def main() -> int:
    # ------------------------------------------------------------------ A
    print("\nA. Decrements in both modes")
    a = new_auction(lines=[(10, 1000.0)], min_dec=10, max_dec=100)
    line = a.lines[0]
    w = E.bid_window(db, a, line)
    check("first bid may sit exactly on the ceiling", w.max_allowed == 1000.0, str(w.max_allowed))
    place(a, line, VENDORS[0], 1000)
    w = E.bid_window(db, a, line)
    check("next bid must be a full decrement below", w.max_allowed == 990.0, str(w.max_allowed))
    check("and no more than the maximum below", w.min_allowed == 900.0, str(w.min_allowed))
    msg = place(a, line, VENDORS[1], 990.01)
    check("a bid a paisa above the window is refused", "Too high" in msg, msg[:40])
    msg = place(a, line, VENDORS[1], 899.99)
    check("a bid a paisa below the window is refused", "one step" in msg, msg[:40])
    place(a, line, VENDORS[1], 990)
    check("a bid exactly on the boundary is accepted",
          E.best_bid(db, line.id).unit_price == 990.0)

    pct = new_auction(lines=[(1, 1000.0)], decrement_type=DecrementType.PERCENT,
                      min_dec=2, max_dec=10, title="Percent")
    pline = pct.lines[0]
    place(pct, pline, VENDORS[0], 1000)
    w = E.bid_window(db, pct, pline)
    check("2% of 1000 is a 20 step", near(w.max_allowed, 980.0), str(w.max_allowed))
    check("10% of 1000 caps the drop at 900", near(w.min_allowed, 900.0), str(w.min_allowed))
    place(pct, pline, VENDORS[1], 980)
    w = E.bid_window(db, pct, pline)
    check("the percentage follows the new price down", near(w.max_allowed, 960.4),
          str(w.max_allowed))
    msg = place(pct, pline, VENDORS[2], 961)
    check("...and enforces it", "Too high" in msg, msg[:36])

    free = new_auction(lines=[(5, 200.0)], min_dec=0, max_dec=0, title="No minimum")
    fline = free.lines[0]
    place(free, fline, VENDORS[0], 200)
    msg = place(free, fline, VENDORS[1], 199.99)
    check("with no minimum decrement, a paisa is enough",
          E.best_bid(db, fline.id).unit_price == 199.99, msg[:40])
    msg = place(free, fline, VENDORS[2], 199.99)
    check("but matching the best price is still refused",
          E.best_bid(db, fline.id).unit_price == 199.99 and msg != "", msg[:40])

    # ------------------------------------------------------------------ B
    print("\nB. Ties, ranking and withdrawal")
    t = new_auction(lines=[(2, 500.0)], min_dec=0, title="Ties")
    tline = t.lines[0]
    place(t, tline, VENDORS[0], 400)
    place(t, tline, VENDORS[1], 400.0)      # identical price, later
    ranked = E.best_per_vendor(db, tline.id)
    check("an equal price is refused, so the first bidder keeps L1",
          len(ranked) == 1 and ranked[0].vendor_id == VENDORS[0].id, str(len(ranked)))
    place(t, tline, VENDORS[1], 399)
    place(t, tline, VENDORS[2], 398)
    ranked = E.best_per_vendor(db, tline.id)
    check("ranks read cheapest first",
          [b.unit_price for b in ranked] == [398.0, 399.0, 400.0],
          ", ".join(str(b.unit_price) for b in ranked))
    check("each vendor appears once", len({b.vendor_id for b in ranked}) == 3)
    check("the highest bid is the worst price offered",
          E.highest_bid(db, tline.id).unit_price == 400.0)

    best = E.best_bid(db, tline.id)
    CLIENTS[VENDORS[2].id].post(f"/auctions/{t.id}/bids/{best.id}/withdraw",
                                data={"reason": "mistake"}, follow_redirects=False)
    db.expire_all()
    check("withdrawing L1 promotes the next bidder",
          E.best_bid(db, tline.id).unit_price == 399.0)
    check("a withdrawn bid is not the highest either",
          E.highest_bid(db, tline.id).unit_price == 400.0)
    check("the withdrawn vendor drops out of the ranking",
          VENDORS[2].id not in {b.vendor_id for b in E.best_per_vendor(db, tline.id)})
    check("that vendor may bid again after withdrawing",
          place(t, tline, VENDORS[2], 397) != "" and E.best_bid(db, tline.id).unit_price == 397.0)

    empty = new_auction(lines=[(3, 90.0)], title="All withdrawn")
    eline = empty.lines[0]
    place(empty, eline, VENDORS[0], 80)
    only = E.best_bid(db, eline.id)
    CLIENTS[VENDORS[0].id].post(f"/auctions/{empty.id}/bids/{only.id}/withdraw",
                                follow_redirects=False)
    db.expire_all()
    check("a line whose every bid was withdrawn has no best bid",
          E.best_bid(db, eline.id) is None)
    result = E.line_result(db, eline)
    check("...its baseline falls back to the ceiling", near(result["baseline"], 270.0))
    check("...and its savings are zero, not negative", near(result["savings"], 0.0),
          str(result["savings"]))
    check("...the award screen still opens",
          BUYER_C.get(f"/auctions/{empty.id}/award", follow_redirects=False).status_code
          in (200, 303))

    # ------------------------------------------------------------------ C
    print("\nC. Fractional quantities and rounding")
    fr = new_auction(lines=[(2.5, 33.333)], min_dec=0.01, title="Fractions")
    frline = fr.lines[0]
    place(fr, frline, VENDORS[0], 33.333)
    bid = E.best_bid(db, frline.id)
    check("the price is stored rounded to paise", bid.unit_price == 33.33, str(bid.unit_price))
    check("the line total is the rounded price times the quantity",
          near(bid.total, round(2.5 * 33.33, 2)), str(bid.total))
    res = E.line_result(db, frline)
    check("savings on a fractional quantity are right",
          near(res["savings"], 2.5 * 33.333 - 2.5 * 33.33), f"{res['savings']:.4f}")

    # ------------------------------------------------------------------ D
    print("\nD. A line with no ceiling")
    open_a = new_auction(lines=[(4, None)], min_dec=5, title="No ceiling")
    oline = open_a.lines[0]
    w = E.bid_window(db, open_a, oline)
    check("the window is open until someone bids", w.open_ended and w.max_allowed is None)
    check("baseline is zero while nobody has bid", near(E.line_baseline(db, oline), 0.0))
    s = E.auction_summary(db, open_a)
    check("...and the savings percentage does not divide by zero",
          near(s["savings_pct"], 0.0) and near(s["savings"], 0.0))
    place(open_a, oline, VENDORS[0], 700)
    place(open_a, oline, VENDORS[1], 650)
    place(open_a, oline, VENDORS[2], 600)
    check("baseline becomes the highest bid received",
          near(E.line_baseline(db, oline), 4 * 700), str(E.line_baseline(db, oline)))
    res = E.line_result(db, oline)
    check("savings run from that highest bid", near(res["savings"], 4 * (700 - 600)),
          str(res["savings"]))
    check("the highest column shows 700, not 650",
          res["highest"].unit_price == 700.0, str(res["highest"].unit_price))

    # ------------------------------------------------------------------ E
    print("\nE. Awarding, re-awarding and the totals")
    mix = new_auction(lines=[(10, 100.0), (5, None), (2, 50.0)], min_dec=1, title="Mixed")
    l1, l2, l3 = mix.lines
    place(mix, l1, VENDORS[0], 95)
    place(mix, l1, VENDORS[1], 90)
    place(mix, l2, VENDORS[0], 300)
    place(mix, l2, VENDORS[1], 280)
    # nobody bids on l3
    expected_baseline = 10 * 100 + 5 * 300 + 2 * 50
    s = E.auction_summary(db, mix)
    check("baseline mixes ceilings and, where none, the highest bid",
          near(s["baseline"], expected_baseline), f"{s['baseline']:.0f}")
    check("before awarding, the final value uses the best bids",
          near(s["final_value"], 10 * 90 + 5 * 280 + 2 * 50), f"{s['final_value']:.0f}")

    mix.status = AuctionStatus.CLOSED
    db.commit()
    r = BUYER_C.post(f"/auctions/{mix.id}/award", follow_redirects=False, data={
        f"winner_{l1.id}": str(VENDORS[1].id), f"price_{l1.id}": "90",
        f"winner_{l2.id}": str(VENDORS[0].id), f"price_{l2.id}": "300",
        f"winner_{l3.id}": "",
    })
    db.expire_all()
    awards = db.query(Award).filter_by(auction_id=mix.id).all()
    check("one award per awarded line, quantity in full",
          len(awards) == 2 and all(near(x.qty, x.line.qty) for x in awards))
    check("an item may be left unawarded",
          l3.id not in {x.line_id for x in awards})
    s = E.auction_summary(db, mix)
    check("the unawarded line keeps its baseline in the final value",
          near(s["final_value"], 10 * 90 + 5 * 300 + 2 * 50), f"{s['final_value']:.0f}")
    check("savings are baseline minus that", near(s["savings"], expected_baseline - s["final_value"]))
    check("the confirmation quotes the same number",
          f"{s['savings']:,.2f}" in flash_of(r), flash_of(r)[:70])

    before_emails = len([m for m in mailer_all() if m.event == "awarded"])
    r = BUYER_C.post(f"/auctions/{mix.id}/award", follow_redirects=False, data={
        f"winner_{l1.id}": str(VENDORS[0].id), f"price_{l1.id}": "95",
        f"winner_{l2.id}": str(VENDORS[1].id), f"price_{l2.id}": "280",
        f"winner_{l3.id}": "",
    })
    db.expire_all()
    awards = db.query(Award).filter_by(auction_id=mix.id).all()
    check("re-awarding replaces the old award rather than adding to it", len(awards) == 2)
    check("the new winners are recorded",
          {x.vendor_id for x in awards} == {VENDORS[0].id, VENDORS[1].id})
    mailer.flush()
    check("the new winners were emailed",
          len([m for m in mailer_all() if m.event == "awarded"]) > before_emails)
    s = E.auction_summary(db, mix)
    check("savings follow the revised award",
          near(s["final_value"], 10 * 95 + 5 * 280 + 2 * 50), f"{s['final_value']:.0f}")
    per_line = sum(E.line_result(db, l)["savings"] for l in mix.lines)
    check("the line savings add up to the auction savings", near(per_line, s["savings"]),
          f"{per_line:.4f} vs {s['savings']:.4f}")

    # The same invariant where the quantity is fractional: qty x price and the
    # booked total are not the same number, and the two must not drift apart.
    frac = new_auction(lines=[(2.5, 100.0), (1.5, 80.0)], min_dec=0.01, title="Fractional award")
    fa, fb = frac.lines
    place(frac, fa, VENDORS[0], 33.33)
    place(frac, fb, VENDORS[1], 27.77)
    frac.status = AuctionStatus.CLOSED
    db.commit()
    BUYER_C.post(f"/auctions/{frac.id}/award", follow_redirects=False, data={
        f"winner_{fa.id}": str(VENDORS[0].id), f"price_{fa.id}": "33.33",
        f"winner_{fb.id}": str(VENDORS[1].id), f"price_{fb.id}": "27.77",
    })
    db.expire_all()
    sf = E.auction_summary(db, frac)
    per_line = sum(E.line_result(db, l)["savings"] for l in frac.lines)
    check("...and they still add up on fractional quantities",
          near(per_line, sf["savings"], 0.0001), f"{per_line:.4f} vs {sf['savings']:.4f}")
    check("each line's final value is the total that was booked",
          all(near(E.line_result(db, l)["final_value"],
                   db.query(Award).filter_by(line_id=l.id).one().total, 0.0001)
              for l in frac.lines))

    # ------------------------------------------------------------------ F
    print("\nF. The clock")
    ext = new_auction(lines=[(1, 100.0)], min_dec=1, auto_extend=True, trigger=120,
                      extend_by=180, max_ext=2, title="Extensions")
    eline2 = ext.lines[0]
    ext.end_at = datetime.utcnow() + timedelta(seconds=30)
    db.commit()
    ends = []
    for vendor, price in [(VENDORS[0], 99), (VENDORS[1], 98), (VENDORS[2], 97)]:
        # Each bid has to land inside the closing window to earn an extension.
        # Once the clock has been pushed out by three minutes the next bid is
        # nowhere near the end, so wind it back first - otherwise this proves
        # nothing about the cap.
        ext.end_at = datetime.utcnow() + timedelta(seconds=30)
        db.commit()
        place(ext, eline2, vendor, price)
        db.refresh(ext)
        ends.append(ext.end_at)
    check("the clock moved twice and then stopped", ext.extensions_used == 2,
          str(ext.extensions_used))
    check("...each move pushed it forward", all(e > datetime.utcnow() for e in ends))
    check("the third late bid did not extend it again",
          (ends[2] - ends[1]).total_seconds() < 180)
    check("the bid itself was still accepted", E.best_bid(db, eline2.id).unit_price == 97.0)

    sched = new_auction(lines=[(1, 10.0)], status=AuctionStatus.SCHEDULED, title="Scheduled")
    sched.start_at = datetime.utcnow() + timedelta(seconds=1)
    db.commit()
    stats = scheduler.tick()
    db.refresh(sched)
    check("a future auction is left alone", sched.status == AuctionStatus.SCHEDULED)
    sched.start_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    scheduler.tick()
    db.refresh(sched)
    check("the scheduler opens it when its time comes", sched.status == AuctionStatus.LIVE)
    sched.end_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    scheduler.tick()
    db.refresh(sched)
    check("and closes it when the time is up", sched.status == AuctionStatus.CLOSED)
    closed_at = sched.closed_at
    scheduler.tick()
    db.refresh(sched)
    check("a later tick does not touch it again", sched.closed_at == closed_at)

    cancelled = new_auction(lines=[(1, 10.0)], title="Cancelled")
    BUYER_C.post(f"/auctions/{cancelled.id}/cancel", data={"reason": "not needed"},
                 follow_redirects=False)
    db.expire_all()
    cancelled.end_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    scheduler.tick()
    db.refresh(cancelled)
    check("the scheduler ignores a cancelled auction",
          cancelled.status == AuctionStatus.CANCELLED)
    r = BUYER_C.post(f"/auctions/{mix.id}/cancel", data={"reason": "too late"},
                     follow_redirects=False)
    db.refresh(mix)
    check("an awarded auction cannot be cancelled", mix.status == AuctionStatus.AWARDED,
          flash_of(r)[:44])

    # ------------------------------------------------------------------ G
    print("\nG. What the reports say")
    data = reporting.auction_summary_report(db, mix)
    check("the report agrees with the engine on savings",
          near(data["summary"]["savings"], E.auction_summary(db, mix)["savings"]))
    csv = BUYER_C.get(f"/reports/auction/{mix.id}/export/csv").content.decode("utf-8-sig")
    check("the CSV carries the awarded price", "95.00" in csv)
    check("the CSV names the line with no ceiling", "no ceiling" in csv)
    check("the CSV lists every bid", csv.count("Supplier") >= 4)
    pdf = BUYER_C.get(f"/reports/auction/{mix.id}/export/pdf")
    check("the PDF builds", pdf.content[:4] == b"%PDF", f"{len(pdf.content)} bytes")

    today = datetime.now().strftime("%Y-%m-%d")
    span = f"?date_from=2000-01-01&date_to={today}"
    page = BUYER_C.get("/reports" + span).text
    total = reporting.total_savings(
        db, datetime(2000, 1, 1), datetime.utcnow() + timedelta(days=1))
    engine_total = sum(E.auction_summary(db, x["auction"])["savings"] for x in total["rows"])
    check("the savings report totals what the engine totals",
          near(total["totals"]["savings"], engine_total),
          f"{total['totals']['savings']:.2f}")
    check("...and the page shows that figure",
          f"{total['totals']['savings']:,.2f}" in page)

    # ------------------------------------------------------------------ H
    print("\nH. The dashboard adds up")
    home = BUYER_C.get("/").text
    awarded = [x for x in db.query(Auction).filter_by(status=AuctionStatus.AWARDED).all()]
    dash_savings = sum(E.auction_summary(db, x)["savings"] for x in awarded)
    check("the dashboard's total savings match the awarded auctions",
          f"{dash_savings:,.2f}" in home, f"{dash_savings:.2f}")
    check("no NaN or Infinity anywhere on the dashboard",
          "nan" not in home.lower().replace("nanometer", "") and "Infinity" not in home)

    # ------------------------------------------------------------------ I
    print("\nI. Two bidders at once")
    race = new_auction(lines=[(1, 100.0)], min_dec=5, title="Race")
    rline = race.lines[0]
    place(race, rline, VENDORS[0], 100)
    import threading
    results = []

    def racer(vendor, price):
        results.append(place(race, rline, vendor, price))

    threads = [threading.Thread(target=racer, args=(VENDORS[1], 95)),
               threading.Thread(target=racer, args=(VENDORS[2], 95))]
    for t_ in threads:
        t_.start()
    for t_ in threads:
        t_.join()
    db.expire_all()
    live = [b for b in E.line_bids(db, rline.id)]
    prices = sorted(b.unit_price for b in live)
    check("two simultaneous equal bids cannot both stand",
          prices.count(95.0) <= 1, ", ".join(str(p) for p in prices))

    db.close()
    print("\n" + "-" * 62)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All scenario checks passed.")
    return 0


def mailer_all():
    from app.models import EmailMessage
    session = SessionLocal()
    try:
        return session.query(EmailMessage).all()
    finally:
        session.close()


def test_scenarios():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
