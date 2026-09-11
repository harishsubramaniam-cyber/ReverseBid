"""Vendor, item and unit masters, plus the Odoo-style inline create endpoints
used from inside the auction form so the buyer never loses their place."""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..audit import record
from ..db import get_db
from ..emails_util import EmailError, describe, normalise, parse, validate
from ..engine import ADDER_FIELDS
from ..errors import ActionError as MasterProblem
from ..models import Item, Unit, User, Vendor
from ..security import buyer_only, current_user
from ..web import client_ip, redirect, render

router = APIRouter(prefix="/masters")


def _masters_screen(request: Request, db: Session, user: User, tab: str = "vendors",
                    q: str = "", *, error: str = "", prefill: dict | None = None,
                    status_code: int = 200):
    """The masters page. ``prefill`` puts back what was typed after a refusal -
    a mistyped email used to wipe all eight boxes and send the buyer back to a
    blank form."""
    vendors = db.query(Vendor).order_by(Vendor.name).all()
    items = db.query(Item).order_by(Item.name).all()
    units = db.query(Unit).order_by(Unit.code).all()
    if q:
        needle = q.lower()
        vendors = [v for v in vendors if needle in v.name.lower() or needle in v.email.lower()]
        items = [i for i in items if needle in i.name.lower()]
        units = [u for u in units if needle in u.code.lower()]
    return render(request, "masters.html",
                  {"vendors": vendors, "items": items, "units": units, "tab": tab, "q": q,
                   "error": error, "pf": prefill or {}},
                  user=user, db=db, help_key="masters", status_code=status_code)


@router.get("")
def masters_home(request: Request, tab: str = "vendors", q: str = "",
                 user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    return _masters_screen(request, db, user, tab, q)


# ------------------------------------------------------------------ delivered cost
def _default_adders(form) -> dict:
    """The vendor's usual freight, duty and packaging.

    These only pre-fill the auction form. Each auction keeps its own copy, so
    changing a vendor's usual freight never rewrites what a past auction was
    ranked on.
    """
    values: dict = {}
    for field, label in ADDER_FIELDS:
        raw = (form.get(f"default_{field}") or "").strip()
        if raw:
            try:
                amount = float(raw)
            except ValueError:
                raise MasterProblem(f"{label} has to be a number — “{raw}” is not.")
            if not math.isfinite(amount) or amount < 0:
                raise MasterProblem(f"{label} cannot be negative, and has to be a real number.")
            basis = (form.get(f"default_{field}_basis") or "unit").strip().lower()
            if basis not in ("unit", "percent"):
                basis = "unit"
            if basis == "percent" and amount >= 100:
                raise MasterProblem(f"{label} of {amount:g}% is almost certainly a typo.")
            values[f"default_{field}"] = amount
            values[f"default_{field}_basis"] = basis
    label_text = (form.get("default_other_label") or "").strip()
    if label_text:
        values["default_other_label"] = label_text[:60]
    return values


# ------------------------------------------------------------------ create
def create_vendor(db: Session, user: User, name: str, email: str, **extra) -> Vendor:
    """Create (or reuse) a vendor. ``email`` may itself be a list of addresses -
    the first becomes the primary and the rest join the extra contacts."""
    name = name.strip()
    try:
        typed = validate(email, field="email address")
        extras = validate(extra.pop("extra_emails", ""), field="email address")
    except EmailError as exc:
        raise MasterProblem(str(exc))
    if not name:
        raise MasterProblem("A vendor needs a company name.")
    if not typed:
        raise MasterProblem("A vendor needs at least one email address — that is where the "
                            "invitations go.")
    email, rest = typed[0], typed[1:]
    extras = [a for a in rest + extras if a != email]
    existing = db.query(Vendor).filter(Vendor.email == email).first()
    if existing:
        # That address is already on file. Update the record rather than
        # quietly discarding what was just typed, and bring it back from the
        # archive so it shows up in the auction form's bidder list.
        existing.was_named = existing.name if name and name != existing.name else ""
        existing.name = name or existing.name
        for field, value in extra.items():
            if value:
                setattr(existing, field, value)
        merged = [a for a in parse(existing.extra_emails) + extras
                  if a != existing.email.lower()]
        existing.extra_emails = "\n".join(dict.fromkeys(merged))
        existing.is_active = True
        record(db, action="vendor.update", entity_type="vendor", entity_id=existing.id,
               actor=user, detail={"matched_on_email": email})
        db.commit()
        existing.reused = True
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
        raise MasterProblem("An item needs a name.")
    unit_id = extra.pop("default_unit_id", None)
    if unit_id and not str(unit_id).strip().isdigit():
        raise MasterProblem("That unit was not one of the choices. Reload the page and pick "
                            "it again.")
    if unit_id and not db.get(Unit, int(unit_id)):
        raise MasterProblem("That unit no longer exists. Pick another one.")
    # Two items with the same name give the auction form two identical choices
    # and nobody can tell which is which afterwards.
    twin = db.query(Item).filter(func.lower(Item.name) == name.lower()).first()
    if twin:
        raise MasterProblem(
            f"“{twin.name}” is already on your item list"
            + (" (archived — restore it instead of adding it again)."
               if not twin.is_active else ". Pick it from the list rather than adding it twice.")
        )
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
        raise MasterProblem("A unit needs a short code, like KG.")
    existing = db.query(Unit).filter(Unit.code == code).first()
    if existing:
        existing.reused = True
        return existing
    unit = Unit(code=code, name=name.strip())
    db.add(unit)
    db.flush()
    record(db, action="unit.create", entity_type="unit", entity_id=unit.id, actor=user,
           detail={"code": code})
    db.commit()
    return unit


@router.post("/vendors")
async def post_vendor(request: Request, name: str = Form(""), email: str = Form(""),
                      extra_emails: str = Form(""), code: str = Form(""),
                      contact_person: str = Form(""), phone: str = Form(""),
                      gstin: str = Form(""), address: str = Form(""),
                      user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        vendor = create_vendor(db, user, name, email, extra_emails=extra_emails, code=code,
                               contact_person=contact_person, phone=phone, gstin=gstin,
                               address=address, **_default_adders(form))
        count = 1 + len(parse(vendor.extra_emails))
        addresses = f"Emails go to {count} address(es)."
        if getattr(vendor, "reused", False):
            # That email address was already on file, so this updated the
            # supplier we had rather than making a second copy of them. Say so
            # plainly, and say if the name changed - it changes on every
            # auction they are already on.
            renamed = getattr(vendor, "was_named", "")
            message = (f"“{vendor.email}” was already on file, so we updated that supplier "
                       f"instead of adding a second one. ")
            message += (f"It was called “{renamed}” and is now “{vendor.name}”, everywhere it "
                        f"appears. " if renamed else f"They are still “{vendor.name}”. ")
            return redirect("/masters?tab=vendors", message + addresses)
        return redirect("/masters?tab=vendors",
                        f"Vendor “{vendor.name}” saved. {addresses}")
    except MasterProblem as exc:
        db.rollback()
        return _masters_screen(request, db, user, "vendors", error=str(exc),
                               prefill={"vendor": {
                                   "name": name, "email": email, "extra_emails": extra_emails,
                                   "code": code, "contact_person": contact_person,
                                   "phone": phone, "gstin": gstin, "address": address}})



@router.post("/vendors/{vendor_id}/emails")
def update_vendor_emails(vendor_id: int, request: Request, email: str = Form(""),
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
def post_item(request: Request, name: str = Form(""), code: str = Form(""),
              category: str = Form(""), description: str = Form(""),
              default_unit_id: str = Form(""), user: User = Depends(buyer_only),
              db: Session = Depends(get_db)):
    try:
        if default_unit_id and not default_unit_id.strip().isdigit():
            raise MasterProblem("That unit was not one of the choices. Reload the page and "
                                "pick it again.")
        item = create_item(db, user, name, code=code, category=category, description=description,
                           default_unit_id=default_unit_id or None)
        return redirect("/masters?tab=items", f"Item “{item.name}” saved.")
    except MasterProblem as exc:
        db.rollback()
        return _masters_screen(request, db, user, "items", error=str(exc),
                               prefill={"item": {
                                   "name": name, "code": code, "category": category,
                                   "description": description,
                                   "default_unit_id": default_unit_id}})



@router.post("/units")
def post_unit(request: Request, code: str = Form(""), name: str = Form(""),
              user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    try:
        unit = create_unit(db, user, code, name)
        if getattr(unit, "reused", False):
            # Codes are held upper-cased, so "kg" and "KG" are the same unit.
            # Saying "saved" would leave the buyer looking for a second row.
            return redirect("/masters?tab=units",
                            f"“{unit.code}” already exists"
                            + (f" — {unit.name}." if unit.name else ".")
                            + " Nothing was added.")
        return redirect("/masters?tab=units", f"Unit “{unit.code}” saved.")
    except MasterProblem as exc:
        db.rollback()
        return _masters_screen(request, db, user, "units", error=str(exc),
                               prefill={"unit": {"code": code, "name": name}})



# ------------------------------------------------------------------ inline (JSON)
def _quick(fn):
    """Inline create from the auction form: reply with a message, never a crash."""
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except MasterProblem as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return wrapper


@router.post("/quick/vendor")
@_quick
def quick_vendor(name: str = Form(""), email: str = Form(""), phone: str = Form(""),
                 extra_emails: str = Form(""), user: User = Depends(buyer_only),
                 db: Session = Depends(get_db)):
    vendor = create_vendor(db, user, name, email, phone=phone, extra_emails=extra_emails)
    addresses = [vendor.email] + parse(vendor.extra_emails)
    return {"id": vendor.id, "label": f"{vendor.name} — {vendor.email}",
            "emails": ", ".join(addresses), "count": len(addresses)}


@router.post("/quick/item")
@_quick
def quick_item(name: str = Form(""), default_unit_id: str = Form(""),
               user: User = Depends(buyer_only), db: Session = Depends(get_db)):
    item = create_item(db, user, name, default_unit_id=default_unit_id or None)
    return {"id": item.id, "label": item.name,
            "unit_id": item.default_unit_id or ""}


@router.post("/quick/unit")
@_quick
def quick_unit(code: str = Form(""), name: str = Form(""),
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
