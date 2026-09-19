"""The database schema.

This is the join every other slice has been faking. A2 produces listings
into an in-memory dict that dies with the process; C1 reads three JSON
files; B1 is handed a product on each request and remembers nothing. All
three of those stop being true here.

Shape of it::

    clusters --< artisans --< listings --1 inventory
                     |            |
                     |            +--1 passports --< passport_steps
                     |            +--< order_lines >-- orders
                     |                     |
                     |                     +--< allocations
                     +--< payouts >--------+
    accounts --< tokens, otp_challenges
    media

Three rules the columns enforce, rather than the application enforcing them:

* **Nullable means unknown.** ``material_cost_inr`` and ``hours_worked``
  are nullable with no default. A ``DEFAULT 0`` on either would turn "we
  have not asked her yet" into "it cost nothing and took no time", which
  is how a wage floor collapses to zero without anyone noticing.
* **Money is whole rupees, stored as integers.** A float rupee drifts a
  payout split by a rupee or two per line and nobody can say why.
* **A payout points at an order line and an artisan, not at an order.**
  A bulk line is split across a cluster, so "who gets paid for this order"
  has no single answer and the schema refuses to pretend otherwise.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# ---------------------------------------------------------------------------
# Who
# ---------------------------------------------------------------------------


class Cluster(Base, TimestampMixin):
    """A self-help group or craft cluster. Bulk orders split across members."""

    __tablename__ = "clusters"

    cluster_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200))
    district: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))

    artisans: Mapped[list[Artisan]] = relationship(back_populates="cluster")


class Artisan(Base, TimestampMixin):
    __tablename__ = "artisans"

    artisan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    village: Mapped[str | None] = mapped_column(String(120))
    district: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    craft: Mapped[str | None] = mapped_column(String(200))
    cluster_id: Mapped[str | None] = mapped_column(
        ForeignKey("clusters.cluster_id", ondelete="SET NULL"), index=True
    )
    years_experience: Mapped[int | None] = mapped_column(Integer)
    languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    photo_url: Mapped[str | None] = mapped_column(String(500))
    story: Mapped[str | None] = mapped_column(Text)

    #: Where money owed to this artisan is sent. Nullable because an artisan
    #: can list and sell before her bank details are collected. The payout
    #: then sits as "blocked" with a reason, which is a state a human can
    #: act on. Silently dropping the payout is not.
    payout_upi: Mapped[str | None] = mapped_column(String(120))
    payout_account_name: Mapped[str | None] = mapped_column(String(200))

    cluster: Mapped[Cluster | None] = relationship(back_populates="artisans")
    listings: Mapped[list[Listing]] = relationship(back_populates="artisan")


# ---------------------------------------------------------------------------
# What
# ---------------------------------------------------------------------------


class Listing(Base):
    """The shared contract, persisted. Mirrors capture/app/schema.py."""

    __tablename__ = "listings"

    listing_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artisan_id: Mapped[str] = mapped_column(
        ForeignKey("artisans.artisan_id", ondelete="CASCADE"), index=True
    )

    source_language: Mapped[str | None] = mapped_column(String(16))
    transcript_raw: Mapped[str | None] = mapped_column(Text)

    title_en: Mapped[str | None] = mapped_column(String(300))
    title_hi: Mapped[str | None] = mapped_column(String(300))
    description_en: Mapped[str | None] = mapped_column(Text)
    description_hi: Mapped[str | None] = mapped_column(Text)
    summary_spoken: Mapped[str | None] = mapped_column(Text)

    category: Mapped[str | None] = mapped_column(String(120), index=True)
    craft_type: Mapped[str | None] = mapped_column(String(160), index=True)
    material: Mapped[str | None] = mapped_column(String(160))
    colours: Mapped[list[str] | None] = mapped_column(JSON)
    dimensions: Mapped[str | None] = mapped_column(String(200))

    # No server_default. Null is "ask the artisan"; 0 is "she said none".
    material_cost_inr: Mapped[int | None] = mapped_column(Integer)
    hours_worked: Mapped[float | None] = mapped_column(Float)

    confidence: Mapped[dict] = mapped_column(JSON, default=dict)
    needs_confirmation: Mapped[list[str]] = mapped_column(JSON, default=list)

    image_original_url: Mapped[str | None] = mapped_column(String(500))
    image_clean_url: Mapped[str | None] = mapped_column(String(500))
    making_clip_url: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    #: A listing is a draft until the artisan confirms it. Unpublished
    #: listings are invisible to every buyer surface.
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    channels: Mapped[list[str]] = mapped_column(JSON, default=list)

    artisan: Mapped[Artisan] = relationship(back_populates="listings")
    inventory: Mapped[Inventory | None] = relationship(
        back_populates="listing", uselist=False, cascade="all, delete-orphan"
    )
    passport: Mapped[Passport | None] = relationship(
        back_populates="listing", uselist=False, cascade="all, delete-orphan"
    )


class Inventory(Base, TimestampMixin):
    __tablename__ = "inventory"

    listing_id: Mapped[str] = mapped_column(
        ForeignKey("listings.listing_id", ondelete="CASCADE"), primary_key=True
    )
    in_stock: Mapped[int] = mapped_column(Integer, default=0)
    made_to_order: Mapped[bool] = mapped_column(Boolean, default=True)
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    monthly_capacity: Mapped[int | None] = mapped_column(Integer)
    cluster_capacity: Mapped[int | None] = mapped_column(Integer)

    listing: Mapped[Listing] = relationship(back_populates="inventory")


# ---------------------------------------------------------------------------
# Proof
# ---------------------------------------------------------------------------


class Passport(Base):
    __tablename__ = "passports"

    listing_id: Mapped[str] = mapped_column(
        ForeignKey("listings.listing_id", ondelete="CASCADE"), primary_key=True
    )
    #: Stored, not recomputed: a printed tag outlives the listing that
    #: minted it. See app/ids.py.
    verification_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    #: Dashes stripped, upper-cased. Lookup happens on this column so a
    #: buyer who types the code without dashes still gets an answer.
    code_key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    listing: Mapped[Listing] = relationship(back_populates="passport")
    steps: Mapped[list[PassportStep]] = relationship(
        back_populates="passport",
        cascade="all, delete-orphan",
        order_by="PassportStep.position",
    )


class PassportStep(Base):
    """One verifiable step in how the object came to exist.

    Persisted rather than derived, because the chain records things that
    happened at particular times: when the voice note was catalogued, when
    the floor was checked. A chain regenerated on read would quietly
    rewrite its own history every time the wording changed.
    """

    __tablename__ = "passport_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[str] = mapped_column(
        ForeignKey("passports.listing_id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    passport: Mapped[Passport] = relationship(back_populates="steps")


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


class Media(Base):
    """One stored file. Content-addressed. See app/media.py."""

    __tablename__ = "media"

    media_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(120))
    bytes: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32), default="image")
    listing_id: Mapped[str | None] = mapped_column(
        ForeignKey("listings.listing_id", ondelete="SET NULL"), index=True
    )
    artisan_id: Mapped[str | None] = mapped_column(
        ForeignKey("artisans.artisan_id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="retail")
    status: Mapped[str] = mapped_column(String(32), default="placed", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="own_store")

    buyer_name: Mapped[str] = mapped_column(String(200))
    buyer_phone: Mapped[str] = mapped_column(String(40), index=True)
    buyer_email: Mapped[str | None] = mapped_column(String(200))
    buyer_organisation: Mapped[str | None] = mapped_column(String(200))
    buyer_address: Mapped[str | None] = mapped_column(Text)
    buyer_city: Mapped[str | None] = mapped_column(String(120))
    buyer_state: Mapped[str | None] = mapped_column(String(120))
    buyer_pincode: Mapped[str | None] = mapped_column(String(16))
    buyer_country: Mapped[str] = mapped_column(String(8), default="IN")

    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="SET NULL"), index=True
    )

    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderLine.position"
    )
    events: Mapped[list[OrderEvent]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderEvent.id"
    )
    payouts: Mapped[list[Payout]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderLine(Base):
    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.order_id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    listing_id: Mapped[str] = mapped_column(
        ForeignKey("listings.listing_id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[int] = mapped_column(Integer)

    #: B1's numbers, stored verbatim as quoted to the buyer. This service
    #: never recalculates them: a platform that recomputes a price can
    #: disagree with the receipt, and then there are two truths.
    unit_price_inr: Mapped[int] = mapped_column(Integer)
    artisan_take_home_inr: Mapped[int | None] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(32), default="own_store")

    #: B1's whole SplitPlan as sent, kept for audit. The rows money is
    #: actually paid against are in `allocations`.
    split_plan: Mapped[dict | None] = mapped_column(JSON)

    order: Mapped[Order] = relationship(back_populates="lines")
    allocations: Mapped[list[Allocation]] = relationship(
        back_populates="line", cascade="all, delete-orphan"
    )


class Allocation(Base):
    """One artisan's share of one line. The row a payout is computed from."""

    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    line_id: Mapped[int] = mapped_column(
        ForeignKey("order_lines.id", ondelete="CASCADE"), index=True
    )
    artisan_id: Mapped[str] = mapped_column(
        ForeignKey("artisans.artisan_id", ondelete="RESTRICT"), index=True
    )
    artisan_name: Mapped[str] = mapped_column(String(200))
    village: Mapped[str | None] = mapped_column(String(120))
    quantity: Mapped[int] = mapped_column(Integer)
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    take_home_inr: Mapped[int | None] = mapped_column(Integer)

    line: Mapped[OrderLine] = relationship(back_populates="allocations")


class OrderEvent(Base):
    """Append-only status history.

    The current status is derivable from it, and a dispute is arguable
    from it.
    """

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.order_id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    order: Mapped[Order] = relationship(back_populates="events")


class Payout(Base):
    """Money owed to one artisan for one line of one order.

    Written once, when the order is delivered, and never recomputed. The
    unique constraint is the idempotency guarantee: a delivery webhook that
    fires twice cannot pay an artisan twice, and cannot pay her nothing the
    second time either.
    """

    __tablename__ = "payouts"
    __table_args__ = (UniqueConstraint("order_id", "line_id", "artisan_id", name="uq_payout"),)

    payout_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.order_id", ondelete="CASCADE"), index=True
    )
    line_id: Mapped[int] = mapped_column(
        ForeignKey("order_lines.id", ondelete="CASCADE"), index=True
    )
    artisan_id: Mapped[str] = mapped_column(
        ForeignKey("artisans.artisan_id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    unit_take_home_inr: Mapped[int] = mapped_column(Integer)
    amount_inr: Mapped[int] = mapped_column(Integer)
    #: "pending" until a real payment rail exists; "blocked" with a reason
    #: when we know what is owed but not where to send it.
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(300))
    destination: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="payouts")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class Account(Base, TimestampMixin):
    """An artisan or a buyer who can log in.

    Separate from `artisans` because the two answer different questions.
    An Artisan row is *who made this*, which a passport renders and which
    must exist for a seeded artisan who has never opened the app. An
    Account row is *who is holding this phone*. Merging them would mean
    either inventing credentials for every seeded artisan or refusing to
    name a maker who has not logged in yet.
    """

    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), index=True)  # artisan | buyer | service
    phone: Mapped[str | None] = mapped_column(String(40), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str | None] = mapped_column(String(300))
    artisan_id: Mapped[str | None] = mapped_column(
        ForeignKey("artisans.artisan_id", ondelete="SET NULL"), index=True
    )
    #: The artisan's chosen UI language. Stored here rather than on the
    #: Artisan row because it is a property of the person using the phone.
    language: Mapped[str] = mapped_column(String(16), default="hi")
    #: A1's onboarding consent toggle for C2's AI voice calls. Off unless
    #: she said yes: a system that rings a woman's phone by default has
    #: decided on her behalf that it is welcome to.
    ai_call_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)

    tokens: Mapped[list[Token]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Token(Base):
    """A bearer token, stored as a hash.

    Only the hash is kept. If this database leaks, the tokens in it are not
    usable, which is the entire reason to store a hash of something the
    client has already been shown once.
    """

    __tablename__ = "tokens"

    token_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="CASCADE"), index=True
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label: Mapped[str | None] = mapped_column(String(120))

    account: Mapped[Account] = relationship(back_populates="tokens")


class OtpChallenge(Base):
    """A one-time code sent to an artisan's phone.

    Hashed like a password, attempt-limited, and single-use. An artisan who
    cannot read is still perfectly able to hear a six-digit code and tap
    it, which is why this is the artisan login rather than a password.
    """

    __tablename__ = "otp_challenges"

    challenge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    phone: Mapped[str] = mapped_column(String(40), index=True)
    code_hash: Mapped[str] = mapped_column(String(300))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
