"""Order fulfilment: one order through courier booking and the artisan calls.

    confirm  ->  courier.book + calls: order_confirmed, pickup_scheduled
    deliver  ->  calls: payment_credited        (after the courier delivers)

Both steps are idempotent per order; running `confirm` twice returns the
same shipment and calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from c2 import c1_client, courier, voice
from c2.models import CallLog, Order, Payout, Shipment


def payouts(order: Order) -> list[Payout]:
    """What each artisan is owed on this order, from C1's own numbers.

    A bulk line pays each allocation `take_home_inr` per unit; a retail line
    pays the artisan who made it `artisan_take_home_inr` per unit. If C1 did
    not carry a per-unit figure the amount is None - not 0.
    """
    acc: dict[str, dict[str, Any]] = {}

    def add(artisan_id: str, qty: int, per_unit: int | None, title_en: str, title_hi: str | None) -> None:
        row = acc.setdefault(
            artisan_id, {"qty": 0, "amount": 0, "known": True, "en": [], "hi": []}
        )
        row["qty"] += qty
        if per_unit is None:
            row["known"] = False
        else:
            row["amount"] += per_unit * qty
        if title_en not in row["en"]:
            row["en"].append(title_en)
        if title_hi and title_hi not in row["hi"]:
            row["hi"].append(title_hi)

    for line in order.lines:
        listing = c1_client.seed_listings().get(line.listing_id, {})
        title_hi = listing.get("title_hi")
        allocations = line.split.allocations if line.split else []
        if allocations:
            for alloc in allocations:
                add(alloc.artisan_id, alloc.quantity, alloc.take_home_inr, line.title, title_hi)
            continue
        try:
            product, _ = c1_client.get_product(line.listing_id, line.channel)
        except KeyError:
            continue
        if product.get("artisan_id"):
            add(product["artisan_id"], line.quantity, line.artisan_take_home_inr, line.title, title_hi)

    out = []
    for artisan_id, row in acc.items():
        art = c1_client.artisan(artisan_id)
        out.append(
            Payout(
                artisan_id=artisan_id,
                artisan_name=art.get("name", artisan_id),
                village=art.get("village"),
                state=art.get("state"),
                languages=art.get("languages", []),
                quantity=row["qty"],
                titles_en=row["en"],
                titles_hi=row["hi"],
                amount_inr=row["amount"] if row["known"] else None,
            )
        )
    return out


def confirm(order: Order) -> tuple[Shipment, list[Payout], list[CallLog]]:
    shipment = courier.book(order)
    owed = payouts(order)
    calls: list[CallLog] = []
    for p in owed:
        calls.append(voice.place_call("order_confirmed", order, p, shipment))
        calls.append(voice.place_call("pickup_scheduled", order, p, shipment))
    return shipment, owed, calls


def deliver(order: Order) -> list[CallLog]:
    """Simulate delivery: mark the shipment delivered and place the payout calls."""
    shipment = courier.get(order.order_id) or courier.book(order)
    at = datetime.now(timezone.utc)
    return [voice.place_call("payment_credited", order, p, shipment, at=at) for p in payouts(order)]
