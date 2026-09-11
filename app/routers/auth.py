from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import config, notify
from ..audit import record
from ..db import get_db
from ..emails_util import EmailError, parse as parse_emails, validate
from ..models import Item, Organisation, Role, User, Vendor
from ..security import (_MAX_PER_ACCOUNT, SESSION_COOKIE, clear_failed_logins, current_user,
                        current_user_optional, hash_password, login_blocked, make_invite,
                        note_failed_login, read_invite, safe_next, set_session_cookie,
                        verify_password)
from ..utils import humanize_seconds, first_name
from ..web import client_ip, redirect, render

router = APIRouter()



@router.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
    if current_user_optional(request, db):
        return redirect("/")
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""),
          next: str = Form("/"), chose: str = Form(""), db: Session = Depends(get_db)):
    # Only ever send people on to a page inside this app: ?next= arrives from
    # the address bar, so it must not be able to bounce them to another site.
    next = safe_next(next)
    typed = email.strip().lower()
    if not typed or not password:
        return render(request, "login.html",
                      {"next": next, "error": "Please type both your email and your password.",
                       "email": email})

    # Slow down password guessing, per address and per computer.
    # Two buckets: one per address-and-computer, and one for the address
    # alone. The second is the one that matters - the first can be spread
    # across as many apparent addresses as the caller likes.
    throttle_key = f"{typed}|{client_ip(request)}"
    account_key = f"account|{typed}"
    wait = max(login_blocked(throttle_key),
               login_blocked(account_key, _MAX_PER_ACCOUNT))
    if wait:
        return render(request, "login.html",
                      {"next": next, "email": email,
                       "error": "Too many sign-in attempts. Please wait "
                                f"{humanize_seconds(wait)} and try again, or reset the "
                                "password if you have forgotten it."}, status_code=200)

    # One address can hold an account with several buying organisations - a
    # supplier who sells to two of them has two logins, by design - so match
    # the password against each and see how many it opens.
    candidates = [u for u in db.query(User).filter(User.email == typed).all()
                  if u.is_active and verify_password(password, u.password_hash)]
    if not candidates:
        note_failed_login(throttle_key)
        note_failed_login(account_key)
        return render(request, "login.html",
                      {"next": next, "error": "That email and password don't match an account.",
                       "email": email}, status_code=200)
    if len(candidates) > 1:
        # The same address and password in more than one organisation: ask
        # which one they meant rather than guessing and showing them the
        # wrong company's auctions.
        clear_failed_logins(throttle_key)
        clear_failed_logins(account_key)
        chosen = (chose or "").strip()
        # `next` is this function's own parameter, so no builtin here.
        picked = None
        for candidate in candidates:
            if str(candidate.org_id) == chosen:
                picked = candidate
                break
        if picked is None:
            return render(request, "login.html",
                          {"next": next, "email": email, "password": password,
                           "choices": [(u.org_id, u.org.name if u.org else "Unnamed")
                                       for u in candidates]})
        user = picked
    else:
        user = candidates[0]
    clear_failed_logins(throttle_key)
    clear_failed_logins(account_key)
    record(db, action="user.login", entity_type="user", entity_id=user.id, actor=user,
           ip=client_ip(request), commit=True)
    response = redirect(next or "/", f"Welcome back, {first_name(user.name)}.")
    set_session_cookie(response, user.id)
    return response


@router.get("/signup")
def signup_form(request: Request, db: Session = Depends(get_db)):
    """Set up a new buying organisation.

    Anyone can start one, and it is completely separate from every other:
    its own colleagues, supplier list, items and auctions. Nobody outside it
    ever sees any of that.

    Suppliers do not come this way. They arrive on an invitation link from an
    auction they have been asked to bid on, which is what attaches them to
    the buyer who invited them.
    """
    return render(request, "signup.html", {})


@router.post("/signup")
def signup(request: Request, name: str = Form(""), email: str = Form(""),
           password: str = Form(""), company: str = Form(""),
           db: Session = Depends(get_db)):
    email = email.strip().lower()
    typed = {"name": name, "email": email, "company": company}
    if not name.strip() or not email:
        return render(request, "signup.html",
                      {"error": "Please fill in your name and email address.", **typed})
    if not company.strip():
        return render(request, "signup.html",
                      {"error": "Please give your organisation a name — it is what your "
                                "colleagues and suppliers will see.", **typed})
    if len(password) < 6:
        return render(request, "signup.html",
                      {"error": "Please choose a password of at least 6 characters.", **typed})
    try:
        validate(email, field="email address")
    except EmailError as exc:
        return render(request, "signup.html", {"error": str(exc), **typed})

    org = Organisation(name=company.strip()[:200])
    db.add(org)
    db.flush()
    # Unique per organisation, not globally: this address may already have an
    # account with somebody else's organisation, and that is none of our
    # business. Within this brand-new one it cannot clash with anything.
    user = User(name=name.strip(), email=email, password_hash=hash_password(password),
                role=Role.BUYER, org_id=org.id)
    db.add(user)
    db.flush()
    record(db, action="org.create", entity_type="organisation", entity_id=org.id, actor=user,
           ip=client_ip(request), detail={"name": org.name})
    record(db, action="user.signup", entity_type="user", entity_id=user.id, actor=user,
           ip=client_ip(request), detail={"role": Role.BUYER.value, "org": org.name})
    db.commit()

    response = redirect("/onboarding",
                        f"{org.name} is set up, and you are its first buyer.")
    set_session_cookie(response, user.id)
    return response


# ------------------------------------------------------------------ invitations
@router.get("/join/{token}")
def join_form(token: str, request: Request, db: Session = Depends(get_db)):
    """Set a password on an invitation. This is how every supplier gets in."""
    data = read_invite(token, config.INVITE_DAYS)
    if not data:
        return render(request, "join.html",
                      {"expired": True,
                       "message": "This invitation link has expired or is not valid. Ask the "
                                  "buyer to send you a new one — publishing the auction again "
                                  "will do it."}, status_code=400)
    existing = (db.query(User)
                  .filter(User.email == data["e"], User.org_id == data["o"]).first())
    if existing:
        return redirect("/login", "You already have an account for that address — please sign "
                                  "in with your password.")
    vendor = db.get(Vendor, data.get("v")) if data.get("v") else None
    if vendor is not None and vendor.org_id != data["o"]:
        vendor = None
    if data["r"] == "vendor" and not vendor:
        return render(request, "join.html",
                      {"expired": True,
                       "message": "The supplier this invitation belongs to is no longer on "
                                  "the system. Ask the buyer to invite you again."},
                      status_code=400)
    return render(request, "join.html",
                  {"token": token, "email": data["e"], "role": data["r"],
                   "vendor": vendor})


@router.post("/join/{token}")
def join(token: str, request: Request, name: str = Form(""), password: str = Form(""),
         confirm: str = Form(""), db: Session = Depends(get_db)):
    data = read_invite(token, config.INVITE_DAYS)
    if not data:
        return render(request, "join.html",
                      {"expired": True,
                       "message": "This invitation link has expired or is not valid. Ask for "
                                  "a new one."}, status_code=400)
    vendor = db.get(Vendor, data.get("v")) if data.get("v") else None
    if vendor is not None and vendor.org_id != data["o"]:
        vendor = None
    again = {"token": token, "email": data["e"], "role": data["r"], "vendor": vendor,
             "name": name}
    if db.query(User).filter(User.email == data["e"], User.org_id == data["o"]).first():
        return redirect("/login", "You already have an account for that address — please sign in.")
    if not name.strip():
        return render(request, "join.html", {**again, "error": "Please type your name."})
    if len(password) < 6:
        return render(request, "join.html",
                      {**again, "error": "Please choose a password of at least 6 characters."})
    if password != confirm:
        return render(request, "join.html",
                      {**again, "error": "Those two passwords are not the same. Type them again."})

    role = Role.VENDOR if data["r"] == "vendor" else Role.BUYER
    if role == Role.VENDOR and vendor is None:
        # The GET refuses this; the POST used to go ahead and make a supplier
        # login attached to no supplier at all, which could then see nothing
        # and bid on nothing.
        return render(request, "join.html",
                      {"expired": True,
                       "message": "The supplier this invitation was for is no longer on the "
                                  "buyer's list. Ask them to invite you again."},
                      status_code=400)
    org = db.get(Organisation, data["o"])
    if org is None:
        return render(request, "join.html",
                      {"expired": True,
                       "message": "The organisation that sent this invitation no longer "
                                  "exists."}, status_code=400)
    user = User(name=name.strip(), email=data["e"], password_hash=hash_password(password),
                role=role, org_id=org.id,
                vendor_id=vendor.id if (role == Role.VENDOR and vendor) else None)
    db.add(user)
    db.flush()
    record(db, action="user.join", entity_type="user", entity_id=user.id, actor=user,
           ip=client_ip(request),
           detail={"role": role.value, "vendor": vendor.name if vendor else None})
    db.commit()
    where = "/" if role == Role.VENDOR else "/onboarding"
    response = redirect(where, f"Welcome, {first_name(user.name)}. Your account is ready.")
    set_session_cookie(response, user.id)
    return response


@router.get("/team")
def team(request: Request, user: User = Depends(current_user),
         db: Session = Depends(get_db)):
    """Who else at your company can sign in, and a box to invite one more."""
    if user.is_vendor:
        colleagues = (db.query(User).filter(User.vendor_id == user.vendor_id)
                        .order_by(User.name).all())
        vendor = db.get(Vendor, user.vendor_id)
        waiting = [address for address in
                   ([vendor.email] + parse_emails(vendor.extra_emails) if vendor else [])
                   if address not in {u.email for u in colleagues}]
    else:
        colleagues = (db.query(User).filter(User.role.in_([Role.BUYER, Role.ADMIN]),
                                            User.org_id == user.org_id)
                        .order_by(User.name).all())
        vendor, waiting = None, []
    return render(request, "team.html",
                  {"colleagues": colleagues, "vendor": vendor, "waiting": waiting},
                  user=user, db=db)


@router.post("/team/invite")
def team_invite(request: Request, email: str = Form(""),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Invite a colleague at your own company. Never anyone else's."""
    try:
        addresses = validate(email, field="email address")
    except EmailError as exc:
        return redirect("/team", str(exc), kind="error")
    if not addresses:
        return redirect("/team", "Type the email address of the colleague you want to invite.",
                        kind="error")
    address = addresses[0]
    # Only within this organisation. The same address having an account with
    # somebody else's organisation is normal and none of our business.
    if db.query(User).filter(User.email == address, User.org_id == user.org_id).first():
        return redirect("/team", f"{address} can already sign in.", kind="error")

    vendor = db.get(Vendor, user.vendor_id) if user.is_vendor else None
    if user.is_vendor and vendor is None:
        return redirect("/team", "Your account is not linked to a supplier record yet. Ask "
                                 "the buyer to check it.", kind="error")
    if vendor is not None:
        # Keep them on the vendor's email list as well, so they hear about the
        # auctions even before they set a password.
        known = parse_emails(vendor.extra_emails)
        if address != (vendor.email or "").lower() and address not in known:
            vendor.extra_emails = "\n".join(known + [address])
    role = "vendor" if user.is_vendor else "buyer"
    token = make_invite(address, role, vendor.id if vendor else None, org_id=user.org_id)
    record(db, action="user.invite", entity_type="user", actor=user,
           ip=client_ip(request), detail={"email": address, "role": role})
    db.commit()
    notify.colleague_invited(db, inviter=user, email=address,
                             vendor=vendor, link=f"/join/{token}")
    return redirect("/team", f"Invitation sent to {address}. The link works for "
                             f"{config.INVITE_DAYS} days.")


@router.get("/onboarding")
def onboarding(request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    counts = {
        "vendors": db.query(Vendor).filter(Vendor.org_id == user.org_id).count(),
        "items": db.query(Item).filter(Item.org_id == user.org_id).count(),
    }
    return render(request, "onboarding.html", {"counts": counts}, user=user, db=db,
                  help_key="dashboard")


@router.post("/onboarding/done")
def onboarding_done(user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.onboarding_done = True
    db.commit()
    return redirect("/", "You're all set. The help button is in the top bar whenever you need it.")


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    """Signing out is a POST, so another site cannot sign someone out with a link."""
    user = current_user_optional(request, db)
    if user:
        record(db, action="user.logout", entity_type="user", entity_id=user.id, actor=user,
               ip=client_ip(request), commit=True)
    response = redirect("/login", "You have been signed out.")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/logout")
def logout_page(request: Request, db: Session = Depends(get_db)):
    """Someone who typed /logout in the address bar, or followed an old link."""
    if not current_user_optional(request, db):
        return redirect("/login")
    return render(request, "logout.html", {})
