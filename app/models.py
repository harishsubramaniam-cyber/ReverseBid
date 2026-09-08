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
    APPROVER = "approver"  # approves / rejects / sends back for rework
    VENDOR = "vendor"      # bids


class AuctionStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    REWORK = "rework"
    REJECTED = "rejected"
    SCHEDULED = "scheduled"
    LIVE = "live"
    CLOSED = "closed"
    AWARDED = "awarded"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = (AuctionStatus.SCHEDULED, AuctionStatus.LIVE)


class DecrementType(str, enum.Enum):
    ABSOLUTE = "absolute"
    PERCENT = "percent"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REWORK = "rework"


# --------------------------------------------------------------------------- masters
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(200), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    password_hash = Column(String(300), nullable=False)
    role = Column(Enum(Role), nullable=False, default=Role.BUYER)
    phone = Column(String(40), default="")
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    is_active = Column(Boolean, default=True)
    onboarding_done = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)

    vendor = relationship("Vendor", back_populates="users", foreign_keys=[vendor_id])

    @property
    def is_vendor(self) -> bool:
        return self.role == Role.VENDOR

    @property
    def is_buyer_side(self) -> bool:
        return self.role in (Role.BUYER, Role.ADMIN, Role.APPROVER)


class Vendor(Base):
    """Vendor master. Only name + email are mandatory - deliberately lighter."""
    __tablename__ = "vendors"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)              # mandatory
    email = Column(String(200), nullable=False)             # mandatory
    code = Column(String(50), default="")
    contact_person = Column(String(200), default="")
    phone = Column(String(40), default="")
    address = Column(Text, default="")
    gstin = Column(String(40), default="")
    is_active = Column(Boolean, default=True)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=utcnow)

    users = relationship("User", back_populates="vendor",
                         foreign_keys="User.vendor_id")


class Unit(Base):
    """Unit of measure master. Only code is mandatory."""
    __tablename__ = "units"
    id = Column(Integer, primary_key=True)
    code = Column(String(30), unique=True, nullable=False)  # mandatory
    name = Column(String(120), default="")
    created_at = Column(DateTime, default=utcnow)


class Item(Base):
    """Item master. Only name is mandatory."""
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
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
    id = Column(Integer, primary_key=True)
    reference = Column(String(40), unique=True, index=True)
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

    auto_extend = Column(Boolean, default=True)
    extend_trigger_seconds = Column(Integer, default=120)
    extend_by_seconds = Column(Integer, default=180)
    max_extensions = Column(Integer, default=5)
    extensions_used = Column(Integer, default=0)

    requires_approval = Column(Boolean, default=False)

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
        return sum(l.qty * l.starting_price for l in self.lines)

    @property
    def is_live(self) -> bool:
        return self.status == AuctionStatus.LIVE

    @property
    def editable(self) -> bool:
        return self.status in (AuctionStatus.DRAFT, AuctionStatus.REWORK,
                               AuctionStatus.REJECTED, AuctionStatus.SCHEDULED)


class AuctionLine(Base):
    __tablename__ = "auction_lines"
    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    unit_id = Column(Integer, ForeignKey("units.id"), nullable=True)
    qty = Column(Float, default=1.0)
    starting_price = Column(Float, nullable=False)   # per-unit CEILING
    specification = Column(Text, default="")

    auction = relationship("Auction", back_populates="lines")
    item = relationship("Item")
    unit = relationship("Unit")

    @property
    def baseline(self) -> float:
        return self.qty * self.starting_price


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (UniqueConstraint("auction_id", "vendor_id"),)
    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=False)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False)
    invited_at = Column(DateTime, default=utcnow)
    alias = Column(String(30), default="")   # "Bidder A" when names are hidden

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
    note = Column(String(400), default="")
    withdrawn = Column(Boolean, default=False, index=True)
    withdrawn_at = Column(DateTime)
    withdraw_reason = Column(String(400), default="")
    created_at = Column(DateTime, default=utcnow, index=True)

    auction = relationship("Auction", back_populates="bids")
    line = relationship("AuctionLine")
    vendor = relationship("Vendor")


class Award(Base):
    """One award row per line per vendor - a line can be split across vendors."""
    __tablename__ = "awards"
    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=False, index=True)
    line_id = Column(Integer, ForeignKey("auction_lines.id"), nullable=False)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False)
    bid_id = Column(Integer, ForeignKey("bids.id"), nullable=True)
    qty = Column(Float, nullable=False)
    unit_price = Column(Float, nullable=False)
    total = Column(Float, nullable=False)
    notes = Column(Text, default="")
    awarded_by_id = Column(Integer, ForeignKey("users.id"))
    awarded_at = Column(DateTime, default=utcnow)

    line = relationship("AuctionLine")
    vendor = relationship("Vendor")
    auction = relationship("Auction")


class Approval(Base):
    __tablename__ = "approvals"
    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, ForeignKey("auctions.id"), nullable=False)
    requested_by_id = Column(Integer, ForeignKey("users.id"))
    approver_id = Column(Integer, ForeignKey("users.id"))
    status = Column(Enum(ApprovalStatus), default=ApprovalStatus.PENDING, index=True)
    comments = Column(Text, default="")
    requested_at = Column(DateTime, default=utcnow)
    acted_at = Column(DateTime)

    auction = relationship("Auction")
    approver = relationship("User", foreign_keys=[approver_id])
    requested_by = relationship("User", foreign_keys=[requested_by_id])


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
