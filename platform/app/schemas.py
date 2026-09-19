"""Request and response bodies for endpoints that are B2's own.

Split from `app/contracts.py` on purpose. That file is a *negotiated*
contract — three slices agree on it and a parity test enforces the
agreement. This file is not negotiated: it is the shape of B2's own doors,
and changing anything in it needs nobody's permission but the caller's.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.contracts import Channel, Listing, OrderStatus


# -- auth -------------------------------------------------------------------


class OtpRequest(BaseModel):
    phone: str
    name: str | None = None
    language: str = "hi"


class OtpIssued(BaseModel):
    challenge_id: str
    expires_at: datetime
    #: Only populated when CRAFTLY_OTP_ECHO is on. There is no SMS gateway
    #: in this repo — C2 owns delivery — and a login you cannot complete is
    #: not a login. It must be off before this is public.
    dev_code: str | None = None
    delivery: str = Field("echo", description="How the code reached the artisan.")


class OtpVerify(BaseModel):
    challenge_id: str
    code: str


class BuyerRegister(BaseModel):
    email: str
    password: str = Field(min_length=8)
    name: str
    phone: str | None = None


class BuyerLogin(BaseModel):
    email: str
    password: str


class TokenIssued(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    account_id: str
    role: str
    artisan_id: str | None = None
    language: str | None = None


class AccountOut(BaseModel):
    account_id: str
    role: str
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    artisan_id: str | None = None
    language: str
    ai_call_consent: bool


class AccountUpdate(BaseModel):
    name: str | None = None
    language: str | None = None
    ai_call_consent: bool | None = None
    payout_upi: str | None = None
    payout_account_name: str | None = None


# -- listings ---------------------------------------------------------------


class InventoryIn(BaseModel):
    in_stock: int | None = None
    made_to_order: bool | None = None
    lead_time_days: int | None = None
    monthly_capacity: int | None = None
    cluster_capacity: int | None = None


class ListingIn(BaseModel):
    """What A2 (or A1's offline queue) posts after a capture.

    `listing` is the full shared contract. Everything else is B2's own
    bookkeeping that the contract has no field for.

    `artisan_id` on the inner listing is ignored in favour of the token's
    artisan: a phone can only create listings for the person holding it,
    and trusting a client-supplied owner id is how one artisan's work ends
    up under another's name.
    """

    listing: Listing
    inventory: InventoryIn | None = None
    making_clip_url: str | None = None
    channels: list[Channel] | None = None
    #: Drafts are the default. A listing goes live when the artisan has
    #: heard the readback and confirmed it, which is A1's confirmation loop.
    publish: bool = False


class ListingPatch(BaseModel):
    """A correction from the confirmation loop, or a publish toggle."""

    title_en: str | None = None
    title_hi: str | None = None
    description_en: str | None = None
    description_hi: str | None = None
    category: str | None = None
    craft_type: str | None = None
    material: str | None = None
    colours: list[str] | None = None
    dimensions: str | None = None
    material_cost_inr: int | None = None
    hours_worked: float | None = None
    image_original_url: str | None = None
    image_clean_url: str | None = None
    making_clip_url: str | None = None
    channels: list[Channel] | None = None
    published: bool | None = None
    inventory: InventoryIn | None = None


class ListingOut(BaseModel):
    listing: Listing
    published: bool
    channels: list[Channel]
    verification_code: str | None = None
    passport_url: str | None = None
    qr_url: str | None = None
    making_clip_url: str | None = None


# -- media ------------------------------------------------------------------


class MediaOut(BaseModel):
    media_id: str
    url: str
    absolute_url: str
    sha256: str
    content_type: str
    kind: str
    bytes: int
    #: True when these exact bytes were already stored. A1's offline queue
    #: retries uploads, and knowing a retry was a no-op is the difference
    #: between a sync log that makes sense and one that looks like it is
    #: duplicating an artisan's work.
    deduplicated: bool = False


# -- orders -----------------------------------------------------------------


class StatusChange(BaseModel):
    status: OrderStatus
    note: str | None = None


class PayoutOut(BaseModel):
    payout_id: str
    order_id: str
    artisan_id: str
    artisan_name: str | None = None
    listing_title: str | None = None
    quantity: int
    unit_take_home_inr: int
    amount_inr: int
    status: str
    blocked_reason: str | None = None
    created_at: datetime
    paid_at: datetime | None = None


class LineSplitOut(BaseModel):
    listing_id: str
    title: str
    quantity: int
    gross_inr: int
    artisan_total_inr: int
    platform_fee_inr: int
    platform_absorbed_inr: int
    problems: list[str] = Field(default_factory=list)


class SettlementOut(BaseModel):
    order_id: str
    status: OrderStatus
    gross_inr: int
    artisan_total_inr: int
    platform_fee_inr: int
    platform_absorbed_inr: int
    artisan_share_pct: float | None = None
    settled: bool = False
    complete: bool = True
    lines: list[LineSplitOut] = Field(default_factory=list)
    payouts: list[PayoutOut] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: str
    database: str
    listings: int | None = None
    published: int | None = None
    artisans: int | None = None
    orders: int | None = None
    warnings: list[str] = Field(default_factory=list)
