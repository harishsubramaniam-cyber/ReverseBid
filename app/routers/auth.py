from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..audit import record
from ..db import get_db
from ..models import Item, Role, User, Vendor
from ..security import (SESSION_COOKIE, clear_failed_logins, current_user,
                        current_user_optional, hash_password, login_blocked,
                        note_failed_login, safe_next, set_session_cookie, verify_password)
from ..utils import humanize_seconds
from ..web import client_ip, redirect, render

router = APIRouter()


@router.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
    if current_user_optional(request, db):
        return redirect("/")
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""),
          next: str = Form("/"), db: Session = Depends(get_db)):
    # Only ever send people on to a page inside this app: ?next= arrives from
    # the address bar, so it must not be able to bounce them to another site.
    next = safe_next(next)
    typed = email.strip().lower()
    if not typed or not password:
        return render(request, "login.html",
                      {"next": next, "error": "Please type both your email and your password.",
                       "email": email})

    # Slow down password guessing, per address and per computer.
    throttle_key = f"{typed}|{client_ip(request)}"
    wait = login_blocked(throttle_key)
    if wait:
        return render(request, "login.html",
                      {"next": next, "email": email,
                       "error": "Too many sign-in attempts. Please wait "
                                f"{humanize_seconds(wait)} and try again, or reset the "
                                "password if you have forgotten it."}, status_code=200)

    user = db.query(User).filter(User.email == typed).first()
    if not user or not verify_password(password, user.password_hash) or not user.is_active:
        note_failed_login(throttle_key)
        return render(request, "login.html",
                      {"next": next, "error": "That email and password don't match an account.",
                       "email": email}, status_code=200)
    clear_failed_logins(throttle_key)
    record(db, action="user.login", entity_type="user", entity_id=user.id, actor=user,
           ip=client_ip(request), commit=True)
    response = redirect(next or "/", f"Welcome back, {user.name.split()[0]}.")
    set_session_cookie(response, user.id)
    return response


@router.get("/signup")
def signup_form(request: Request):
    return render(request, "signup.html", {})


@router.post("/signup")
def signup(request: Request, name: str = Form(""), email: str = Form(""),
           password: str = Form(""), account_type: str = Form("buyer"),
           company: str = Form(""), db: Session = Depends(get_db)):
    email = email.strip().lower()
    # Keep everything they typed - including which kind of account they asked
    # for. Losing account_type silently turned a supplier into a buyer.
    typed = {"name": name, "email": email, "account_type": account_type, "company": company}
    if not name.strip() or not email:
        return render(request, "signup.html",
                      {"error": "Please fill in your name and email address.", **typed})
    if db.query(User).filter(User.email == email).first():
        return render(request, "signup.html",
                      {"error": "There is already an account with that email.", **typed})
    if len(password) < 6:
        return render(request, "signup.html",
                      {"error": "Please choose a password of at least 6 characters.", **typed})
    if account_type not in ("buyer", "vendor"):
        return render(request, "signup.html",
                      {"error": "Choose whether this is a buying account or a supplier "
                                "account.", **typed})

    vendor_id = None
    role = Role.VENDOR if account_type == "vendor" else Role.BUYER
    if role == Role.VENDOR:
        vendor = Vendor(name=company.strip() or name, email=email)
        db.add(vendor)
        db.flush()
        vendor_id = vendor.id

    user = User(name=name.strip(), email=email, password_hash=hash_password(password),
                role=role, vendor_id=vendor_id)
    db.add(user)
    db.flush()
    record(db, action="user.signup", entity_type="user", entity_id=user.id, actor=user,
           ip=client_ip(request), detail={"role": role.value})
    db.commit()

    response = redirect("/onboarding", "Your account is ready.")
    set_session_cookie(response, user.id)
    return response


@router.get("/onboarding")
def onboarding(request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    counts = {
        "vendors": db.query(Vendor).count(),
        "items": db.query(Item).count(),
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
