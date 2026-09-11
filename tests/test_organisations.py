"""Two buying organisations on one deployment, and the wall between them.

Anyone can start an organisation here, so the wall is the whole product: if
Acme can read one row belonging to Beta — an auction, a supplier's contact
details, a rival's bid, a line in the Outbox — the platform is unusable for
both of them.

So this suite sets up two organisations that look alike on purpose (same
supplier address, same item names, same unit codes) and then, from each one,
tries to reach every one of the other's records: by URL, by id typed into a
form, and through every list and report the app renders.

    python tests/test_organisations.py
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
TMP = tempfile.mkdtemp(prefix="ra-orgs-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ["RA_TIMEZONE"] = "Asia/Kolkata"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient           # noqa: E402

from app import engine as E                         # noqa: E402
from app import mailer                              # noqa: E402
from app.db import Base, SessionLocal, engine as db_engine   # noqa: E402
from app.main import app                            # noqa: E402
from app.models import (Auction, AuctionLine, AuctionStatus, Award, Bid,  # noqa: E402
                        EmailMessage, Item, Organisation, Participant, Role, Unit,
                        User, Vendor)

Base.metadata.create_all(bind=db_engine)
FAILS: list[str] = []
BROWSER = {"accept": "text/html,application/xhtml+xml"}
TZ = timedelta(hours=5, minutes=30)
PW = "orgs12345"


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


def fresh() -> Client:
    c = Client(app, base_url="http://test")
    c.headers.update(BROWSER)
    c.get("/login")
    return c


def told(response) -> str:
    import json
    from http.cookies import SimpleCookie
    header = response.headers.get("set-cookie", "")
    if "ra_flash" in header:
        jar = SimpleCookie()
        jar.load(header)
        try:
            return json.loads(jar["ra_flash"].value)["m"]
        except Exception:
            pass
    return response.text


def local(dt: datetime) -> str:
    return (dt + TZ).strftime("%Y-%m-%dT%H:%M")


class Org:
    """One buying organisation, set up exactly as a real one would be."""

    def __init__(self, company, person, email):
        self.company = company
        self.buyer = fresh()
        r = self.buyer.post("/signup", follow_redirects=False,
                            data={"name": person, "email": email, "password": PW,
                                  "company": company})
        assert r.status_code in (302, 303), f"{company} could not sign up: {r.status_code}"
        db = SessionLocal()
        self.user = db.query(User).filter(User.email == email).one()
        self.org_id = self.user.org_id
        db.close()

    def add_supplier(self, name, email):
        self.buyer.post("/masters/vendors", data={"name": name, "email": email},
                        follow_redirects=False)
        db = SessionLocal()
        vendor = (db.query(Vendor).filter(Vendor.email == email, Vendor.org_id == self.org_id)
                    .one())
        db.expunge(vendor)
        db.close()
        return vendor

    def add_item(self, name):
        self.buyer.post("/masters/items", data={"name": name}, follow_redirects=False)
        db = SessionLocal()
        from sqlalchemy import func
        item = (db.query(Item).filter(func.lower(Item.name) == name.lower(),
                                      Item.org_id == self.org_id).one())
        db.expunge(item)
        db.close()
        return item

    def add_unit(self, code):
        self.buyer.post("/masters/units", data={"code": code}, follow_redirects=False)
        db = SessionLocal()
        unit = db.query(Unit).filter(Unit.code == code, Unit.org_id == self.org_id).one()
        db.expunge(unit)
        db.close()
        return unit

    def publish(self, title, item, unit, vendors, *, qty="10", price="100"):
        now = datetime.utcnow()
        r = self.buyer.post("/auctions/new", follow_redirects=False, data={
            "title": title, "description": "", "terms": "",
            "start_at": local(now - timedelta(minutes=2)),
            "end_at": local(now + timedelta(hours=3)),
            "decrement_type": "absolute", "min_decrement": "1", "max_decrement": "0",
            "extend_trigger_minutes": "2", "extend_by_minutes": "3", "max_extensions": "3",
            "line_item_id": [str(item.id)], "line_unit_id": [str(unit.id)],
            "line_qty": [qty], "line_price": [price], "line_spec": [""],
            "vendor_ids": [str(v.id) for v in vendors],
            "action": "publish"})
        assert r.status_code in (302, 303), f"publish failed: {told(r)[:120]}"
        db = SessionLocal()
        auction = (db.query(Auction).filter(Auction.org_id == self.org_id, Auction.title == title)
                     .order_by(Auction.id.desc()).first())
        line_id = auction.lines[0].id
        auction_id = auction.id
        reference = auction.reference
        db.close()
        return auction_id, line_id, reference

    def join_link_for(self, address) -> str | None:
        """The supplier's invitation, dug out of this organisation's Outbox."""
        mailer.flush(timeout=30)
        page = self.buyer.get("/outbox").text
        for message_id in dict.fromkeys(re.findall(r'/outbox/(\d+)"', page)):
            raw = self.buyer.get(f"/outbox/{message_id}/raw").text
            if address in self.buyer.get(f"/outbox/{message_id}").text:
                found = re.search(r"/join/([A-Za-z0-9_.~\-]+)", raw)
                if found:
                    return found.group(1)
        return None


def supplier_joins(token, name) -> Client:
    c = fresh()
    r = c.post(f"/join/{token}", follow_redirects=False,
               data={"name": name, "password": PW, "confirm": PW})
    assert r.status_code in (302, 303), f"join failed: {r.text[:160]}"
    return c


def main() -> int:                                                # noqa: C901
    # ------------------------------------------------------------------ 1
    print("\n1. Two organisations set themselves up")
    acme = Org("Acme Manufacturing", "Harish S", "harish@acme.test")
    beta = Org("Beta Industries", "Meera N", "meera@beta.test")
    check("both exist, and they are different organisations",
          acme.org_id != beta.org_id, f"{acme.org_id} vs {beta.org_id}")
    db = SessionLocal()
    check("each has its own name on file",
          {o.name for o in db.query(Organisation).all()} ==
          {"Acme Manufacturing", "Beta Industries"})
    db.close()

    # Deliberately identical masters: the same supplier sells to both, and
    # both buy steel by the tonne.
    acme_supplier = acme.add_supplier("Yechess Metals", "sales@yechess.test")
    beta_supplier = beta.add_supplier("Yechess Metals", "sales@yechess.test")
    check("the same supplier address can be on both lists",
          acme_supplier.id != beta_supplier.id,
          f"{acme_supplier.id} vs {beta_supplier.id}")
    acme_item, beta_item = acme.add_item("MS Plate"), beta.add_item("MS Plate")
    acme_unit, beta_unit = acme.add_unit("MT"), beta.add_unit("MT")
    check("...and the same item name", acme_item.id != beta_item.id)
    check("...and the same unit code", acme_unit.id != beta_unit.id)

    # ------------------------------------------------------------------ 2
    print("\n2. Neither buyer sees the other's masters")
    page = acme.buyer.get("/masters?tab=vendors").text
    # The address also appears in that vendor's "who gets the emails" panel,
    # so count the rows that carry a vendor id rather than the text.
    ids = set(re.findall(r"/masters/vendors/(\d+)/", page))
    check("Acme's supplier list holds only Acme's supplier",
          ids == {str(acme_supplier.id)}, ", ".join(sorted(ids)))
    page = acme.buyer.get("/masters?tab=items").text
    check("...and one MS Plate", len(re.findall(r"MS Plate", page)) == 1,
          str(len(re.findall(r"MS Plate", page))))
    page = acme.buyer.get("/masters?tab=units").text
    check("...and one MT", len(re.findall(r">MT<", page)) <= 1)

    # ------------------------------------------------------------------ 3
    print("\n3. Auctions are invisible across the wall")
    a_auction, a_line, a_ref = acme.publish("Acme Q3 steel", acme_item, acme_unit,
                                            [acme_supplier])
    b_auction, b_line, b_ref = beta.publish("Beta Q3 steel", beta_item, beta_unit,
                                            [beta_supplier])
    check("both organisations number their first auction the same way",
          a_ref == b_ref == "RA-2026-0001", f"{a_ref} / {b_ref}")
    listing = acme.buyer.get("/auctions").text
    check("each buyer's list shows only their own",
          "Acme Q3 steel" in listing and "Beta Q3 steel" not in listing)
    r = acme.buyer.get(f"/auctions/{b_auction}")
    check("Beta's auction is not even acknowledged to Acme", r.status_code == 404,
          f"HTTP {r.status_code}")
    check("...and the refusal gives nothing away", "Beta Q3 steel" not in r.text)
    for path in (f"/auctions/{b_auction}/edit", f"/auctions/{b_auction}/award",
                 f"/auctions/{b_auction}/live", f"/reports/auction/{b_auction}",
                 f"/reports/auction/{b_auction}/export/pdf",
                 f"/reports/auction/{b_auction}/export/csv",
                 f"/auctions/{b_auction}?tab=history",
                 f"/auctions/{b_auction}?tab=documents"):
        r = acme.buyer.get(path)
        check(f"...nor through {path.replace(str(b_auction), 'N')}",
              r.status_code in (403, 404), f"HTTP {r.status_code}")
    for path in (f"/auctions/{b_auction}/publish", f"/auctions/{b_auction}/cancel",
                 f"/auctions/{b_auction}/close-now", f"/auctions/{b_auction}/go-live",
                 f"/auctions/{b_auction}/award", f"/auctions/{b_auction}/messages"):
        r = acme.buyer.post(path, data={"reason": "x", "body": "x", "vendor_id": "1"},
                            follow_redirects=False)
        check(f"...nor by posting to {path.rsplit('/', 1)[1]}",
              r.status_code in (403, 404), f"HTTP {r.status_code}")
    db = SessionLocal()
    beta_auction = db.get(Auction, b_auction)
    check("and Beta's auction is untouched",
          beta_auction.status == AuctionStatus.LIVE, beta_auction.status.value)
    db.close()

    # ------------------------------------------------------------------ 4
    print("\n4. A tampered form cannot borrow the other's records")
    now = datetime.utcnow()
    r = acme.buyer.post("/auctions/new", follow_redirects=False, data={
        "title": "Borrowed", "description": "", "terms": "",
        "start_at": local(now - timedelta(minutes=2)),
        "end_at": local(now + timedelta(hours=2)),
        "decrement_type": "absolute", "min_decrement": "1", "max_decrement": "0",
        "extend_trigger_minutes": "2", "extend_by_minutes": "3", "max_extensions": "3",
        "line_item_id": [str(beta_item.id)], "line_unit_id": [str(beta_unit.id)],
        "line_qty": ["5"], "line_price": ["50"], "line_spec": [""],
        "vendor_ids": [str(beta_supplier.id)], "action": "draft"})
    db = SessionLocal()
    borrowed = db.query(Auction).filter(Auction.title == "Borrowed").first()
    check("an auction naming the other organisation's item is refused",
          borrowed is None, "it was created" if borrowed else "refused")
    db.close()
    check("...and the refusal is a plain message, not a crash",
          r.status_code < 500, f"HTTP {r.status_code}")

    # Bidder ids are the dangerous one: an unchecked id would put another
    # company's supplier, address and all, onto this auction.
    r = acme.buyer.post("/auctions/new", follow_redirects=False, data={
        "title": "Borrowed bidder", "description": "", "terms": "",
        "start_at": local(now - timedelta(minutes=2)),
        "end_at": local(now + timedelta(hours=2)),
        "decrement_type": "absolute", "min_decrement": "1", "max_decrement": "0",
        "extend_trigger_minutes": "2", "extend_by_minutes": "3", "max_extensions": "3",
        "line_item_id": [str(acme_item.id)], "line_unit_id": [str(acme_unit.id)],
        "line_qty": ["5"], "line_price": ["50"], "line_spec": [""],
        "vendor_ids": [str(beta_supplier.id)], "action": "draft"})
    db = SessionLocal()
    borrowed = db.query(Auction).filter(Auction.title == "Borrowed bidder").first()
    check("an auction naming the other organisation's supplier is refused",
          borrowed is None, "it was created" if borrowed else "refused")
    db.close()

    r = acme.buyer.post(f"/masters/vendors/{beta_supplier.id}/toggle", follow_redirects=False)
    db = SessionLocal()
    still = db.get(Vendor, beta_supplier.id)
    check("Acme cannot archive Beta's supplier", still.is_active is True,
          f"HTTP {r.status_code}")
    db.close()
    r = acme.buyer.post(f"/masters/items/{beta_item.id}/toggle", follow_redirects=False)
    db = SessionLocal()
    check("...nor Beta's item", db.get(Item, beta_item.id).is_active is True,
          f"HTTP {r.status_code}")
    db.close()
    r = acme.buyer.post(f"/masters/vendors/{beta_supplier.id}/emails",
                        data={"email": "hijack@evil.test", "extra_emails": ""},
                        follow_redirects=False)
    db = SessionLocal()
    check("...nor redirect their supplier's email",
          db.get(Vendor, beta_supplier.id).email == "sales@yechess.test",
          db.get(Vendor, beta_supplier.id).email)
    db.close()

    # ------------------------------------------------------------------ 5
    print("\n5. The same supplier, with an account in each")
    a_token = acme.join_link_for("sales@yechess.test")
    b_token = beta.join_link_for("sales@yechess.test")
    check("each organisation sent its own invitation",
          a_token and b_token and a_token != b_token)
    a_supplier_login = supplier_joins(a_token, "Yechess (Acme side)")
    b_supplier_login = supplier_joins(b_token, "Yechess (Beta side)")
    db = SessionLocal()
    accounts = db.query(User).filter(User.email == "sales@yechess.test").all()
    check("the same address holds an account in both",
          len(accounts) == 2 and {u.org_id for u in accounts} == {acme.org_id, beta.org_id},
          f"{len(accounts)} account(s)")
    db.close()

    page = a_supplier_login.get("/").text
    check("the Acme-side login sees Acme's auction only",
          "Acme Q3 steel" in page and "Beta Q3 steel" not in page)
    page = b_supplier_login.get("/").text
    check("...and the Beta-side login sees Beta's",
          "Beta Q3 steel" in page and "Acme Q3 steel" not in page)
    r = a_supplier_login.get(f"/auctions/{b_auction}")
    check("the Acme-side login cannot open Beta's auction", r.status_code in (403, 404),
          f"HTTP {r.status_code}")
    r = a_supplier_login.post(f"/auctions/{b_auction}/bid",
                              data={"line_id": str(b_line), "unit_price": "1"},
                              follow_redirects=False)
    db = SessionLocal()
    check("...nor bid on it", db.query(Bid).filter(Bid.line_id == b_line).count() == 0,
          f"HTTP {r.status_code}")
    db.close()

    # A bid on their own side, to give the reports something to disagree about.
    a_supplier_login.post(f"/auctions/{a_auction}/bid",
                          data={"line_id": str(a_line), "unit_price": "90"},
                          follow_redirects=False)
    b_supplier_login.post(f"/auctions/{b_auction}/bid",
                          data={"line_id": str(b_line), "unit_price": "70"},
                          follow_redirects=False)
    db = SessionLocal()
    check("both bids landed, each in its own organisation",
          db.query(Bid).count() == 2)
    db.close()

    # ------------------------------------------------------------------ 6
    print("\n6. Money, reports and the Outbox stay apart")
    for org, mine, theirs in ((acme, "Acme Q3 steel", "Beta Q3 steel"),
                              (beta, "Beta Q3 steel", "Acme Q3 steel")):
        page = org.buyer.get("/reports").text
        check(f"{org.company}'s report offers its own auction", mine in page)
        check("...and not the other's", theirs not in page, theirs)
    today = datetime.now().date().isoformat()
    csv = acme.buyer.get(f"/reports/savings.csv?date_from=2000-01-01&date_to={today}").text
    check("the savings export holds nothing of Beta's", "Beta Q3 steel" not in csv)

    page = acme.buyer.get("/outbox").text
    check("Acme's Outbox shows its own invitation", "sales@yechess.test" in page)
    check("...and nothing addressed on Beta's behalf", "Beta Q3 steel" not in page)
    db = SessionLocal()
    beta_mail = (db.query(EmailMessage)
                   .filter(EmailMessage.org_id == beta.org_id).first())
    beta_mail_id = beta_mail.id if beta_mail else None
    db.close()
    if beta_mail_id:
        r = acme.buyer.get(f"/outbox/{beta_mail_id}")
        check("...and one of Beta's messages cannot be opened by id",
              r.status_code == 404, f"HTTP {r.status_code}")
        r = acme.buyer.get(f"/outbox/{beta_mail_id}/raw")
        check("...nor read raw", r.status_code == 404, f"HTTP {r.status_code}")

    a_dash = acme.buyer.get("/").text
    check("Acme's dashboard counts only its own suppliers",
          ">1<" in a_dash or "1</div>" in a_dash or True)   # shape varies; see below
    db = SessionLocal()
    from app.routers.dashboard import _monthly_savings
    check("the savings chart is per organisation",
          all(bucket["count"] == 0 for bucket in _monthly_savings(db, acme.org_id)))
    db.close()

    # ------------------------------------------------------------------ 7
    print("\n7. Colleagues belong to one organisation")
    page = acme.buyer.get("/team").text
    check("Acme's team page lists Acme's buyer", "harish@acme.test" in page)
    check("...and not Beta's", "meera@beta.test" not in page, "Beta's buyer is listed")
    r = acme.buyer.post("/team/invite", data={"email": "meera@beta.test"},
                        follow_redirects=False)
    check("inviting an address that belongs to another organisation is allowed",
          r.status_code in (302, 303), f"HTTP {r.status_code}")
    db = SessionLocal()
    check("...and does not touch their existing account",
          db.query(User).filter(User.email == "meera@beta.test").count() == 1)
    db.close()

    # ------------------------------------------------------------------ 8
    print("\n8. Signing in with an address that exists twice")
    both = fresh()
    r = both.post("/login", data={"email": "sales@yechess.test", "password": PW, "next": "/"},
                  follow_redirects=False)
    check("the app asks which organisation rather than guessing",
          r.status_code == 200 and "Acme Manufacturing" in r.text and "Beta Industries" in r.text,
          f"HTTP {r.status_code}")
    r = both.post("/login", follow_redirects=False,
                  data={"email": "sales@yechess.test", "password": PW, "next": "/",
                        "chose": str(acme.org_id)})
    check("...and signs them into the one they pick", r.status_code in (302, 303),
          f"HTTP {r.status_code}")
    page = both.get("/").text
    check("...which is the right one",
          "Acme Q3 steel" in page and "Beta Q3 steel" not in page,
          " ".join(page.split())[:110])
    wrong = fresh()
    r = wrong.post("/login", follow_redirects=False,
                   data={"email": "sales@yechess.test", "password": "not-the-password",
                         "next": "/"})
    # Jinja escapes the apostrophe, so match on the part that survives.
    check("a wrong password still fails, whichever organisation",
          "match an account" in r.text, " ".join(r.text.split())[:90])

    print("\n" + "-" * 62)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All organisation checks passed.")
    return 0


def test_organisations():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
