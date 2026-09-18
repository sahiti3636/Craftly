"""The basket: a signed cookie holding listing ids and quantities.

No server-side session, no database table, no login before you can put
something in a basket. The cookie holds `{listing_id: quantity}` and
nothing else — no prices. Prices are re-fetched from B1 on every render,
so a basket that sat open for three days cannot check out at a stale
price, and nobody can edit a cookie into a discount.

Signed rather than plain because an unsigned cart cookie is an invitation
to paste arbitrary listing ids at the server; signing means a tampered
cookie is discarded as if the basket were empty, which is a safe way to
fail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeSerializer

from app.money import rupees
from app.view import ProductCard

COOKIE_NAME = "craftly_cart"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

#: Not a security boundary — the cart holds no prices and no identity, so
#: the worst a forged cookie achieves is showing someone their own basket
#: with things in it. It stops casual tampering and keeps the parsing
#: honest. A real secret belongs in the environment once B2 has auth.
_SECRET = "craftly-cart-v1"
_serializer = URLSafeSerializer(_SECRET, salt="cart")

MAX_QUANTITY_PER_LINE = 99


def read(raw: str | None) -> dict[str, int]:
    if not raw:
        return {}
    try:
        payload = _serializer.loads(raw)
    except BadSignature:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, int] = {}
    for listing_id, quantity in payload.items():
        try:
            n = int(quantity)
        except (TypeError, ValueError):
            continue
        if isinstance(listing_id, str) and 0 < n <= MAX_QUANTITY_PER_LINE:
            out[listing_id] = n
    return out


def write(items: dict[str, int]) -> str:
    return _serializer.dumps(items)


def add(items: dict[str, int], listing_id: str, quantity: int = 1) -> dict[str, int]:
    updated = dict(items)
    current = updated.get(listing_id, 0)
    updated[listing_id] = min(current + max(quantity, 1), MAX_QUANTITY_PER_LINE)
    return updated


def set_quantity(items: dict[str, int], listing_id: str, quantity: int) -> dict[str, int]:
    updated = dict(items)
    if quantity <= 0:
        updated.pop(listing_id, None)
    else:
        updated[listing_id] = min(quantity, MAX_QUANTITY_PER_LINE)
    return updated


@dataclass
class CartLine:
    card: ProductCard
    quantity: int

    @property
    def subtotal_inr(self) -> int:
        return self.card.price_inr * self.quantity

    @property
    def subtotal(self) -> str:
        return rupees(self.subtotal_inr)

    @property
    def artisan_subtotal_inr(self) -> int:
        return self.card.quote.artisan_take_home_inr * self.quantity


@dataclass
class Cart:
    lines: list[CartLine]
    #: Items dropped while building the cart, with the reason. A basket
    #: that quietly loses a line is worse than one that explains.
    removed: list[tuple[str, str]]

    @property
    def total_inr(self) -> int:
        return sum(line.subtotal_inr for line in self.lines)

    @property
    def total(self) -> str:
        return rupees(self.total_inr)

    @property
    def artisan_total_inr(self) -> int:
        return sum(line.artisan_subtotal_inr for line in self.lines)

    @property
    def artisan_total(self) -> str:
        return rupees(self.artisan_total_inr)

    @property
    def count(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def is_empty(self) -> bool:
        return not self.lines

    @property
    def artisan_count(self) -> int:
        return len({line.card.artisan_id for line in self.lines})


def build(items: dict[str, int], cards: dict[str, ProductCard]) -> Cart:
    """Turn `{listing_id: qty}` into priced lines, dropping what cannot be sold.

    A listing that has been unpublished, or that lost its wage floor
    because the artisan is still being asked about hours, comes out of the
    basket here rather than at the payment step.
    """
    lines: list[CartLine] = []
    removed: list[tuple[str, str]] = []

    for listing_id, quantity in items.items():
        card = cards.get(listing_id)
        if card is None:
            removed.append((listing_id, "This piece is no longer listed."))
            continue
        if not card.sellability.can_buy:
            removed.append(
                (card.title(), card.sellability.detail or card.sellability.reason or "Unavailable.")
            )
            continue
        lines.append(CartLine(card=card, quantity=quantity))

    return Cart(lines=lines, removed=removed)
