"""Vendor, item and unit masters, plus the Odoo-style inline create endpoints
used from inside the auction form so the buyer never loses their place."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from ..audit import record
from ..db import get_db
from ..emails_util import EmailError, describe, normalise, parse, validate
from ..models import Item, Unit, User, Vendor
from ..security import buyer_only, current_user
from ..web import client_ip, redirect, render

router = APIRouter(prefix="/masters")


@router.get("")
def masters_home(request: Request, tab: str = "vendors", q: str = "",
                 user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    vendors = db.query(Vendor).order_by(Vendor.name).all()
    items = db.query(Item).order_by(Item.name).all()
    units = db.query(Unit).order_by(Unit.code).all()
    if q:
        needle = q.lower()
        vendors = [v for v in vendors if needle in v.name.lower() or needle in v.email.lower()]
        items = [i for i in items if needle in i.name.lower()]
        units = [u for u in units if needle in u.code.lower()]
    return render(request, "masters.html",
                  {"vendors": vendors, "items": items, "units": units, "tab": tab, "q": q},
                  user=user, db=db, help_key="masters")


# ------------------------------------------------------------------ create
def create_vendor(db: Session, user: User, name: str, email: str, **extra) -> Vendor:
    """Create (or reuse) a vendor. ``email`` may itself be a list of addresses -
    the first becomes the primary and the rest join the extra contacts."""
    name = name.strip()
    try:
        typed = validate(email, field="email address")
        extras = validate(extra.pop("extra_emails", ""), field="email address")
    except EmailError as exc:
        raise HTTPException(400, str(exc))
    if not name or not typed:
        raise HTTPException(400, "A vendor needs a name and at least one email address.")
    email, rest = typed[0], typed[1:]
    extras = [a for a in rest + extras if a != email]
    existing = db.query(Vendor).filter(Vendor.email == email).first()
    if existing:
        merged = [a for a in parse(existing.extra_emails) + extras
                  if a != existing.email.lower()]
        existing.extra_emails = "\n".join(dict.fromkeys(merged))
        db.commit()
        return existing
    extra["extra_emails"] = "\n".join(dict.fromkeys(extras))
    vendor = Vendor(name=name, email=email, created_by_id=user.id,
                    **{k: (v or "") for k, v in extra.items()})
    db.add(vendor)
    db.flush()
    record(db, action="vendor.create", entity_type="vendor", entity_id=vendor.id, actor=user,
           detail={"name": name, "email": email})
    db.commit()
    return vendor


def create_item(db: Session, user: User, name: str, **extra) -> Item:
    name = name.strip()
    if not name:
        raise HTTPException(400, "An item needs a name.")
    unit_id = extra.pop("default_unit_id", None)
    item = Item(name=name, created_by_id=user.id,
                default_unit_id=int(unit_id) if unit_id else None,
                **{k: (v or "") for k, v in extra.items()})
    db.add(item)
    db.flush()
    record(db, action="item.create", entity_type="item", entity_id=item.id, actor=user,
           detail={"name": name})
    db.commit()
    return item


def create_unit(db: Session, user: User, code: str, name: str = "") -> Unit:
    code = code.strip().upper()
    if not code:
        raise HTTPException(400, "A unit needs a short code, like KG.")
    existing = db.query(Unit).filter(Unit.code == code).first()
    if existing:
        return existing
    unit = Unit(code=code, name=name.strip())
    db.add(unit)
    db.flush()
    record(db, action="unit.create", entity_type="unit", entity_id=unit.id, actor=user,
           detail={"code": code})
    db.commit()
    return unit


@router.post("/vendors")
def post_vendor(request: Request, name: str = Form(...), email: str = Form(...),
                extra_emails: str = Form(""), code: str = Form(""),
                contact_person: str = Form(""), phone: str = Form(""),
                gstin: str = Form(""), address: str = Form(""),
                user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    vendor = create_vendor(db, user, name, email, extra_emails=extra_emails, code=code,
                           contact_person=contact_person, phone=phone, gstin=gstin,
                           address=address)
    count = 1 + len(parse(vendor.extra_emails))
    return redirect("/masters?tab=vendors",
                    f"Vendor “{vendor.name}” saved. Emails go to {count} address(es).")


@router.post("/vendors/{vendor_id}/emails")
def update_vendor_emails(vendor_id: int, request: Request, email: str = Form(...),
                         extra_emails: str = Form(""), user: User = Depends(buyer_only),
                         db: Session = Depends(get_db)):
    """Edit exactly who at this vendor receives the platform's emails."""
    vendor = db.get(Vendor, vendor_id)
    if not vendor:
        raise HTTPException(404, "That vendor no longer exists.")
    try:
        primary = validate(email, field="email address")
        extras = validate(extra_emails, field="email address")
    except EmailError as exc:
        return redirect("/masters?tab=vendors", str(exc), kind="error")
    if not primary:
        return redirect("/masters?tab=vendors",
                        "A vendor needs at least one email address.", kind="error")
    before = [vendor.email] + parse(vendor.extra_emails)
    vendor.email = primary[0]
    vendor.extra_emails = "\n".join(
        dict.fromkeys([a for a in primary[1:] + extras if a != vendor.email]))
    after = [vendor.email] + parse(vendor.extra_emails)
    record(db, action="vendor.emails", entity_type="vendor", entity_id=vendor.id, actor=user,
           ip=client_ip(request), detail={"before": before, "after": after}, commit=True)
    return redirect("/masters?tab=vendors",
                    f"“{vendor.name}” will now be emailed at {describe(after)}.")


@router.post("/items")
def post_item(request: Request, name: str = Form(...), code: str = Form(""),
              category: str = Form(""), description: str = Form(""),
              default_unit_id: str = Form(""), user: User = Depends(buyer_only),
              db: Session = Depends(get_db)):
    item = create_item(db, user, name, code=code, category=category, description=description,
                       default_unit_id=default_unit_id or None)
    return redirect("/masters?tab=items", f"Item “{item.name}” saved.")


@router.post("/units")
def post_unit(request: Request, code: str = Form(...), name: str = Form(""),
              user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    unit = create_unit(db, user, code, name)
    return redirect("/masters?tab=units", f"Unit “{unit.code}” saved.")


# ------------------------------------------------------------------ inline (JSON)
@router.post("/quick/vendor")
def quick_vendor(name: str = Form(...), email: str = Form(...), phone: str = Form(""),
                 extra_emails: str = Form(""), user: User = Depends(buyer_only),
                 db: Session = Depends(get_db)):
    vendor = create_vendor(db, user, name, email, phone=phone, extra_emails=extra_emails)
    addresses = [vendor.email] + parse(vendor.extra_emails)
    return {"id": vendor.id, "label": f"{vendor.name} — {vendor.email}",
            "emails": ", ".join(addresses), "count": len(addresses)}


@router.post("/quick/item")
def quick_item(name: str = Form(...), default_unit_id: str = Form(""),
               user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    item = create_item(db, user, name, default_unit_id=default_unit_id or None)
    return {"id": item.id, "label": item.name,
            "unit_id": item.default_unit_id or ""}


@router.post("/quick/unit")
def quick_unit(code: str = Form(...), name: str = Form(""),
               user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    unit = create_unit(db, user, code, name)
    return {"id": unit.id, "label": unit.code}


# ------------------------------------------------------------------ edit / archive
@router.post("/vendors/{vendor_id}/toggle")
def toggle_vendor(vendor_id: int, request: Request, user: User = Depends(buyer_only),
                  db: Session = Depends(get_db)):
    vendor = db.get(Vendor, vendor_id)
    if not vendor:
        raise HTTPException(404, "That vendor no longer exists.")
    vendor.is_active = not vendor.is_active
    record(db, action="vendor.toggle", entity_type="vendor", entity_id=vendor.id, actor=user,
           ip=client_ip(request), detail={"active": vendor.is_active}, commit=True)
    state = "reactivated" if vendor.is_active else "archived"
    return redirect("/masters?tab=vendors", f"“{vendor.name}” {state}.")


@router.post("/items/{item_id}/toggle")
def toggle_item(item_id: int, request: Request, user: User = Depends(buyer_only),
                db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(404, "That item no longer exists.")
    item.is_active = not item.is_active
    record(db, action="item.toggle", entity_type="item", entity_id=item.id, actor=user,
           ip=client_ip(request), detail={"active": item.is_active}, commit=True)
    state = "reactivated" if item.is_active else "archived"
    return redirect("/masters?tab=items", f"“{item.name}” {state}.")
