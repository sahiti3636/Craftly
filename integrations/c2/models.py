"""Types C2 reads from C1 and the records C2 produces.

The order types are a *read-only mirror* of `market/app/contracts.py`
(`Order`, `OrderLine`, `Buyer`, `SplitPlan`), trimmed to the fields C2
uses and set to ignore anything else. They are duplicated rather than
imported for the same reason C1 duplicates `Listing`: both packages would
otherwise fight over the top-level name `app`. `extra="ignore"` means C1
adding a field never breaks C2; C1 renaming one of these does, loudly, in
`tests/test_orders.py`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Mirror(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Read from C1
# ---------------------------------------------------------------------------


class Buyer(_Mirror):
    name: str
    phone: str
    email: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    country: str = "IN"


class Allocation(_Mirror):
    artisan_id: str
    artisan_name: str
    village: str | None = None
    quantity: int
    lead_time_days: int | None = None
    take_home_inr: int | None = None  # per unit


class SplitPlan(_Mirror):
    allocations: list[Allocation] = Field(default_factory=list)
    feasible: bool = True


class OrderLine(_Mirror):
    listing_id: str
    title: str
    quantity: int
    unit_price_inr: int
    channel: str = "own_store"
    artisan_take_home_inr: int | None = None  # per unit
    split: SplitPlan | None = None


class Order(_Mirror):
    order_id: str
    kind: str = "retail"
    status: str = "placed"
    buyer: Buyer
    lines: list[OrderLine] = Field(default_factory=list)
    channel: str = "own_store"
    deadline: datetime | None = None
    notes: str | None = None
    source: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total_inr(self) -> int:
        return sum(l.unit_price_inr * l.quantity for l in self.lines)

    @property
    def unit_count(self) -> int:
        return sum(l.quantity for l in self.lines)


# ---------------------------------------------------------------------------
# Produced by C2
# ---------------------------------------------------------------------------


class ChannelPush(BaseModel):
    """What a marketplace adapter would send, and the marketplace's reply."""

    channel: str
    listing_id: str
    simulated: bool = True
    price_source: str = Field(
        ..., description="Where the priced listing came from: c1_http, c1_inprocess or seed_fallback."
    )
    request: dict[str, Any]
    response: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class PickupStop(BaseModel):
    """One pickup: a bulk order pooled across a cluster has several."""

    artisan_id: str
    artisan_name: str
    village: str | None = None
    state: str | None = None
    quantity: int
    waybill: str


class TrackingEvent(BaseModel):
    status: str
    detail: str
    at: datetime


class Shipment(BaseModel):
    order_id: str
    simulated: bool = True
    courier: str
    service: str
    master_waybill: str
    pickup_date: date
    pickup_window: str
    pickups: list[PickupStop]
    deliver_to: str
    est_delivery: date
    tracking: list[TrackingEvent] = Field(default_factory=list)


class Payout(BaseModel):
    artisan_id: str
    artisan_name: str
    village: str | None = None
    state: str | None = None
    languages: list[str] = Field(default_factory=list)
    quantity: int
    titles_en: list[str] = Field(default_factory=list)
    titles_hi: list[str] = Field(default_factory=list)
    amount_inr: int | None = Field(
        None,
        description="None when C1 did not carry a take-home figure. Missing is not zero.",
    )


class CallLog(BaseModel):
    call_id: str
    simulated: bool = True
    event: str
    order_id: str
    artisan_id: str
    artisan_name: str
    to_phone: str
    language: str
    script: str
    script_en: str
    duration_sec: int
    outcome: str
    at: datetime


class DemandItem(BaseModel):
    listing_id: str
    title: str
    craft_type: str | None = None
    artisan_name: str | None = None
    units_ordered: int
    orders: int
    in_stock: int | None = None
    lead_time_days: int | None = None
    action: str


class DemandAlert(BaseModel):
    simulated: bool = False
    window_start: date
    window_end: date
    orders_counted: int
    orders_source: str
    items: list[DemandItem]
    message_en: str
    message_hi: str
