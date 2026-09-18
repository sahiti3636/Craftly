"""Types that cross slice boundaries.

Three kinds of thing live here:

1. ``Listing`` — a mirror of the shared contract owned by A2 in
   ``capture/app/schema.py``. It is duplicated, not imported, because both
   packages are called ``app`` and neither is installable, so
   ``from app.schema import Listing`` inside ``market/`` would resolve to
   the wrong module. ``tests/test_schema_parity.py`` loads A2's file
   directly and fails if the two drift, so the duplication cannot rot
   silently. **Do not edit the mirror to fix a parity failure** — a schema
   change is a team-wide announcement, and the parity test failing means
   the announcement happened somewhere C1 did not hear it.

2. Types C1 needs but does not own — ``PriceQuote``, ``SplitPlan``,
   ``Passport``, ``Artisan``, ``Inventory``. B1 and B2 produce these; C1
   only renders them. They are written down here because C1 had to start
   before those slices existed: this file is C1's proposal for what the
   real services should return, and ``app/adapters/stub_*.py`` are
   throwaway implementations of exactly this shape. When B1 and B2 land,
   the negotiation is over these classes, and only ``app/adapters/``
   should have to change.

3. Types C1 produces and hands on — ``Order``, ``OrderLine``, ``Buyer``.
   An order leaving a buyer surface is the input to B1's splitter and B2's
   payment split.

The null rule from the Listing contract applies throughout: a missing
number is ``None``, never ``0``. ``hours_worked = 0.0`` would mean an item
took no work, which would drag a wage floor to nothing; ``None`` means
nobody has asked the artisan yet, and the surface must say so rather than
quietly price the item as if the work were free.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 1. Mirror of the shared Listing contract
#    (source of truth: capture/app/schema.py)
# ---------------------------------------------------------------------------


class Listing(BaseModel):
    listing_id: str
    artisan_id: str

    source_language: str | None = None
    transcript_raw: str | None = None

    title_en: str | None = None
    title_hi: str | None = None
    description_en: str | None = None
    description_hi: str | None = None

    summary_spoken: str | None = None

    category: str | None = None
    craft_type: str | None = None
    material: str | None = None
    colours: list[str] | None = None
    dimensions: str | None = None

    material_cost_inr: int | None = None
    hours_worked: float | None = None

    confidence: dict[str, float] = Field(default_factory=dict)
    needs_confirmation: list[str] = Field(default_factory=list)

    image_original_url: str | None = None
    image_clean_url: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 2. Owned by other slices, rendered by C1
# ---------------------------------------------------------------------------


class Channel(str, Enum):
    """Where a buyer is standing when they see a price.

    The same item carries different prices on different channels because
    marketplaces take different cuts, and B1's rule is that the artisan's
    take-home is the constant. A buyer surface therefore must never show a
    bare "price" without knowing which channel it is quoting.
    """

    OWN_STORE = "own_store"
    B2B = "b2b"
    MELA_QR = "mela_qr"
    AMAZON = "amazon"
    FLIPKART = "flipkart"
    EBAY = "ebay"


class Artisan(BaseModel):
    """Who made it. B2 owns this record."""

    artisan_id: str
    name: str
    village: str | None = None
    district: str | None = None
    state: str | None = None
    craft: str | None = None
    cluster_id: str | None = Field(
        None,
        description=(
            "The self-help group or cluster this artisan pools capacity with. "
            "A bulk order is split across members of one cluster."
        ),
    )
    years_experience: int | None = None
    languages: list[str] = Field(default_factory=list)
    photo_url: str | None = None
    story: str | None = None


class Inventory(BaseModel):
    """What can actually be supplied, and how fast. B2 owns this record."""

    listing_id: str
    in_stock: int = 0
    made_to_order: bool = True
    lead_time_days: int | None = Field(
        None, description="Days to make one more unit. None means nobody has asked."
    )
    monthly_capacity: int | None = Field(
        None, description="Units this one artisan can produce in a month."
    )
    cluster_capacity: int | None = Field(
        None, description="Units the whole cluster can produce in a month."
    )

    @property
    def sellable_now(self) -> int:
        return max(self.in_stock, 0)


class PriceQuote(BaseModel):
    """What B1's price engine returns for one listing on one channel.

    ``floor_inr`` is the load-bearing field. It is material cost plus hours
    at the state minimum wage plus a wastage allowance, and the hard rule
    is that nothing publishes below it. A buyer surface that renders a
    price without rendering where the floor sits is hiding the one number
    this whole project exists to defend.
    """

    listing_id: str
    channel: Channel = Channel.OWN_STORE

    floor_inr: int
    market_low_inr: int | None = None
    market_high_inr: int | None = None
    passport_premium_inr: int = 0

    price_inr: int = Field(..., description="What the buyer pays on this channel.")
    artisan_take_home_inr: int = Field(
        ..., description="What reaches the artisan after this channel's cut."
    )
    channel_fee_inr: int = 0

    below_floor: bool = Field(
        False,
        description=(
            "True when the comparable market band sits under the wage floor. "
            "The honest failure path: the item is still offered at the floor, and "
            "the surface says the market underpays this craft rather than "
            "silently cutting the price to match."
        ),
    )
    floor_incomplete: bool = Field(
        False,
        description=(
            "True when material_cost_inr or hours_worked was null, so the floor is "
            "a guess and the listing must not be sold until the artisan is asked."
        ),
    )
    explanation: list[str] = Field(
        default_factory=list,
        description="Plain-language lines for the buyer-facing price breakdown.",
    )


class Allocation(BaseModel):
    """One artisan's share of a split bulk order. B1's order engine owns this."""

    artisan_id: str
    artisan_name: str
    village: str | None = None
    quantity: int
    lead_time_days: int | None = None
    take_home_inr: int | None = Field(
        None, description="Per-unit take-home for this artisan, not the line total."
    )


class SplitPlan(BaseModel):
    """How a bulk order is spread across a cluster, and whether it is possible."""

    listing_id: str
    quantity_requested: int
    quantity_allocated: int
    allocations: list[Allocation] = Field(default_factory=list)
    lead_time_days: int | None = None
    feasible: bool = True
    shortfall: int = 0
    notes: list[str] = Field(default_factory=list)


class PassportStep(BaseModel):
    """One verifiable step in how the object came to exist."""

    label: str
    detail: str | None = None
    at: datetime | None = None


class Passport(BaseModel):
    """The craft passport: proof that a human made this, and which human.

    B2 assembles it and mints the QR. C1 renders it at ``/p/{code}`` — the
    page someone reaches by scanning the tag on an object they are holding
    at a mela, which is the only surface in this system a buyer meets
    before they have met the storefront.
    """

    listing_id: str
    verification_code: str = Field(
        ..., description="Short human-readable code printed beside the QR."
    )
    artisan: Artisan
    craft_type: str | None = None
    material: str | None = None
    hours_worked: float | None = None
    material_cost_inr: int | None = None
    made_at: str | None = Field(None, description="Village or workshop where it was made.")
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    making_clip_url: str | None = None
    image_url: str | None = None
    qr_url: str | None = Field(
        None, description="URL of the QR image B2 minted for this passport."
    )
    chain: list[PassportStep] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 3. Produced by C1, consumed by B1 (splitting) and B2 (payment, fulfilment)
# ---------------------------------------------------------------------------


class OrderKind(str, Enum):
    RETAIL = "retail"
    BULK = "bulk"


class OrderStatus(str, Enum):
    PLACED = "placed"
    ACCEPTED = "accepted"
    IN_PRODUCTION = "in_production"
    DISPATCHED = "dispatched"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class Buyer(BaseModel):
    name: str
    phone: str
    email: str | None = None
    organisation: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    country: str = "IN"


class OrderLine(BaseModel):
    listing_id: str
    title: str
    quantity: int
    unit_price_inr: int
    channel: Channel = Channel.OWN_STORE
    artisan_take_home_inr: int | None = Field(
        None, description="Per-unit take-home, not the line total."
    )
    split: SplitPlan | None = Field(
        None,
        description="Set on bulk lines: how B1 proposes to spread this line across a cluster.",
    )

    @property
    def subtotal_inr(self) -> int:
        return self.unit_price_inr * self.quantity


class Order(BaseModel):
    order_id: str
    kind: OrderKind = OrderKind.RETAIL
    status: OrderStatus = OrderStatus.PLACED
    buyer: Buyer
    lines: list[OrderLine] = Field(default_factory=list)
    channel: Channel = Channel.OWN_STORE
    deadline: datetime | None = Field(
        None, description="B2B only: the date the buyer needs delivery by."
    )
    notes: str | None = None
    source: str | None = Field(
        None, description="Which surface placed it: 'storefront', 'b2b', 'qr_reorder'."
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total_inr(self) -> int:
        return sum(line.subtotal_inr for line in self.lines)

    @property
    def artisan_total_inr(self) -> int:
        return sum((line.artisan_take_home_inr or 0) * line.quantity for line in self.lines)

    @property
    def unit_count(self) -> int:
        return sum(line.quantity for line in self.lines)
