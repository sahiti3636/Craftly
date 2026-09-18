"""Browse, filter and sort. Pure functions over already-priced cards.

No I/O, no adapters, no request object — `run()` takes a list of
`ProductCard` and a `SearchQuery` and returns a `SearchResult`. That is
what makes the interesting parts testable without a server, and it is why
the price filter works: prices arrive on the card from B1 rather than
being re-derived here.

The ranking is deliberately explainable rather than clever. A buyer
typing "kutch indigo" is matching words against a field, and a craft
marketplace with a few thousand items does not need an embedding index to
do that. When the catalogue outgrows this, the replacement is a real
search engine behind `CatalogSource`, not a smarter loop here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from app.view import ProductCard

_TOKEN = re.compile(r"[a-z0-9ऀ-ॿ]+")


class Sort(str, Enum):
    RELEVANCE = "relevance"
    PRICE_LOW = "price_low"
    PRICE_HIGH = "price_high"
    NEWEST = "newest"
    MOST_WORK = "most_work"
    ARTISAN_SHARE = "artisan_share"


#: Field weights for text matching. Craft and material score above the
#: description because someone searching "dhokra" wants the technique, not
#: every listing whose story mentions it in passing.
_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("craft_type", 6),
    ("material", 5),
    ("category", 4),
    ("colours", 3),
    ("artisan", 4),
    ("place", 4),
    ("title", 6),
    ("description", 2),
)


@dataclass
class SearchQuery:
    q: str = ""
    category: str | None = None
    craft_type: str | None = None
    material: str | None = None
    colour: str | None = None
    state: str | None = None
    min_price_inr: int | None = None
    max_price_inr: int | None = None
    in_stock_only: bool = False
    buyable_only: bool = False
    sort: Sort = Sort.RELEVANCE
    page: int = 1
    per_page: int = 12

    def with_(self, **changes: object) -> "SearchQuery":
        """A copy with some fields changed — for building filter links."""
        data = {**self.__dict__, **changes}
        return SearchQuery(**data)  # type: ignore[arg-type]

    @property
    def is_filtered(self) -> bool:
        return any(
            [
                self.q.strip(),
                self.category,
                self.craft_type,
                self.material,
                self.colour,
                self.state,
                self.min_price_inr is not None,
                self.max_price_inr is not None,
                self.in_stock_only,
            ]
        )


@dataclass
class Facet:
    value: str
    count: int
    selected: bool = False


@dataclass
class SearchResult:
    cards: list[ProductCard]
    total: int
    page: int
    per_page: int
    query: SearchQuery
    facets: dict[str, list[Facet]] = field(default_factory=dict)

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def tokenise(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _card_fields(card: ProductCard) -> dict[str, str]:
    listing = card.listing
    return {
        "craft_type": (listing.craft_type or "") + " " + (card.craft or ""),
        "material": listing.material or "",
        "category": listing.category or "",
        "colours": " ".join(listing.colours or []),
        "artisan": card.artisan_name,
        "place": " ".join(p for p in (card.village, card.district, card.state) if p),
        "title": " ".join(p for p in (listing.title_en, listing.title_hi) if p),
        "description": " ".join(
            p for p in (listing.description_en, listing.description_hi) if p
        ),
    }


def score(card: ProductCard, tokens: list[str]) -> int:
    """Sum of field weights for every query token that appears in a field.

    A token matches on prefix, so "pott" finds "pottery" and a buyer who
    stops typing halfway still gets results.
    """
    if not tokens:
        return 0
    fields = {name: tokenise(text) for name, text in _card_fields(card).items()}
    total = 0
    for token in tokens:
        for name, weight in _WEIGHTS:
            words = fields.get(name, [])
            if any(word.startswith(token) for word in words):
                total += weight
    return total


def _matches_filters(card: ProductCard, query: SearchQuery) -> bool:
    listing = card.listing

    if query.category and (listing.category or "").lower() != query.category.lower():
        return False
    if query.craft_type and (listing.craft_type or "").lower() != query.craft_type.lower():
        return False
    if query.material and (listing.material or "").lower() != query.material.lower():
        return False
    if query.colour:
        colours = [c.lower() for c in (listing.colours or [])]
        if query.colour.lower() not in colours:
            return False
    if query.state and (card.state or "").lower() != query.state.lower():
        return False
    if query.min_price_inr is not None and card.price_inr < query.min_price_inr:
        return False
    if query.max_price_inr is not None and card.price_inr > query.max_price_inr:
        return False
    if query.in_stock_only and card.inventory.sellable_now <= 0:
        return False
    if query.buyable_only and not card.sellability.can_buy:
        return False
    return True


def _facets(cards: list[ProductCard], query: SearchQuery) -> dict[str, list[Facet]]:
    """Counts for the filter sidebar, over the text-matched set.

    Counted before the structured filters are applied, so that picking
    "textiles" does not make every other category vanish from the list
    and strand the buyer with no way back.
    """

    def tally(key) -> list[Facet]:
        counts: dict[str, int] = {}
        for card in cards:
            for value in key(card):
                if value:
                    counts[value] = counts.get(value, 0) + 1
        return [Facet(value=v, count=n) for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    facets = {
        "category": tally(lambda c: [c.listing.category]),
        "craft_type": tally(lambda c: [c.listing.craft_type]),
        "material": tally(lambda c: [c.listing.material]),
        "colour": tally(lambda c: c.listing.colours or []),
        "state": tally(lambda c: [c.state]),
    }

    selected = {
        "category": query.category,
        "craft_type": query.craft_type,
        "material": query.material,
        "colour": query.colour,
        "state": query.state,
    }
    for name, chosen in selected.items():
        if not chosen:
            continue
        for facet in facets[name]:
            facet.selected = facet.value.lower() == chosen.lower()

    return facets


def _sort_key(sort: Sort):
    if sort is Sort.PRICE_LOW:
        return lambda item: (item[1].price_inr, item[1].listing_id)
    if sort is Sort.PRICE_HIGH:
        return lambda item: (-item[1].price_inr, item[1].listing_id)
    if sort is Sort.NEWEST:
        return lambda item: (-item[1].created_at.timestamp(), item[1].listing_id)
    if sort is Sort.MOST_WORK:
        return lambda item: (-(item[1].listing.hours_worked or 0), item[1].listing_id)
    if sort is Sort.ARTISAN_SHARE:
        return lambda item: (-item[1].artisan_share_pct, item[1].listing_id)
    # Relevance: score first, then newest, so an empty query is still a
    # sensible shop front rather than an arbitrary one.
    return lambda item: (-item[0], -item[1].created_at.timestamp(), item[1].listing_id)


def run(cards: list[ProductCard], query: SearchQuery) -> SearchResult:
    tokens = tokenise(query.q)

    scored: list[tuple[int, ProductCard]] = []
    for card in cards:
        card_score = score(card, tokens)
        if tokens and card_score == 0:
            continue
        scored.append((card_score, card))

    text_matched = [card for _, card in scored]
    facets = _facets(text_matched, query)

    filtered = [(s, c) for s, c in scored if _matches_filters(c, query)]
    filtered.sort(key=_sort_key(query.sort))

    total = len(filtered)
    per_page = max(1, query.per_page)
    page = max(1, query.page)
    start = (page - 1) * per_page
    window = [card for _, card in filtered[start : start + per_page]]

    return SearchResult(
        cards=window,
        total=total,
        page=page,
        per_page=per_page,
        query=query,
        facets=facets,
    )
