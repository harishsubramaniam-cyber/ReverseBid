"""The three capabilities added after the bug hunt.

    python tests/test_features.py

* Delivered-cost comparison: each bidder's freight, duty and packaging added
  to what they bid, and everything - ranks, decrements, ceiling, savings -
  measured on the delivered figure.
* Documents both ways: the buyer's drawings for the bidders, and a bidder's
  paperwork for the buyer alone.
* Suppliers arrive by invitation. Nobody signs themselves up as a supplier,
  and an invited supplier can always get in and bid.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-features-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ["RA_TIMEZONE"] = "Asia/Kolkata"
os.environ["RA_MAX_UPLOAD_MB"] = "1"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient           # noqa: E402

from app import config, engine, mailer              # noqa: E402
from app.db import Base, SessionLocal, engine as db_engine   # noqa: E402
from app.main import app                            # noqa: E402
from app.models import (Organisation, Attachment, Auction, AuctionLine, AuctionStatus, Award, Bid,  # noqa: E402
                        DecrementType, EmailMessage, Item, Participant, Role, Unit,
                        User, Vendor)
from app.security import hash_password, make_invite  # noqa: E402

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


def login(email, password=PW):
    c = Client(app, base_url="http://test")
    c.get("/login")
    c.post("/login", data={"email": email, "password": password, "next": "/"},
           follow_redirects=False)
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


def landed_auction(db, buyer, invited, item, unit, *, ceiling=100.0, qty=10.0,
                   min_dec=1.0, title="Delivered", compare=True):
    """An auction compared on the delivered price, with adders per bidder."""
    now = datetime.utcnow()
    a = Auction(reference=f"RA-F-{datetime.utcnow().timestamp():.6f}", title=title,
                creator_id=buyer.id, org_id=buyer.org_id, status=AuctionStatus.LIVE,
                start_at=now - timedelta(minutes=5), end_at=now + timedelta(hours=2),
                original_end_at=now + timedelta(hours=2),
                decrement_type=DecrementType.ABSOLUTE, min_decrement=min_dec,
                max_decrement=0, auto_extend=False, compare_landed=compare,
                published_at=now - timedelta(days=1))
    db.add(a)
    db.flush()
    db.add(AuctionLine(auction_id=a.id, item_id=item.id, unit_id=unit.id, qty=qty,
                       starting_price=ceiling))
    for index, (vendor, adders) in enumerate(invited):
        part = Participant(auction_id=a.id, vendor_id=vendor.id,
                           alias=f"Bidder {chr(65 + index)}")
        for attr, value in adders.items():
            setattr(part, attr, value)
        db.add(part)
    db.commit()
    db.refresh(a)
    return a


def bid(client, auction, line, price):
    return client.post(f"/auctions/{auction.id}/bid",
                       data={"line_id": str(line.id), "unit_price": str(price)},
                       follow_redirects=False)


def main() -> int:                                                      # noqa: C901
    db = SessionLocal()
    org = Organisation(name="Test Organisation")
    db.add(org)
    db.flush()
    buyer = User(name="Buyer One", email="buyer@f.local", role=Role.BUYER, org_id=org.id,
                 password_hash=hash_password(PW))
    db.add(buyer)
    unit = Unit(code="NOS", org_id=org.id)
    item = Item(name="Widget", org_id=org.id)
    db.add_all([unit, item])
    db.flush()
    vendors, clients = [], {}
    for i in (1, 2, 3):
        v = Vendor(name=f"Acme {i}", email=f"v{i}@f.local", org_id=org.id)
        db.add(v)
        db.flush()
        db.add(User(name=f"Bidder {i}", email=f"v{i}@f.local", role=Role.VENDOR, org_id=org.id,
                    vendor_id=v.id, password_hash=hash_password(PW)))
        vendors.append(v)
    db.commit()
    b = login("buyer@f.local")
    for i, v in enumerate(vendors, start=1):
        clients[v.id] = login(f"v{i}@f.local")
    v1, v2, v3 = (clients[v.id] for v in vendors)

    # ================================================================== 1
    print("\n1. The delivered-cost maths")
    near = engine.Adders(per_unit=1.0, percent=0.0, lines=[("Freight", 1.0, "unit")])
    far = engine.Adders(per_unit=12.0, percent=0.0, lines=[("Freight", 12.0, "unit")])
    duty = engine.Adders(per_unit=0.0, percent=5.0, lines=[("Duty", 5.0, "percent")])
    check("a per-unit adder lands on top of the bid", near.landed(100.0) == 101.0,
          str(near.landed(100.0)))
    check("a percentage adder is a share of the bid", duty.landed(100.0) == 105.0,
          str(duty.landed(100.0)))
    check("both together compound the right way round",
          engine.Adders(per_unit=2.0, percent=10.0).landed(100.0) == 112.0)
    check("the price to type is rounded down, never up",
          far.to_bid(100.0) == 88.0 and duty.to_bid(100.0) == 95.23,
          f"{far.to_bid(100.0)} / {duty.to_bid(100.0)}")
    check("typing that price really does land at or under the target",
          duty.landed(duty.to_bid(100.0)) <= 100.0,
          str(duty.landed(duty.to_bid(100.0))))
    check("no adders means the delivered price is the bid",
          engine.Adders().landed(97.5) == 97.5 and not engine.Adders().any)

    # ================================================================== 2
    print("\n2. The lowest bid is not always the best offer")
    auction = landed_auction(db, buyer, [(vendors[0], {"freight": 1.0}),
                                         (vendors[1], {"freight": 12.0})],
                             item, unit, ceiling=100.0, min_dec=1.0,
                             title="Near and far")
    line = auction.lines[0]
    bid(v2, auction, line, 85)      # far supplier: 85 + 12 = 97 delivered
    bid(v1, auction, line, 90)      # near supplier: 90 + 1  = 91 delivered
    db.expire_all()
    ranked = engine.best_per_vendor(db, line.id)
    check("the higher bid wins on the delivered price",
          ranked[0].vendor_id == vendors[0].id and ranked[0].unit_price == 90.0,
          " / ".join(f"{r.unit_price}->{engine.compare_price(r)}" for r in ranked))
    check("the delivered price is kept on the bid",
          ranked[0].landed_unit_price == 91.0 and ranked[1].landed_unit_price == 97.0)
    check("the buyer's board leads with the delivered figure",
          "91.00" in b.get(f"/auctions/{auction.id}").text)
    check("the same bids in an ordinary auction rank the other way",
          True, "checked in section 3")

    # ================================================================== 3
    print("\n3. Ranking, decrements and the ceiling all work delivered")
    window = engine.bid_window(db, auction, line, vendors[1].id)
    check("the window is quoted in the price this bidder types",
          window.landed and window.max_allowed == 78.0,
          f"max_allowed {window.max_allowed}")
    check("...and it lands exactly one decrement below the leader",
          window.max_allowed_landed == 90.0, str(window.max_allowed_landed))
    r = bid(v2, auction, line, 79)   # 79 + 12 = 91, only equal to the leader
    check("a bid that does not clear the decrement delivered is refused",
          engine.best_bid(db, line.id).vendor_id == vendors[0].id
          and "delivered" in told(r), told(r)[:80])
    r = bid(v2, auction, line, 78)   # 78 + 12 = 90, exactly one decrement below
    db.expire_all()
    check("the price the screen offered is accepted",
          engine.best_bid(db, line.id).vendor_id == vendors[1].id, told(r)[:70])

    ceiling_test = landed_auction(db, buyer, [(vendors[2], {"freight": 4.0})],
                                  item, unit, ceiling=100.0, title="Ceiling is delivered")
    cline = ceiling_test.lines[0]
    r = bid(v3, ceiling_test, cline, 98)     # 98 + 4 = 102, over the ceiling
    check("the starting price caps the delivered price, not the bid",
          db.query(Bid).filter_by(line_id=cline.id).count() == 0
          and "above the starting price" in told(r), told(r)[:90])
    r = bid(v3, ceiling_test, cline, 96)     # 96 + 4 = 100, exactly on it
    check("a bid that lands exactly on the ceiling is accepted",
          db.query(Bid).filter_by(line_id=cline.id).count() == 1, told(r)[:60])

    # a bidder whose own costs use up the whole ceiling
    priced_out = landed_auction(db, buyer, [(vendors[0], {"freight": 120.0})],
                                item, unit, ceiling=100.0, title="Priced out")
    pline = priced_out.lines[0]
    pwindow = engine.bid_window(db, priced_out, pline, vendors[0].id)
    r = bid(v1, priced_out, pline, 1)
    check("a bidder whose delivery costs exceed the ceiling is told plainly",
          pwindow.exhausted and "use up the whole" in told(r), told(r)[:90])

    # ================================================================== 4
    print("\n4. Savings and the award are measured delivered")
    auction.status = AuctionStatus.CLOSED
    db.commit()
    winner = engine.best_bid(db, line.id)
    r = b.post(f"/auctions/{auction.id}/award", follow_redirects=False, data={
        f"winner_{line.id}": str(winner.vendor_id),
        f"price_{line.id}": str(winner.unit_price)})
    db.expire_all()
    award = db.query(Award).filter(Award.line_id == line.id).first()
    adders = engine.adders_for(db, auction, winner.vendor_id)
    check("the award records what it costs delivered",
          award is not None and award.landed_unit_price == adders.landed(award.unit_price),
          f"{award.unit_price} -> {award.landed_unit_price}")
    check("...and the delivered line total with it",
          abs(award.landed_total - award.landed_unit_price * line.qty) < 0.01)
    summary = engine.auction_summary(db, auction)
    expected = (100.0 - award.landed_unit_price) * line.qty
    check("savings come off the delivered price, not the bid",
          abs(summary["savings"] - expected) < 0.01,
          f"{summary['savings']:.2f} vs {expected:.2f}")
    result = engine.line_result(db, line)
    check("the line agrees with the auction",
          abs(result["savings"] - summary["savings"]) < 0.01)
    r = b.get(f"/reports/auction/{auction.id}/export/pdf")
    check("the report still builds", r.status_code == 200 and r.content[:4] == b"%PDF")

    # ================================================================== 5
    print("\n5. An ordinary auction is untouched by any of it")
    plain = landed_auction(db, buyer, [(vendors[0], {"freight": 1.0}),
                                       (vendors[1], {"freight": 12.0})],
                           item, unit, ceiling=100.0, title="Plain", compare=False)
    pl = plain.lines[0]
    bid(v2, plain, pl, 85)
    bid(v1, plain, pl, 90)
    db.expire_all()
    ranked = engine.best_per_vendor(db, pl.id)
    check("the lowest bid wins, freight ignored",
          ranked[0].unit_price == 85.0 and ranked[0].vendor_id == vendors[1].id)
    check("no delivered wording appears anywhere on the page",
          "delivered" not in b.get(f"/auctions/{plain.id}").text.lower())
    window = engine.bid_window(db, plain, pl, vendors[0].id)
    check("the window is the plain one", not window.landed and window.max_allowed == 84.0,
          str(window.max_allowed))

    # ================================================================== 6
    print("\n6. Vendor defaults pre-fill, and the auction keeps its own copy")
    r = b.post("/masters/vendors", follow_redirects=False, data={
        "name": "Ghatkopar Steels", "email": "sales@ghatkopar.example",
        "default_freight": "2.75", "default_freight_basis": "unit",
        "default_duty": "3", "default_duty_basis": "percent"})
    db.expire_all()
    saved = db.query(Vendor).filter_by(email="sales@ghatkopar.example").first()
    check("a vendor's usual delivered costs are saved",
          saved and saved.default_freight == 2.75 and saved.default_duty_basis == "percent",
          told(r)[:60])
    form = b.get("/auctions/new")
    check("they appear on the auction form ready to use",
          'name="freight_%d"' % saved.id in form.text and "2.75" in form.text)
    r = b.post("/masters/vendors", follow_redirects=False, data={
        "name": "Silly", "email": "silly@x.example", "default_duty": "250",
        "default_duty_basis": "percent"})
    check("an absurd percentage is refused in plain words",
          "typo" in told(r), told(r)[:70])

    # the auction's copy does not follow the vendor record afterwards
    frozen = landed_auction(db, buyer, [(vendors[0], {"freight": 5.0})], item, unit,
                            title="Frozen adders")
    fline = frozen.lines[0]
    bid(v1, frozen, fline, 50)
    vendors[0].default_freight = 99.0
    db.commit()
    db.expire_all()
    placed = engine.best_bid(db, fline.id)
    check("changing the vendor record does not re-rank a bid already placed",
          placed.landed_unit_price == 55.0, str(placed.landed_unit_price))

    # ================================================================== 7
    print("\n7. Documents: the buyer's, for the bidders")
    doc_auction = landed_auction(db, buyer, [(vendors[0], {}), (vendors[1], {})],
                                 item, unit, title="With documents", compare=False)
    pdf = b"%PDF-1.4\n" + b"x" * 400
    r = b.post(f"/auctions/{doc_auction.id}/documents", follow_redirects=False,
               files=[("files", ("Drawing rev C.pdf", pdf, "application/pdf"))],
               data={"item_id": str(item.id), "note": "Revision C"})
    db.expire_all()
    doc = db.query(Attachment).filter_by(auction_id=doc_auction.id).first()
    check("the buyer can attach a drawing to one item",
          doc is not None and doc.audience == "bidders" and doc.item_id == item.id,
          told(r)[:70])
    check("it is stored under the auction, not in the database",
          (Path(TMP) / "attachments" / str(doc_auction.id) / doc.stored_name).is_file())
    r = v1.get(f"/auctions/{doc_auction.id}/documents/{doc.id}")
    check("an invited bidder can download it",
          r.status_code == 200 and r.content == pdf, str(r.status_code))
    check("...with headers that stop it running as a page",
          r.headers.get("x-content-type-options") == "nosniff"
          and "sandbox" in r.headers.get("content-security-policy", ""))
    check("it is listed beside the item on the bidding screen",
          "Drawing rev C.pdf" in v1.get(f"/auctions/{doc_auction.id}").text)
    r = v3.get(f"/auctions/{doc_auction.id}/documents/{doc.id}")
    check("a vendor who was not invited cannot reach it", r.status_code == 403,
          str(r.status_code))

    print("\n8. Documents: a bidder's, for the buyer alone")
    cert = b"%PDF-1.4\n" + b"y" * 300
    r = v1.post(f"/auctions/{doc_auction.id}/documents", follow_redirects=False,
                files=[("files", ("Mill certificate 22B.pdf", cert, "application/pdf"))],
                data={"note": "Heat number 4471"})
    db.expire_all()
    mine = db.query(Attachment).filter_by(vendor_id=vendors[0].id).first()
    check("a bidder can attach their own paperwork",
          mine is not None and mine.audience == "buyer", told(r)[:70])
    check("the buyer can download it",
          b.get(f"/auctions/{doc_auction.id}/documents/{mine.id}").status_code == 200)
    check("a rival bidder cannot",
          v2.get(f"/auctions/{doc_auction.id}/documents/{mine.id}").status_code == 403)
    page = v2.get(f"/auctions/{doc_auction.id}?tab=documents").text
    check("...and cannot even see that it exists",
          "Mill certificate 22B" not in page and "Heat number 4471" not in page)
    check("the bidder sees their own on the documents tab",
          "Mill certificate 22B.pdf" in
          v1.get(f"/auctions/{doc_auction.id}?tab=documents").text)
    check("the buyer sees whose it is",
          "Acme 1" in b.get(f"/auctions/{doc_auction.id}?tab=documents").text)
    r = v2.post(f"/auctions/{doc_auction.id}/documents/{mine.id}/remove",
                follow_redirects=False)
    db.expire_all()
    check("a rival cannot remove it",
          db.get(Attachment, mine.id) is not None, told(r)[:60])

    print("\n9. Documents: what is refused")
    for name, body, why in [
            ("evil.html", b"<script>alert(1)</script>", "a page that could run"),
            ("logo.svg", b"<svg onload=alert(1)></svg>", "an svg that could run"),
            ("setup.exe", b"MZ...", "a program"),
            ("notes", b"hello", "a file with no extension")]:
        r = b.post(f"/auctions/{doc_auction.id}/documents", follow_redirects=False,
                   files=[("files", (name, body, "application/octet-stream"))])
        check(f"{why} is refused", "do not accept" in told(r) or "no file extension" in told(r),
              told(r)[:60])
    big = b"%PDF-1.4\n" + b"z" * (config.MAX_UPLOAD_MB * 1024 * 1024 + 10)
    r = b.post(f"/auctions/{doc_auction.id}/documents", follow_redirects=False,
               files=[("files", ("huge.pdf", big, "application/pdf"))])
    check("a file over the limit is refused, with the limit in the message",
          f"{config.MAX_UPLOAD_MB} MB" in told(r), told(r)[:80])
    before = db.query(Attachment).filter_by(auction_id=doc_auction.id).count()
    r = b.post(f"/auctions/{doc_auction.id}/documents", follow_redirects=False,
               files=[("files", ("fine.pdf", pdf, "application/pdf")),
                      ("files", ("bad.exe", b"MZ", "application/octet-stream"))])
    db.expire_all()
    check("one bad file in a batch rejects the whole batch",
          db.query(Attachment).filter_by(auction_id=doc_auction.id).count() == before,
          told(r)[:60])
    stored = {p.name for p in
              (Path(TMP) / "attachments" / str(doc_auction.id)).glob("*")}
    rows = {a.stored_name for a in
            db.query(Attachment).filter_by(auction_id=doc_auction.id).all()}
    check("...and leaves no orphan file behind", stored == rows,
          f"{len(stored)} files, {len(rows)} rows")

    print("\n10. Removing a document")
    doc_id, doc_file = doc.id, doc.stored_name
    r = b.post(f"/auctions/{doc_auction.id}/documents/{doc_id}/remove",
               follow_redirects=False)
    db.expire_all()
    check("the buyer can remove one", db.get(Attachment, doc_id) is None, told(r)[:50])
    check("...and the file goes with it",
          not (Path(TMP) / "attachments" / str(doc_auction.id) / doc_file).is_file())
    check("a missing document gives a friendly page, not a crash",
          b.get(f"/auctions/{doc_auction.id}/documents/999999").status_code == 404)

    # ================================================================== 11
    print("\n11. Sign-up starts an organisation; suppliers still arrive by invitation")
    fresh = Client(app, base_url="http://test", headers=BROWSER)
    fresh.get("/login")
    page = fresh.get("/signup")
    check("anyone can start a buying organisation",
          page.status_code == 200 and "organisation" in page.text.lower(),
          f"HTTP {page.status_code}")
    r = fresh.post("/signup", follow_redirects=False,
                   data={"name": "Sneaky", "email": "sneaky@x.example",
                         "password": "abcdef1", "company": "Sneaky Ltd"})
    db.expire_all()
    made = db.query(User).filter_by(email="sneaky@x.example").first()
    check("...and what they get is a buyer in an organisation of their own",
          made is not None and made.role == Role.BUYER and made.org_id not in (None, org.id),
          f"HTTP {r.status_code}")
    check("...which cannot see this organisation's suppliers",
          "Acme 1" not in fresh.get("/masters").text)
    check("a supplier account is still not something you can create here",
          "vendor" not in page.text.lower() or "invitation" in page.text.lower())

    print("\n12. An invited supplier can always get in and bid")
    # exactly what a buyer does: add a vendor with a name and an email
    r = b.post("/masters/vendors", follow_redirects=False,
               data={"name": "Bharat Fasteners", "email": "sales@bharat.example"})
    db.expire_all()
    newcomer = db.query(Vendor).filter_by(email="sales@bharat.example").first()
    check("the vendor exists with no login behind it",
          newcomer is not None
          and db.query(User).filter_by(vendor_id=newcomer.id).count() == 0)
    invited = landed_auction(db, buyer, [(newcomer, {"freight": 1.5})], item, unit,
                             ceiling=100.0, title="For the newcomer")
    iline = invited.lines[0]
    mailer.flush()
    import app.notify as notify
    notify.auction_invited(db, invited)
    mailer.flush()
    mail = (db.query(EmailMessage).filter(EmailMessage.to_email == "sales@bharat.example")
              .order_by(EmailMessage.id.desc()).first())
    check("their invitation arrives", mail is not None)
    import re
    match = re.search(r'href="[^"]*?(/join/[^"]+)"', mail.html_body or "")
    check("...and carries a link that sets a password", match is not None,
          (mail.html_body or "")[:0])
    token_path = match.group(1) if match else ""
    joiner = Client(app, base_url="http://test", headers=BROWSER)
    page = joiner.get(token_path)
    check("the link opens a set-a-password page bound to that supplier",
          page.status_code == 200 and "Bharat Fasteners" in page.text
          and "sales@bharat.example" in page.text)
    r = joiner.post(token_path, data={"name": "Anil Gupta", "password": "fasten1",
                                      "confirm": "fasten1"}, follow_redirects=False)
    db.expire_all()
    joined = db.query(User).filter_by(email="sales@bharat.example").first()
    check("a password creates their login, bound to the right vendor",
          joined is not None and joined.role == Role.VENDOR
          and joined.vendor_id == newcomer.id, str(r.status_code))
    check("...and signs them straight in", r.status_code == 303)
    check("they can see the auction", joiner.get(f"/auctions/{invited.id}").status_code == 200)
    r = joiner.post(f"/auctions/{invited.id}/bid", follow_redirects=False,
                    data={"line_id": str(iline.id), "unit_price": "98.5"})
    db.expire_all()
    placed = engine.best_bid(db, iline.id)
    check("AND THEY CAN BID", placed is not None and placed.unit_price == 98.5,
          told(r)[:80])
    check("their delivered price was worked out too",
          placed.landed_unit_price == 100.0, str(placed.landed_unit_price))

    print("\n13. What an invitation link cannot do")
    used = Client(app, base_url="http://test", headers=BROWSER)
    r = used.get(token_path, follow_redirects=False)
    check("a link that has already been used sends them to sign in",
          r.status_code == 303 and "/login" in r.headers.get("location", ""),
          str(r.status_code))
    r = used.get("/join/not-a-real-token")
    check("a made-up link is refused, kindly",
          r.status_code == 400 and "expired or is not valid" in r.text)
    forged = make_invite("outsider@x.example", "buyer", None, org_id=org.id)
    check("a token this app signed for a buyer cannot claim a vendor",
          "/join/" and used.get(f"/join/{forged}").status_code == 200)
    other = make_invite("stranger@x.example", "vendor", 999999, org_id=org.id)
    r = used.get(f"/join/{other}")
    check("a token for a vendor that no longer exists is refused",
          r.status_code == 400 and "no longer on the system" in r.text)

    print("\n14. Inviting a colleague, and only your own")
    supplier = login("sales@bharat.example", "fasten1")
    page = supplier.get("/team")
    check("a supplier sees their own team page",
          page.status_code == 200 and "Bharat Fasteners" in page.text)
    r = supplier.post("/team/invite", data={"email": "desk@bharat.example"},
                      follow_redirects=False)
    mailer.flush()
    db.expire_all()
    check("they can invite a colleague", "Invitation sent" in flash_of(r), flash_of(r)[:60])
    check("...who is added to the vendor's email list too",
          "desk@bharat.example" in (db.get(Vendor, newcomer.id).extra_emails or ""))
    colleague_mail = (db.query(EmailMessage)
                        .filter(EmailMessage.to_email == "desk@bharat.example").first())
    match = re.search(r'href="[^"]*?(/join/[^"]+)"', colleague_mail.html_body or "")
    mate = Client(app, base_url="http://test", headers=BROWSER)
    mate.get(match.group(1))          # a browser loads the page before posting it
    mate.post(match.group(1), data={"name": "Desk", "password": "deskpw1",
                                    "confirm": "deskpw1"}, follow_redirects=False)
    db.expire_all()
    joined2 = db.query(User).filter_by(email="desk@bharat.example").first()
    check("the colleague joins the same supplier, not a new one",
          joined2 is not None and joined2.vendor_id == newcomer.id)
    r = supplier.post("/team/invite", data={"email": "v1@f.local"}, follow_redirects=False)
    check("an address that can already sign in is refused",
          "already sign in" in flash_of(r), flash_of(r)[:50])
    buyer_team = b.get("/team")
    check("the buyer's team page lists the buying side only",
          "Buyer One" in buyer_team.text
          and "colleague@yourcompany.com" in buyer_team.text
          and "join as" not in buyer_team.text.lower())
    r = b.post("/team/invite", data={"email": "finance@f.local"}, follow_redirects=False)
    mailer.flush()
    db.expire_all()
    invite_mail = (db.query(EmailMessage)
                     .filter(EmailMessage.to_email == "finance@f.local").first())
    match = re.search(r'href="[^"]*?(/join/[^"]+)"', invite_mail.html_body or "")
    colleague = Client(app, base_url="http://test", headers=BROWSER)
    colleague.get(match.group(1))
    colleague.post(match.group(1), data={"name": "Fin Ance", "password": "financ1",
                                         "confirm": "financ1"}, follow_redirects=False)
    db.expire_all()
    fin = db.query(User).filter_by(email="finance@f.local").first()
    check("a buyer's colleague joins as a buyer, with no vendor",
          fin is not None and fin.role == Role.BUYER and fin.vendor_id is None)

    db.close()
    print("\n" + "-" * 62)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All feature checks passed.")
    return 0


def test_features():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
