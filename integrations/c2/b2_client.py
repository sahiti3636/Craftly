"""The bridge to B2 (`platform/`): the real orders, artisans and payouts.

With `CRAFTLY_SERVICE_TOKEN` set (the same value as B2's), C2 reads its
orders from B2 instead of C1's local order log, and moves them along as
the simulated courier does: picked up is `dispatched`, handed over is
`delivered`, and delivery is what makes B2 settle the payout. The
payment call then speaks B2's payout rows, so the amount an artisan hears
is the amount B2 recorded, not a second calculation of it.

B2 also holds each artisan's phone and whether she agreed to AI calls in
Craftly Studio; `contact()` reads both, and `voice.py` does not call
anyone who has not said yes.

Without the token every function here reports "not available" and C2
falls back to C1's order log, as before.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import httpx

from c2 import config
from c2.models import Order

#: Order states in which the artisan has taken the order on, so a courier
#: can be booked for it.
ACCEPTED = ("accepted", "in_production", "dispatched", "delivered")


class B2Error(RuntimeError):
    """B2 refused, or could not be reached. The message is fit to show."""


def enabled() -> bool:
    return bool(config.SERVICE_TOKEN)


def _call(method: str, path: str, **kwargs) -> Any:
    try:
        response = httpx.request(
            method,
            f"{config.PLATFORM_URL}{path}",
            headers={"Authorization": f"Bearer {config.SERVICE_TOKEN}"},
            timeout=10.0,
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise B2Error(f"The Craftly platform (B2) is not reachable at {config.PLATFORM_URL}.") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise B2Error(str(detail or f"B2 answered {response.status_code} to {method} {path}."))
    return response.json()


def orders() -> list[Order] | None:
    """Every order in B2, oldest first, or None when B2 is not available."""
    if not enabled():
        return None
    try:
        rows = _call("GET", "/orders", params={"limit": 1000})
    except B2Error:
        return None
    return sorted((Order.model_validate(row) for row in rows), key=lambda o: o.created_at)


def status(order_id: str) -> str:
    return _call("GET", f"/orders/{order_id}")["status"]


def deliver(order_id: str) -> str:
    """Walk an order to `delivered` the way a courier would: collected
    (`dispatched`) first if the artisan has not marked it so herself.
    Delivery settles the payout in B2. Idempotent."""
    current = status(order_id)
    if current == "delivered":
        return current
    if current not in ACCEPTED:
        raise B2Error(f"This order is {current}; the artisan has not accepted it yet.")
    if current != "dispatched":
        _call("POST", f"/orders/{order_id}/status", json={"status": "dispatched", "note": "Collected by courier"})
    return _call("POST", f"/orders/{order_id}/status", json={"status": "delivered", "note": "Delivered by courier"})["status"]


def settlement(order_id: str) -> dict[str, Any]:
    return _call("GET", f"/orders/{order_id}/settlement")


@lru_cache(maxsize=512)
def artisan(artisan_id: str) -> dict[str, Any] | None:
    """An artisan's public profile, for one who is not in C1's seed data."""
    if not enabled():
        return None
    try:
        return _call("GET", f"/artisans/{artisan_id}")
    except B2Error:
        return None


def contact(artisan_id: str) -> dict[str, Any] | None:
    """Phone, language and call consent, or None when B2 is not available.
    Not cached: she can change her mind in Studio at any time."""
    if not enabled():
        return None
    try:
        return _call("GET", f"/artisans/{artisan_id}/contact")
    except B2Error:
        return None
