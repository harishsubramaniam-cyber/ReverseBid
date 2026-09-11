"""Domain model for the reverse auction platform.

Reverse auction semantics throughout: the starting price is a CEILING, bids
move DOWNWARD, and rank 1 (L1) is the lowest price.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class Role(str, enum.Enum):
    ADMIN = "admin"
    BUYER = "buyer"        # creates and runs auctions
    VENDOR = "vendor"      # bids


class AuctionStatus(str, enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    LIVE = "live"
    CLOSED = "closed"
    AWARDED = "awarded"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = (AuctionStatus.SCHEDULED, AuctionStatus.LIVE)


class DecrementType(str, enum.Enum):
    ABSOLUTE = "absolute"
    PERCENT = "percent"


# --------------------------------------------------------------------------- masters
class Organisation(Base):
    """One buying organisation, and everything that belongs to it.

    The platform used to be one company per installation: the first account
    owned the whole thing and sign-up closed behind it. An organisation is
    that same idea made into a row, so several buying companies can each run
    their own auctions on one deployment without ever seeing each other.

    Everything a buyer creates hangs off this: their colleagues, their
    supplier list, their items and units, their auctions - and, through the
    auctions, every bid, award, message and document. Supplier logins belong
    to it too, so a supplier who sells to two of these companies has an
    account with each, exactly as they have a separate account with each
    customer's own portal today.
    """
    __tablename__ = "organisations"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    created_at = Column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    #: An address identifies a person *within* one organisation. The same
    #: person may hold an account with several buying organisations - a
    #: supplier usually will - and each is a separate login.
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_user_org_email"),)
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organisations.id"), nullable=True, index=True)
    email = Column(String(200), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    password_hash = Column(String(300), nullable=False)
    role = Column(Enum(Role), nullable=False, default=Role.BUYER)
    phone = Column(String(40), default="")
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    is_active = Column(Boolean, default=True)
    onboarding_done = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)

    vendor = relationship("Vendor", back_populates="users", foreign_keys=[vendor_id])
    org = relationship("Organisation")

    @property
    def is_vendor(self) -> bool:
        return self.role == Role.VENDOR

    @property
    def is_buyer_side(self) -> bool:
        return self.role in (Role.BUYER, Role.ADMIN)


class Vendor(Base):
    """Vendor master. Only name + email are mandatory - deliberately lighter."""
    __tablename__ = "vendors"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organisations.id"), nullable=True, index=True)
    name = Column(String(200), nullable=False)              # mandatory
    email = Column(String(200), nullable=False)             # mandatory
    code = Column(String(50), default="")
    contact_person = Column(String(200), default="")
    phone = Column(String(40), default="")
    #: Extra people at this vendor who should also get every email, one per
    #: line or comma separated. The primary ``email`` always receives too.
    extra_emails = Column(Text, default="")
    address = Column(Text, default="")
    gstin = Column(String(40), default="")
    is_active = Column(Boolean, default=True)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=utcnow)

    # --- what this vendor's price usually costs on top, to get it delivered.
    #     These are only defaults: they pre-fill the auction form, and each
    #     auction keeps its own copy so history cannot be rewritten later.
    default_freight = Column(Float, default=0.0)
    default_freight_basis = Column(String(10), default="unit")     # unit | percent
    default_duty = Column(Float, default=0.0)
    default_duty_basis = Column(String(10), default="percent")
    default_packaging = Column(Float, default=0.0)
    default_packaging_basis = Column(String(10), default="unit")
    default_other = Column(Float, default=0.0)
    default_other_basis = Column(String(10), default="unit")
    default_other_label = Column(String(60), default="")

    users = relationship("User", back_populates="vendor",
                         foreign_keys="User.vendor_id")

    @property
    def has_default_adders(self) -> bool:
        return any([self.default_freight, self.default_duty,
                    self.default_packaging, self.default_other])


class Unit(Base):
    """Unit of measure master. Only code is mandatory."""
    __tablename__ = "units"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organisations.id"), nullable=True, index=True)
    code = Column(String(30), nullable=False)               # mandatory, per org
    name = Column(String(120), default="")
    created_at = Column(DateTime, default=utcnow)


class Item(Base):
    """Item master. Only name is mandatory."""
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organisations.id"), nullable=True, index=True)
    name = Column(String(250), nullable=False)              # mandatory
    code = Column(String(60), default="")
    description = Column(Text, default="")
    category = Column(String(120), default="")
    default_unit_id = Column(Integer, ForeignKey("units.id"), nullable=True)
    is_active = Column(Boolean, default=True)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=utcnow)

    default_unit = relationship("Unit")


# --------------------------------------------------------------------------- auction
class Auction(Base):
    __tablename__ = "auctions"
    __table_args__ = (UniqueConstraint("org_id", "reference", name="uq_auction_org_ref"),)
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organisations.id"), nullable=True, index=True)
    #: Unique inside the organisation that owns it, not across the platform:
    #: each buyer's numbering starts at their own first auction, so nobody can
    #: infer how much business anybody else is doing from a reference number.
    reference = Column(String(40), index=True)
    title = Column(String(250), nullable=False)
    description = Column(Text, default="")
    terms = Column(Text, default="")
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(Enum(AuctionStatus), default=AuctionStatus.DRAFT, index=True)
    currency = Column(String(10), default="INR")

    start_at = Column(DateTime, nullable=False)
    end_at = Column(DateTime, nullable=False)          # editable while not live
    original_end_at = Column(DateTime, nullable=False)

    # --- engine rules
    decrement_type = Column(Enum(DecrementType), default=DecrementType.ABSOLUTE)
    min_decrement = Column(Float, default=0.0)         # required improvement per bid
    max_decrement = Column(Float, default=0.0)         # 0 = no cap
    show_rank = Column(Boolean, default=True)
    show_lowest_bid = Column(Boolean, default=True)
    hide_bidder_names = Column(Boolean, default=True)
    #: Compare bidders on their DELIVERED price - each bidder's own freight,
    #: duty and packaging added to what they bid. Off by default, so an
    #: auction that says nothing about it behaves exactly as it always has.
    #: When it is on, the starting price is a delivered ceiling, ranks and
    #: decrements work on delivered prices, and so does the savings maths.
    compare_landed = Column(Boolean, default=False)

    auto_extend = Column(Boolean, default=True)
    extend_trigger_seconds = Column(Integer, default=120)
    extend_by_seconds = Column(Integer, default=180)
    max_extensions = Column(Integer, default=5)
    extensions_used = Column(Integer, default=0)

    #: The buyer's own people who get a copy of the buyer-side events
    #: (published, closed, awarded, cancelled). No login needed.
    cc_emails = Column(Text, default="")

    # --- lifecycle bookkeeping
    published_at = Column(DateTime)
    started_at = Column(DateTime)
    closed_at = Column(DateTime)
    awarded_at = Column(DateTime)
    cancelled_at = Column(DateTime)
    cancel_reason = Column(Text, default="")
    starting_soon_notified = Column(Boolean, default=False)
    ending_soon_notified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)

    creator = relationship("User")
    lines = relationship("AuctionLine", back_populates="auction",
                         cascade="all, delete-orphan", order_by="AuctionLine.id")
    participants = relationship("Participant", back_populates="auction",
                                cascade="all, delete-orphan")
    bids = relationship("Bid", back_populates="auction", cascade="all, delete-orphan")

    # ---- derived values
    @property
    def baseline_value(self) -> float:
        return sum(l.qty * (l.starting_price or 0.0) for l in self.lines)

    @property
    def is_live(self) -> bool:
        return self.status == AuctionStatus.LIVE

    @property
    def editable(self) -> bool:
        return self.status in (AuctionStatus.DRAFT, AuctionStatus.SCHEDULED)

    @property
    def published(self) -> bool:
        """Have the bidders been told about this auction at all?"""
        return self.published_at is not None or self.status not in (AuctionStatus.DRAFT,)


class AuctionLine(Base):
    __tablename__ = "auction_lines"
    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    unit_id = Column(Integer, ForeignKey("units.id"), nullable=True)
    qty = Column(Float, default=1.0)
    #: Per-unit CEILING. Optional - leave it empty and bidders may open at any
    #: price; savings are then measured from the highest bid received.
    starting_price = Column(Float, nullable=True)
    specification = Column(Text, default="")

    auction = relationship("Auction", back_populates="lines")
    item = relationship("Item")
    unit = relationship("Unit")

    @property
    def has_ceiling(self) -> bool:
        return self.starting_price is not None and self.starting_price > 0

    @property
    def baseline(self) -> float:
        """Budget for this line. Zero when no ceiling was set - use
        ``engine.line_baseline`` where bids should stand in for it."""
        return self.qty * (self.starting_price or 0.0)


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (UniqueConstraint("auction_id", "vendor_id"),)
    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=False)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False)
    invited_at = Column(DateTime, default=utcnow)
    alias = Column(String(30), default="")   # "Bidder A" when names are hidden
    #: Addresses to use for THIS auction only. Blank = the vendor's usual list.
    notify_emails = Column(Text, default="")

    # --- what it costs to get this bidder's goods to the door, for THIS
    #     auction. Copied from the vendor's defaults when they are invited,
    #     then frozen: they can only be changed while the auction is still
    #     editable, so a bid can never be re-ranked after it was placed.
    freight = Column(Float, default=0.0)
    freight_basis = Column(String(10), default="unit")     # unit | percent
    duty = Column(Float, default=0.0)
    duty_basis = Column(String(10), default="percent")
    packaging = Column(Float, default=0.0)
    packaging_basis = Column(String(10), default="unit")
    other = Column(Float, default=0.0)
    other_basis = Column(String(10), default="unit")
    other_label = Column(String(60), default="")

    auction = relationship("Auction", back_populates="participants")
    vendor = relationship("Vendor")


class Bid(Base):
    __tablename__ = "bids"
    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=False, index=True)
    line_id = Column(Integer, ForeignKey("auction_lines.id"), nullable=False, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    unit_price = Column(Float, nullable=False)
    qty = Column(Float, default=1.0)
    total = Column(Float, nullable=False)
    #: The delivered price this bid was ranked at - the bid plus this bidder's
    #: freight, duty and packaging as they stood when it was placed. Kept on
    #: the bid so the record of who led, and by how much, cannot drift.
    landed_unit_price = Column(Float)
    note = Column(String(400), default="")
    withdrawn = Column(Boolean, default=False, index=True)
    withdrawn_at = Column(DateTime)
    withdraw_reason = Column(String(400), default="")
    created_at = Column(DateTime, default=utcnow, index=True)

    auction = relationship("Auction", back_populates="bids")
    line = relationship("AuctionLine")
    vendor = relationship("Vendor")


class Award(Base):
    """One award row per line.

    House rule: a line is won outright by a single bidder, for the whole
    quantity. Different lines may go to different bidders, but a line is never
    carved up between two suppliers, so there is exactly one row per awarded
    line.
    """
    __tablename__ = "awards"
    __table_args__ = (UniqueConstraint("line_id", name="uq_award_line"),)
    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=False, index=True)
    line_id = Column(Integer, ForeignKey("auction_lines.id"), nullable=False)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False)
    bid_id = Column(Integer, ForeignKey("bids.id"), nullable=True)
    qty = Column(Float, nullable=False)
    unit_price = Column(Float, nullable=False)
    total = Column(Float, nullable=False)
    #: The delivered equivalent of the awarded price, where the auction was
    #: compared that way. What the business actually spends.
    landed_unit_price = Column(Float)
    landed_total = Column(Float)
    notes = Column(Text, default="")
    awarded_by_id = Column(Integer, ForeignKey("users.id"))
    awarded_at = Column(DateTime, default=utcnow)

    line = relationship("AuctionLine")
    vendor = relationship("Vendor")
    auction = relationship("Auction")
    #: The bid this award was based on, where one was picked from the board.
    #: Null when the buyer typed a price nobody had bid.
    bid = relationship("Bid")


class Message(Base):
    """Private thread between one bidder (vendor) and the auction creator."""
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=False, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    read_at = Column(DateTime)

    sender = relationship("User")
    vendor = relationship("Vendor")
    auction = relationship("Auction")


class Attachment(Base):
    """A document on an auction: a drawing, a spec, a compliance sheet.

    ``audience`` decides who may download it. A buyer's document is normally
    for the bidders; a bidder's document is only ever for the buyer, so one
    supplier can never see another's paperwork.
    """
    __tablename__ = "attachments"
    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=False, index=True)
    #: Which item it belongs to, if any. Kept by item rather than by auction
    #: line, because editing an auction rebuilds its lines and would otherwise
    #: orphan every drawing attached to them.
    item_id = Column(Integer, ForeignKey("items.id"), nullable=True)
    #: Set when a bidder uploaded it.
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True, index=True)
    audience = Column(String(10), default="bidders")   # bidders | buyer
    filename = Column(String(260), nullable=False)     # what the person called it
    stored_name = Column(String(120), nullable=False)  # what it is called on disk
    content_type = Column(String(120), default="application/octet-stream")
    size_bytes = Column(Integer, default=0)
    note = Column(String(300), default="")
    uploaded_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=utcnow, index=True)

    auction = relationship("Auction")
    item = relationship("Item")
    vendor = relationship("Vendor")
    uploaded_by = relationship("User")

    @property
    def size_label(self) -> str:
        size = float(self.size_bytes or 0)
        for unit in ("bytes", "KB", "MB"):
            if size < 1024 or unit == "MB":
                return f"{size:,.0f} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
            size /= 1024
        return f"{size:,.1f} MB"


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    event = Column(String(60), default="")
    title = Column(String(250), nullable=False)
    body = Column(Text, default="")
    link = Column(String(300), default="")
    read_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, index=True)


class EmailMessage(Base):
    __tablename__ = "email_messages"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organisations.id"), nullable=True, index=True)
    to_email = Column(String(250), nullable=False, index=True)
    to_name = Column(String(200), default="")
    subject = Column(String(400), nullable=False)
    html_body = Column(Text, default="")
    text_body = Column(Text, default="")
    event = Column(String(60), default="")
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=True)
    status = Column(String(30), default="queued")   # queued|sent|outbox|failed
    error = Column(Text, default="")
    file_path = Column(String(500), default="")
    created_at = Column(DateTime, default=utcnow, index=True)
    sent_at = Column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(60), index=True)
    entity_id = Column(Integer, index=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=True, index=True)
    action = Column(String(80), nullable=False)
    actor_id = Column(Integer, ForeignKey("users.id"))
    actor_label = Column(String(200), default="")
    detail = Column(Text, default="")
    ip = Column(String(60), default="")
    created_at = Column(DateTime, default=utcnow, index=True)

    actor = relationship("User")
