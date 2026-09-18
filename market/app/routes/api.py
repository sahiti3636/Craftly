"""The same surfaces as JSON.

Two consumers, and a third that matters later:

* **A1's mobile app** needs the buyer catalogue and passport data without
  scraping HTML — an artisan browsing what her cluster has listed, or the
  fulfilment view resolving an order line.
* **C2's marketplace adapters** need the priced, channel-aware listing to
  push to Amazon or Flipkart, and that is exactly what
  `/api/products/{id}?channel=amazon` returns.
* Anyone who later replaces these server-rendered pages with a
  single-page app can do it against this, without touching the logic in
  `search.py`, `bulk.py` or `view.py`. That is the point of the pages
  being thin.

Responses are the contracts in `app/contracts.py`, with a small view
wrapper where a screen needs more than a contract carries.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app import bulk, deps
from app.adapters import registry
from app.contracts import Channel, Inventory, Listing, Passport, PriceQuote, SplitPlan
from app.search import SearchQuery, Sort, run
from app.view import ProductCard

router = APIRouter(prefix="/api")


class ProductOut(BaseModel):
    """A listing as a buyer surface sees it: priced, placed, sellable or not."""

    listing: Listing
    inventory: Inventory
    price: PriceQuote
    artisan_id: str
    artisan_name: str
    place: str | None = None
    image_url: str | None = None
    can_buy: bool
    blocked_reason: str | None = None
    artisan_share_pct: int
    passport_code: str | None = None

    @classmethod
    def of(cls, card: ProductCard, passport_code: str | None = None) -> "ProductOut":
        return cls(
            listing=card.listing,
            inventory=card.inventory,
            price=card.quote,
            artisan_id=card.artisan_id,
            artisan_name=card.artisan_name,
            place=card.place,
            image_url=card.image_url,
            can_buy=card.sellability.can_buy,
            blocked_reason=card.sellability.detail or card.sellability.reason,
            artisan_share_pct=card.artisan_share_pct,
            passport_code=passport_code,
        )


class ProductPage(BaseModel):
    total: int
    page: int
    per_page: int
    products: list[ProductOut]


class BulkQuoteOut(BaseModel):
    listing_id: str
    quantity: int
    deadline: date | None = None
    unit_price_inr: int
    retail_unit_price_inr: int
    subtotal_inr: int
    artisan_take_home_inr: int
    artisan_total_inr: int
    can_order: bool
    split: SplitPlan
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    max_by_deadline: int | None = None
    days_needed: int | None = None


@router.get("/products", response_model=ProductPage)
def products(
    q: str = "",
    category: str | None = None,
    craft_type: str | None = None,
    material: str | None = None,
    colour: str | None = None,
    state: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    in_stock: bool = False,
    buyable_only: bool = False,
    channel: Channel = Channel.OWN_STORE,
    sort: Sort = Sort.RELEVANCE,
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=100),
) -> ProductPage:
    result = run(
        deps.cards(channel),
        SearchQuery(
            q=q,
            category=category,
            craft_type=craft_type,
            material=material,
            colour=colour,
            state=state,
            min_price_inr=min_price,
            max_price_inr=max_price,
            in_stock_only=in_stock,
            buyable_only=buyable_only,
            sort=sort,
            page=page,
            per_page=per_page,
        ),
    )
    return ProductPage(
        total=result.total,
        page=result.page,
        per_page=result.per_page,
        products=[ProductOut.of(card) for card in result.cards],
    )


@router.get("/products/{listing_id}", response_model=ProductOut)
def product(listing_id: str, channel: Channel = Channel.OWN_STORE) -> ProductOut:
    card = deps.card(listing_id, channel)
    if card is None:
        raise HTTPException(status_code=404, detail="No such listing")
    passport = registry.passports().by_listing(listing_id)
    return ProductOut.of(card, passport.verification_code if passport else None)


@router.get("/passports/{code}", response_model=Passport)
def passport(code: str) -> Passport:
    found = registry.passports().by_code(code)
    if found is None:
        raise HTTPException(status_code=404, detail="No passport with that code")
    return found


@router.get("/bulk/quote", response_model=BulkQuoteOut)
def bulk_quote(
    listing_id: str,
    quantity: int = Query(..., ge=1),
    deadline: date | None = None,
) -> BulkQuoteOut:
    entry = registry.catalog().get_entry(listing_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such listing")

    quoted = bulk.quote(
        entry,
        quantity=quantity,
        prices=registry.prices(),
        engine=registry.order_engine(),
        deadline=deadline,
    )
    return BulkQuoteOut(
        listing_id=listing_id,
        quantity=quantity,
        deadline=deadline,
        unit_price_inr=quoted.unit_price_inr,
        retail_unit_price_inr=quoted.retail_unit_price_inr,
        subtotal_inr=quoted.subtotal_inr,
        artisan_take_home_inr=quoted.artisan_take_home_inr,
        artisan_total_inr=quoted.artisan_total_inr,
        can_order=quoted.can_order,
        split=quoted.split,
        errors=quoted.errors,
        warnings=quoted.warnings,
        max_by_deadline=quoted.max_by_deadline,
        days_needed=quoted.days_needed,
    )
