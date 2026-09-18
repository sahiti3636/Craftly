"""View models: what a template is allowed to see.

A template gets a `ProductCard` or a `ProductView`, never a raw
`CatalogEntry` plus a `PriceQuote` plus a `Passport` and instructions to
combine them. Three reasons that matters here:

* **The sale rule lives in one place.** Whether an item can be bought is
  decided once, in `_sellability`, and every surface — storefront, B2B,
  QR reorder — asks the same object. A rule enforced in three templates
  is a rule enforced in two.

* **Bilingual fallback is not a template's job.** A2 fills `title_hi` when
  it can and leaves it null when it cannot. `title(lang)` resolves that
  once, rather than every template growing `{{ p.title_hi or p.title_en }}`.

* **Nulls become sentences.** `hours_worked = None` renders as "still to
  be confirmed", not as a blank cell that reads like zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.contracts import Channel, Inventory, Listing, Passport, PriceQuote
from app.money import rupees
from app.ports import CatalogEntry


def _text(primary: str | None, fallback: str | None) -> str | None:
    return primary if (primary and primary.strip()) else fallback


@dataclass
class Sellability:
    """Can a buyer put this in a basket, and if not, why not."""

    can_buy: bool
    reason: str | None = None
    detail: str | None = None

    #: True when the blocker is on the artisan's side and a buyer could
    #: usefully be shown "tell us you want this" instead of a dead end.
    can_request: bool = False


@dataclass
class ProductCard:
    """One item on a shelf."""

    listing_id: str
    listing: Listing
    inventory: Inventory
    quote: PriceQuote

    artisan_name: str
    artisan_id: str
    village: str | None
    district: str | None
    state: str | None
    craft: str | None
    cluster_id: str | None

    image_url: str | None
    sellability: Sellability
    channels: tuple[Channel, ...] = (Channel.OWN_STORE,)

    # -- text ------------------------------------------------------------

    def title(self, lang: str = "en") -> str:
        if lang == "hi":
            resolved = _text(self.listing.title_hi, self.listing.title_en)
        else:
            resolved = _text(self.listing.title_en, self.listing.title_hi)
        return resolved or "Untitled piece"

    def description(self, lang: str = "en") -> str | None:
        if lang == "hi":
            return _text(self.listing.description_hi, self.listing.description_en)
        return _text(self.listing.description_en, self.listing.description_hi)

    # -- numbers ---------------------------------------------------------

    @property
    def price_inr(self) -> int:
        return self.quote.price_inr

    @property
    def price(self) -> str:
        return rupees(self.quote.price_inr)

    @property
    def take_home(self) -> str:
        return rupees(self.quote.artisan_take_home_inr)

    @property
    def artisan_share_pct(self) -> int:
        """What fraction of the buyer's money reaches the maker.

        Shown on every card. On a marketplace built to fix exactly this
        number, hiding it would be strange.
        """
        if self.quote.price_inr <= 0:
            return 0
        return round(100 * self.quote.artisan_take_home_inr / self.quote.price_inr)

    @property
    def hours_text(self) -> str:
        hours = self.listing.hours_worked
        if hours is None:
            return "Hours still to be confirmed"
        if hours < 1:
            return f"{round(hours * 60)} minutes of work"
        if hours == int(hours):
            return f"{int(hours)} hours of work"
        return f"{hours:g} hours of work"

    @property
    def place(self) -> str | None:
        return ", ".join(p for p in (self.village, self.state) if p) or None

    @property
    def created_at(self) -> datetime:
        return self.listing.created_at

    # -- stock -----------------------------------------------------------

    @property
    def stock_text(self) -> str:
        n = self.inventory.sellable_now
        if n > 20:
            return "In stock"
        if n > 0:
            return f"Only {n} left"
        if self.inventory.made_to_order and self.inventory.lead_time_days:
            return f"Made to order, about {self.inventory.lead_time_days} days"
        if self.inventory.made_to_order:
            return "Made to order"
        return "Sold out"

    @property
    def search_text(self) -> str:
        listing = self.listing
        parts = [
            listing.title_en,
            listing.title_hi,
            listing.description_en,
            listing.description_hi,
            listing.category,
            listing.craft_type,
            listing.material,
            " ".join(listing.colours or []),
            self.artisan_name,
            self.village,
            self.district,
            self.state,
            self.craft,
        ]
        return " ".join(p for p in parts if p).lower()


@dataclass
class ProductView:
    """A card plus everything only the detail page needs."""

    card: ProductCard
    passport: Passport | None = None
    reel_url: str | None = None
    making_clip_url: str | None = None
    more_from_artisan: list[ProductCard] = field(default_factory=list)
    channel_prices: dict[str, PriceQuote] = field(default_factory=dict)

    def __getattr__(self, name: str):  # pragma: no cover - thin delegation
        # Templates say `product.title('en')` rather than
        # `product.card.title('en')`; anything not defined here falls
        # through to the card.
        return getattr(self.card, name)


def _sellability(listing: Listing, inventory: Inventory, quote: PriceQuote) -> Sellability:
    """The one place that decides whether money may change hands.

    The first rule is the important one. If `material_cost_inr` or
    `hours_worked` is null then the wage floor underneath the price is a
    guess, and selling at a guessed floor is how an artisan ends up
    working for less than minimum wage with a receipt to prove everyone
    agreed. So the buyer surface refuses, and says which question is
    outstanding.
    """
    if quote.floor_incomplete:
        missing = []
        if listing.material_cost_inr is None:
            missing.append("material cost")
        if listing.hours_worked is None:
            missing.append("hours of work")
        return Sellability(
            can_buy=False,
            reason="Not yet for sale",
            detail=(
                "We are still confirming "
                + " and ".join(missing)
                + " with the artisan. Until then there is no honest floor price, "
                "so this piece cannot be sold."
            ),
            can_request=True,
        )

    if inventory.sellable_now <= 0 and not inventory.made_to_order:
        return Sellability(can_buy=False, reason="Sold out", can_request=True)

    return Sellability(can_buy=True)


def build_card(entry: CatalogEntry, quote: PriceQuote) -> ProductCard:
    listing = entry.listing
    artisan = entry.artisan
    return ProductCard(
        listing_id=listing.listing_id,
        listing=listing,
        inventory=entry.inventory,
        quote=quote,
        artisan_name=artisan.name,
        artisan_id=artisan.artisan_id,
        village=artisan.village,
        district=artisan.district,
        state=artisan.state,
        craft=artisan.craft,
        cluster_id=artisan.cluster_id,
        image_url=listing.image_clean_url or listing.image_original_url,
        sellability=_sellability(listing, entry.inventory, quote),
        channels=entry.channels,
    )
