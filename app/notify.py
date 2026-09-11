"""Every key event in one place: in-app notification + email, per recipient.

Adding a new event means adding one function here - routers never build email
bodies themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from . import config
from .emails_util import parse as parse_emails
from .mailer import queue_email
from .models import Auction, Notification, Participant, User, Vendor
from .utils import fmt_dt, fmt_money, first_name

_env = Environment(
    loader=FileSystemLoader(str(config.BASE_DIR / "app" / "templates")),
    autoescape=select_autoescape(["html"]),
)

ACCENTS = {
    "invited": "#1d4ed8", "started": "#0f766e", "outbid": "#b45309",
    "extended": "#7c3aed", "ending_soon": "#b45309", "closed": "#0f172a",
    "awarded": "#15803d", "not_awarded": "#64748b", "cancelled": "#be123c",
    "message": "#0369a1", "bid_received": "#0f766e", "published": "#1d4ed8",
    "starting_soon": "#1d4ed8", "withdrawn": "#b45309", "updated": "#7c3aed",
}


# ------------------------------------------------------------------ recipients
@dataclass
class Recipient:
    """Someone to tell about an event.

    ``email`` may be blank for a person who only gets the in-app alert (their
    address is already covered by an override), and ``user_id`` may be ``None``
    for a plain address with no login - a vendor's second contact, or one of
    the buyer's own colleagues on the copy list.
    """
    name: str
    email: str = ""
    user_id: int | None = None
    #: The vendor this contact belongs to, when they are a bidder's contact.
    #: Anyone here without a login is offered a link that creates one.
    vendor_id: int | None = None

    @property
    def key(self) -> str:
        return self.email.lower() or f"user-{self.user_id}"


def _from_user(user: User) -> Recipient:
    return Recipient(name=user.name, email=user.email, user_id=user.id,
                     vendor_id=user.vendor_id)


def _name_from_email(address: str) -> str:
    """A friendly-enough greeting for an address with no account behind it."""
    local = address.split("@")[0]
    return local.replace(".", " ").replace("_", " ").title() or "there"


def vendor_users(db: Session, vendor_id: int) -> list[User]:
    return db.query(User).filter(User.vendor_id == vendor_id, User.is_active.is_(True)).all()


def vendor_recipients(db: Session, vendor_id: int,
                      auction: Auction | None = None) -> list[Recipient]:
    """Everyone who should hear about this auction on behalf of one vendor.

    Precedence: an address list typed against this auction's invitation wins;
    otherwise the vendor's own list (primary email plus any extra contacts).
    Users with logins always keep their in-app alert either way.
    """
    override: list[str] = []
    if auction is not None:
        part = (db.query(Participant)
                  .filter_by(auction_id=auction.id, vendor_id=vendor_id).first())
        if part and part.notify_emails:
            override = parse_emails(part.notify_emails)

    addresses = override
    if not addresses:
        vendor = db.get(Vendor, vendor_id)
        addresses = ([vendor.email.lower()] if vendor and vendor.email else [])
        if vendor:
            addresses += [a for a in parse_emails(vendor.extra_emails) if a not in addresses]

    out: dict[str, Recipient] = {}
    for address in addresses:
        out[address] = Recipient(name=_name_from_email(address), email=address,
                                 vendor_id=vendor_id)
    for user in vendor_users(db, vendor_id):
        key = user.email.lower()
        if key in out or not override:
            out[key] = _from_user(user)          # a real name beats a guessed one
        else:
            # Their address was replaced for this auction - keep the in-app alert.
            out[f"user-{user.id}"] = Recipient(name=user.name, user_id=user.id,
                                               vendor_id=vendor_id)
    return list(out.values())


def participant_users(db: Session, auction: Auction) -> list[Recipient]:
    """Every bidder contact on an auction, across all invited vendors."""
    out: list[Recipient] = []
    for part in auction.participants:
        out.extend(vendor_recipients(db, part.vendor_id, auction))
    return out


def cc_recipients(db: Session, auction: Auction) -> list[Recipient]:
    """The buyer's own copy list for this auction - no login required."""
    return [Recipient(name=_name_from_email(a), email=a)
            for a in parse_emails(auction.cc_emails)]


# ------------------------------------------------------------------ core send
def send(db: Session, users: Iterable[User | Recipient], *, event: str, title: str,
         paragraphs: Sequence[str], facts: Sequence[tuple[str, str]] = (),
         cta_text: str = "", link: str = "", note: str = "",
         auction: Auction | None = None, in_app: bool = True,
         org_id: int | None = None) -> int:
    """Deliver one event to many users. Returns the number of emails queued.

    A bidder contact with no login gets a link that sets a password, on every
    email - not just the first invitation. Otherwise the button in "bidding is
    open" or "you have been outbid" drops them on a sign-in page for an
    account that does not exist, which is a dead end at the worst moment.
    """
    template = _env.get_template("emails/base.html")
    count = 0
    seen: set[str] = set()
    for entry in users:
        if entry is None:
            continue
        person = entry if isinstance(entry, Recipient) else _from_user(entry)
        if isinstance(entry, User) and not entry.is_active:
            continue
        if person.key in seen:
            continue
        seen.add(person.key)
        if in_app and person.user_id:
            db.add(Notification(user_id=person.user_id, event=event, title=title,
                                body=" ".join(_strip(p) for p in paragraphs)[:800],
                                link=link))
        if not person.email:
            continue
        person_link, person_cta, person_note = link, cta_text, note
        person_paragraphs = list(paragraphs)
        if person.vendor_id and not person.user_id:
            # No account yet: send them the one link that can create it.
            from .security import make_invite
            token = make_invite(person.email, "vendor", person.vendor_id,
                                org_id=auction.org_id if auction is not None else None)
            person_link = f"/join/{token}"
            person_cta = "Set your password and bid"
            person_paragraphs.append(
                "You do not have a password for this platform yet. The button below sets one "
                "up — it takes a moment, and then you can bid.")
            person_note = (f"This link is just for {person.email} and works for "
                           f"{config.INVITE_DAYS} days.")
        html = template.render(
            app_name=config.APP_NAME, title=title,
            greeting=first_name(person.name),
            paragraphs=person_paragraphs, facts=facts,
            accent=ACCENTS.get(event, "#1d4ed8"),
            cta_text=person_cta or "Open in the app",
            cta_url=(config.BASE_URL + person_link) if person_link else "",
            note=person_note,
        )
        queue_email(db, to_email=person.email, to_name=person.name, subject=title,
                    html_body=html, event=event,
                    auction_id=auction.id if auction else None,
                    org_id=(auction.org_id if auction is not None else org_id))
        count += 1
    db.commit()
    return count


def _strip(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html)


def _auction_facts(auction: Auction) -> list[tuple[str, str]]:
    """The summary box at the top of an auction email.

    The money line used to be labelled "Starting price (ceiling)" while
    actually holding quantity x ceiling added up across every item — so a
    single-line auction for 10 units at ₹100 told bidders the ceiling was
    ₹1,000, and the engine then refused anything above ₹100.
    """
    priced = [line for line in auction.lines if line.has_ceiling]
    facts = [
        ("Auction", f"{auction.reference} — {auction.title}"),
        ("Items", str(len(auction.lines))),
        ("Starts", fmt_dt(auction.start_at)),
        ("Ends", fmt_dt(auction.end_at)),
    ]
    if not priced:
        facts.append(("Starting price (ceiling)",
                      "not set — open at any price you like"))
    elif len(auction.lines) == 1:
        facts.append(("Starting price (ceiling)",
                      f"{fmt_money(priced[0].starting_price)} per "
                      f"{priced[0].unit.code if priced[0].unit else 'unit'}"))
    else:
        note = "" if len(priced) == len(auction.lines) else \
            f" ({len(auction.lines) - len(priced)} item(s) have no ceiling)"
        facts.append(("Value at the starting prices",
                      f"{fmt_money(auction.baseline_value)}{note}"))
    return facts


# ------------------------------------------------------------------ events
def buyer_update(db: Session, auction: Auction, *, title: str, paragraphs: Sequence[str],
                 facts: Sequence[tuple[str, str]] = (), event: str = "closed",
                 cta_text: str = "Open the auction") -> int:
    """A copy of a buyer-side milestone for the creator and the copy list.

    The creator is written to separately: the copy-list footnote is true for
    their colleagues and puzzling for the person who created the auction.
    """
    sent = send(db, [auction.creator], event=event, auction=auction, title=title,
                paragraphs=paragraphs, facts=facts, cta_text=cta_text,
                link=f"/auctions/{auction.id}")
    copies = cc_recipients(db, auction)
    if copies:
        creator_address = (auction.creator.email or "").lower()
        copies = [person for person in copies if person.email.lower() != creator_address]
    if copies:
        sent += send(
            db, copies, event=event, auction=auction, title=title, paragraphs=paragraphs,
            facts=facts, cta_text=cta_text, link=f"/auctions/{auction.id}",
            note="You are receiving this because you are on the copy list for this auction.")
    return sent


def auction_published(db: Session, auction: Auction, invited: int) -> int:
    return buyer_update(
        db, auction, event="published",
        title=f"Auction published: {auction.title}",
        paragraphs=[f"The auction is live on the calendar and <b>{invited}</b> bidder contact(s) "
                    "have been invited by email."],
        facts=_auction_facts(auction), cta_text="Watch the bidding")


def award_summary(db: Session, auction: Auction, rows: Sequence[tuple[str, str, str]],
                  total: float, savings: float, savings_pct: float) -> int:
    facts = [(item, f"{who} — {value}") for item, who, value in rows]
    facts += [("Awarded value", fmt_money(total)),
              ("Savings", f"{fmt_money(savings)} ({savings_pct:.1f}%)")]
    return buyer_update(
        db, auction, event="awarded",
        title=f"Awarded: {auction.title}",
        paragraphs=["The auction has been awarded and every bidder has been told the outcome."],
        facts=facts, cta_text="See the award")


def auction_invited(db: Session, auction: Auction) -> int:
    """Invite every bidder contact on the auction.

    Sent one vendor at a time, because a contact with no login gets a link
    that sets a password for *that* supplier - a supplier never picks which
    company they belong to.
    """
    step = (fmt_money(auction.min_decrement) if auction.decrement_type.value == "absolute"
            else f"{auction.min_decrement:g}%")
    delivered = ("", "")
    if auction.compare_landed:
        delivered = (
            "This auction is decided on the <b>delivered</b> price: your bid plus your own "
            "freight, duty and packaging as agreed with the buyer. The bidding screen shows "
            "you both numbers, and the exact price to type to take the lead.", "")
    sent = 0
    for part in auction.participants:
        paragraphs = [
            "You have been invited to a <b>reverse auction</b>. That means the "
            "<b>lowest</b> price wins, and you can keep lowering your bid until the clock "
            "stops.",
            f"The starting price is the <b>maximum</b> the buyer will consider. Every bid you "
            f"place must be at least <b>{step}</b> below the current best price.",
        ]
        if delivered[0]:
            paragraphs.append(delivered[0])
        sent += send(
            db, vendor_recipients(db, part.vendor_id, auction), event="invited",
            auction=auction,
            title=f"You are invited to bid: {auction.title}",
            paragraphs=paragraphs,
            facts=_auction_facts(auction) + _adder_facts(db, part),
            cta_text="View the auction", link=f"/auctions/{auction.id}",
            note="You will get an email when the auction opens, and again if someone "
                 "outbids you.",
        )
    return sent


def _adder_facts(db: Session, part) -> list[tuple[str, str]]:
    """What this bidder's delivered costs add, spelled out in their own email."""
    if part is None or not part.auction or not part.auction.compare_landed:
        return []
    from .engine import adders_from
    adders = adders_from(part)
    if not adders.any:
        return [("Your delivered costs", "none agreed — your bid is your delivered price")]
    return [("Your delivered costs", adders.describe())]


def auction_starting_soon(db: Session, auction: Auction) -> int:
    return send(db, participant_users(db, auction), event="starting_soon", auction=auction,
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
           new_best: float, your_price: float, landed: bool = False) -> int:
    # Where the auction is compared on delivered cost, these are delivered
    # prices - saying "your price" would be quoting a number the bidder never
    # typed, so the labels say which it is.
    yours = "Your delivered price" if landed else "Your price"
    theirs = "Current lowest delivered price" if landed else "Current lowest"
    return send(db, vendor_recipients(db, vendor.id, auction), event="outbid", auction=auction,
                title=f"You have been outbid: {auction.title}",
                paragraphs=[
                    f"Someone has gone below your price on <b>{line_label}</b>. "
                    "You can still win by placing a lower bid before the clock stops."
                    + (" This auction is compared on the <b>delivered</b> price — your bid "
                       "plus your freight, duty and packaging." if landed else ""),
                ],
                facts=[("Item", line_label), (yours, fmt_money(your_price)),
                       (theirs, fmt_money(new_best)),
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
    return send(db, participant_users(db, auction) + [auction.creator]
                + cc_recipients(db, auction),
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
    return send(db, vendor_recipients(db, vendor.id, auction), event="awarded", auction=auction,
                title=f"Congratulations — you have been awarded: {auction.title}",
                paragraphs=["The buyer has awarded you the following items from this auction. "
                            "The buyer will be in touch with next steps."],
                facts=facts, cta_text="View the award", link=f"/auctions/{auction.id}")


def not_awarded(db: Session, auction: Auction, vendor: Vendor) -> int:
    return send(db, vendor_recipients(db, vendor.id, auction), event="not_awarded", auction=auction,
                title=f"Outcome: {auction.title}",
                paragraphs=["Thank you for taking part. On this occasion the business was "
                            "awarded elsewhere. We hope to see you in the next auction."],
                cta_text="View the auction", link=f"/auctions/{auction.id}")


def auction_cancelled(db: Session, auction: Auction, reason: str) -> int:
    return send(db, participant_users(db, auction) + [auction.creator]
                + cc_recipients(db, auction),
                event="cancelled", auction=auction,
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
                cta_text="Reply", link=f"/auctions/{auction.id}?tab=conversation#conversation")


def auction_changed(db: Session, auction: Auction, newly_invited: list[Vendor],
                    changes: list[str]) -> int:
    """Told to bidders after a published auction is edited.

    Editing a scheduled auction used to be silent: vendors added on the edit
    could bid without ever being invited, and a moved closing time reached
    nobody.
    """
    sent = 0
    for vendor in newly_invited:
        sent += send(
            db, vendor_recipients(db, vendor.id, auction), event="invited", auction=auction,
            title=f"You are invited to bid: {auction.title}",
            paragraphs=[
                "You have been added to a <b>reverse auction</b>. The <b>lowest</b> price "
                "wins, and you can keep lowering your bid until the clock stops.",
                "The starting price is the <b>maximum</b> the buyer will consider.",
            ],
            facts=_auction_facts(auction), cta_text="View the auction",
            link=f"/auctions/{auction.id}")
    if not changes:
        return sent
    new_ids = {vendor.id for vendor in newly_invited}
    already = [person for part in auction.participants if part.vendor_id not in new_ids
               for person in vendor_recipients(db, part.vendor_id, auction)]
    if already:
        sent += send(
            db, already, event="updated", auction=auction,
            title=f"Updated: {auction.title}",
            paragraphs=["The buyer has changed this auction. Here is what is different."],
            facts=[("Changed", change) for change in changes] + _auction_facts(auction),
            cta_text="Open the auction", link=f"/auctions/{auction.id}")
    return sent


def bid_withdrawn(db: Session, auction: Auction, vendor: Vendor, line_label: str) -> int:
    return send(db, [auction.creator], event="withdrawn", auction=auction,
                title=f"Bid withdrawn on {auction.reference}",
                paragraphs=[f"<b>{vendor.name}</b> has withdrawn their bid on "
                            f"<b>{line_label}</b>. Ranks have been recalculated."],
                cta_text="View the auction", link=f"/auctions/{auction.id}")


def colleague_invited(db: Session, inviter: User, email: str, vendor, link: str) -> int:
    """Someone asking a colleague at their own company to join them."""
    where = vendor.name if vendor is not None else config.APP_NAME
    return send(
        db, [Recipient(name=_name_from_email(email), email=email)],
        org_id=inviter.org_id, event="invited", title=f"{inviter.name} has invited you to {where}",
        paragraphs=[
            f"<b>{inviter.name}</b> has invited you to join <b>{where}</b> on "
            f"{config.APP_NAME}"
            + (" so you can bid in the buyer's reverse auctions." if vendor is not None
               else ", where your team runs its reverse auctions."),
            "The button below sets your password. Nobody else can use this link.",
        ],
        cta_text="Set your password", link=link,
        note=f"This link is just for {email} and works for {config.INVITE_DAYS} days.")
