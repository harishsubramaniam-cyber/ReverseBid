"""Every key event in one place: in-app notification + email, per recipient.

Adding a new event means adding one function here - routers never build email
bodies themselves.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from . import config
from .mailer import queue_email
from .models import Auction, Notification, Role, User, Vendor
from .utils import fmt_dt, fmt_money

_env = Environment(
    loader=FileSystemLoader(str(config.BASE_DIR / "app" / "templates")),
    autoescape=select_autoescape(["html"]),
)

ACCENTS = {
    "invited": "#1d4ed8", "started": "#0f766e", "outbid": "#b45309",
    "extended": "#7c3aed", "ending_soon": "#b45309", "closed": "#0f172a",
    "awarded": "#15803d", "not_awarded": "#64748b", "cancelled": "#be123c",
    "message": "#0369a1", "approval": "#7c3aed", "bid_received": "#0f766e",
}


# ------------------------------------------------------------------ recipients
def vendor_users(db: Session, vendor_id: int) -> list[User]:
    return db.query(User).filter(User.vendor_id == vendor_id, User.is_active.is_(True)).all()


def participant_users(db: Session, auction: Auction) -> list[User]:
    users: list[User] = []
    for part in auction.participants:
        users.extend(vendor_users(db, part.vendor_id))
    return users


def approvers(db: Session) -> list[User]:
    return db.query(User).filter(User.role.in_([Role.APPROVER, Role.ADMIN]),
                                 User.is_active.is_(True)).all()


# ------------------------------------------------------------------ core send
def send(db: Session, users: Iterable[User], *, event: str, title: str,
         paragraphs: Sequence[str], facts: Sequence[tuple[str, str]] = (),
         cta_text: str = "", link: str = "", note: str = "",
         auction: Auction | None = None, in_app: bool = True) -> int:
    """Deliver one event to many users. Returns the number of emails queued."""
    template = _env.get_template("emails/base.html")
    count = 0
    seen: set[int] = set()
    for user in users:
        if not user or user.id in seen or not user.is_active:
            continue
        seen.add(user.id)
        if in_app:
            db.add(Notification(user_id=user.id, event=event, title=title,
                                body=" ".join(_strip(p) for p in paragraphs)[:800],
                                link=link))
        html = template.render(
            app_name=config.APP_NAME, title=title, greeting=user.name.split()[0],
            paragraphs=paragraphs, facts=facts, accent=ACCENTS.get(event, "#1d4ed8"),
            cta_text=cta_text or "Open in the app",
            cta_url=(config.BASE_URL + link) if link else "",
            note=note,
        )
        queue_email(db, to_email=user.email, to_name=user.name, subject=title,
                    html_body=html, event=event,
                    auction_id=auction.id if auction else None)
        count += 1
    db.commit()
    return count


def _strip(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html)


def _auction_facts(auction: Auction) -> list[tuple[str, str]]:
    return [
        ("Auction", f"{auction.reference} — {auction.title}"),
        ("Items", str(len(auction.lines))),
        ("Starts", fmt_dt(auction.start_at)),
        ("Ends", fmt_dt(auction.end_at)),
        ("Starting price (ceiling)", fmt_money(auction.baseline_value)),
    ]


# ------------------------------------------------------------------ events
def auction_invited(db: Session, auction: Auction) -> int:
    return send(
        db, participant_users(db, auction), event="invited", auction=auction,
        title=f"You are invited to bid: {auction.title}",
        paragraphs=[
            "You have been invited to a <b>reverse auction</b>. That means the "
            "<b>lowest</b> price wins, and you can keep lowering your bid until the clock stops.",
            f"The starting price is the <b>maximum</b> the buyer will consider. Every bid you "
            f"place must be at least "
            f"<b>{fmt_money(auction.min_decrement) if auction.decrement_type.value == 'absolute' else str(auction.min_decrement) + '%'}</b> "
            "below the current best price.",
        ],
        facts=_auction_facts(auction),
        cta_text="View the auction", link=f"/auctions/{auction.id}",
        note="You will get an email when the auction opens, and again if someone outbids you.",
    )


def auction_starting_soon(db: Session, auction: Auction) -> int:
    return send(db, participant_users(db, auction), event="invited", auction=auction,
                title=f"Starts soon: {auction.title}",
                paragraphs=["This auction opens shortly. Have your prices ready."],
                facts=_auction_facts(auction), cta_text="Go to the auction",
                link=f"/auctions/{auction.id}")


def auction_started(db: Session, auction: Auction) -> int:
    return send(db, participant_users(db, auction), event="started", auction=auction,
                title=f"Bidding is open: {auction.title}",
                paragraphs=["The auction is live. Place your bid now — the lowest price wins."],
                facts=_auction_facts(auction), cta_text="Place a bid",
                link=f"/auctions/{auction.id}")


def bid_received(db: Session, auction: Auction, user: User, line_label: str,
                 unit_price: float, rank: int) -> int:
    position = "You are currently L1 (lowest)." if rank == 1 else f"You are currently at rank L{rank}."
    return send(db, [user], event="bid_received", auction=auction,
                title=f"Bid received: {auction.title}",
                paragraphs=[f"We recorded your bid on <b>{line_label}</b>. {position}"],
                facts=[("Item", line_label), ("Your price", fmt_money(unit_price)),
                       ("Your rank", f"L{rank}"), ("Auction ends", fmt_dt(auction.end_at))],
                cta_text="View the auction", link=f"/auctions/{auction.id}", in_app=False)


def outbid(db: Session, auction: Auction, vendor: Vendor, line_label: str,
           new_best: float, your_price: float) -> int:
    return send(db, vendor_users(db, vendor.id), event="outbid", auction=auction,
                title=f"You have been outbid: {auction.title}",
                paragraphs=[
                    f"Someone has gone below your price on <b>{line_label}</b>. "
                    "You can still win by placing a lower bid before the clock stops.",
                ],
                facts=[("Item", line_label), ("Your price", fmt_money(your_price)),
                       ("Current lowest", fmt_money(new_best)),
                       ("Auction ends", fmt_dt(auction.end_at))],
                cta_text="Bid again", link=f"/auctions/{auction.id}")


def auction_extended(db: Session, auction: Auction, seconds: int) -> int:
    minutes = round(seconds / 60, 1)
    return send(db, participant_users(db, auction) + [auction.creator],
                event="extended", auction=auction,
                title=f"Time extended: {auction.title}",
                paragraphs=[f"A bid arrived in the closing moments, so the auction was "
                            f"automatically extended by <b>{minutes} minutes</b> to keep it fair."],
                facts=[("New end time", fmt_dt(auction.end_at)),
                       ("Extensions used", f"{auction.extensions_used} of {auction.max_extensions}")],
                cta_text="Open the auction", link=f"/auctions/{auction.id}")


def ending_soon(db: Session, auction: Auction) -> int:
    return send(db, participant_users(db, auction), event="ending_soon", auction=auction,
                title=f"Closing soon: {auction.title}",
                paragraphs=["This auction closes shortly. This is your last chance to improve "
                            "your price."],
                facts=[("Ends", fmt_dt(auction.end_at))],
                cta_text="Place your final bid", link=f"/auctions/{auction.id}")


def auction_closed(db: Session, auction: Auction) -> int:
    return send(db, participant_users(db, auction) + [auction.creator],
                event="closed", auction=auction,
                title=f"Bidding closed: {auction.title}",
                paragraphs=["Bidding is now closed. The buyer will review the bids and award "
                            "the business. You will be told the outcome by email."],
                facts=[("Closed at", fmt_dt(auction.closed_at or auction.end_at))],
                cta_text="View results", link=f"/auctions/{auction.id}")


def awarded(db: Session, auction: Auction, vendor: Vendor, rows: list[tuple[str, str, str]],
            total: float) -> int:
    facts = [(f"{item}", f"{qty} @ {price}") for item, qty, price in rows]
    facts.append(("Total awarded", fmt_money(total)))
    return send(db, vendor_users(db, vendor.id), event="awarded", auction=auction,
                title=f"Congratulations — you have been awarded: {auction.title}",
                paragraphs=["The buyer has awarded you the following items from this auction. "
                            "The buyer will be in touch with next steps."],
                facts=facts, cta_text="View the award", link=f"/auctions/{auction.id}")


def not_awarded(db: Session, auction: Auction, vendor: Vendor) -> int:
    return send(db, vendor_users(db, vendor.id), event="not_awarded", auction=auction,
                title=f"Outcome: {auction.title}",
                paragraphs=["Thank you for taking part. On this occasion the business was "
                            "awarded elsewhere. We hope to see you in the next auction."],
                cta_text="View the auction", link=f"/auctions/{auction.id}")


def auction_cancelled(db: Session, auction: Auction, reason: str) -> int:
    return send(db, participant_users(db, auction), event="cancelled", auction=auction,
                title=f"Auction cancelled: {auction.title}",
                paragraphs=["The buyer has cancelled this auction. No award will be made.",
                            f"Reason given: <i>{reason or 'not stated'}</i>"],
                cta_text="View the auction", link=f"/auctions/{auction.id}")


def message_posted(db: Session, auction: Auction, recipients: list[User], sender: User,
                   body: str) -> int:
    preview = body if len(body) <= 160 else body[:157] + "…"
    return send(db, recipients, event="message", auction=auction,
                title=f"New message on {auction.reference}",
                paragraphs=[f"<b>{sender.name}</b> wrote:", f"<i>{preview}</i>"],
                cta_text="Reply", link=f"/auctions/{auction.id}#conversation")


def approval_requested(db: Session, auction: Auction, requester: User) -> int:
    return send(db, approvers(db), event="approval", auction=auction,
                title=f"Approval needed: {auction.title}",
                paragraphs=[f"<b>{requester.name}</b> has sent this auction for your approval. "
                            "You can approve it, reject it, or send it back for rework."],
                facts=_auction_facts(auction),
                cta_text="Review the auction", link=f"/auctions/{auction.id}")


def approval_decided(db: Session, auction: Auction, status: str, comments: str) -> int:
    wording = {"approved": "approved and is now scheduled",
               "rejected": "rejected", "rework": "sent back to you for rework"}
    return send(db, [auction.creator], event="approval", auction=auction,
                title=f"Auction {status}: {auction.title}",
                paragraphs=[f"Your auction has been <b>{wording.get(status, status)}</b>.",
                            f"Approver's comments: <i>{comments or 'none'}</i>"],
                cta_text="Open the auction", link=f"/auctions/{auction.id}")


def bid_withdrawn(db: Session, auction: Auction, vendor: Vendor, line_label: str) -> int:
    return send(db, [auction.creator], event="closed", auction=auction,
                title=f"Bid withdrawn on {auction.reference}",
                paragraphs=[f"<b>{vendor.name}</b> has withdrawn their bid on "
                            f"<b>{line_label}</b>. Ranks have been recalculated."],
                cta_text="View the auction", link=f"/auctions/{auction.id}")
