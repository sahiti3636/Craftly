"""The bridge to C1 (`market/`). The only module in C2 that knows where C1 is.

Two things come from C1:

* **Priced listings** - `GET /api/products/{id}?channel=amazon`, the payload
  the README names as what a marketplace adapter pushes. Tried in this
  order: C1 over HTTP; C1's route function loaded in-process (so the demo
  works with C1 not running but its dependencies installed); a bare seed
  listing with *no price*, which the channel adapters refuse to push.
* **Placed orders** - the `market/orders.jsonl` log C1 appends to. When it
  is empty, `sample_orders()` builds a few from the real seed catalogue so
  a fresh clone still has something to fulfil. Those carry
  `source="c2_sample"` and are never written back to C1's log.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Any

import httpx

from c2 import b2_client, config
from c2.models import Order

SRC_HTTP = "c1_http"
SRC_INPROCESS = "c1_inprocess"
SRC_SEED = "seed_fallback"
#: Where orders came from, as `orders_or_sample` reports it.
SRC_B2 = "b2"


def today() -> date:
    if config.TODAY_OVERRIDE:
        return date.fromisoformat(config.TODAY_OVERRIDE)
    return date.today()


# ---------------------------------------------------------------------------
# Seed data (C1's committed catalogue)
# ---------------------------------------------------------------------------


def _load_seed(name: str) -> list[dict[str, Any]]:
    path = config.SEED_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


@lru_cache(maxsize=1)
def seed_listings() -> dict[str, dict[str, Any]]:
    return {row["listing_id"]: row for row in _load_seed("listings.json")}


@lru_cache(maxsize=1)
def seed_artisans() -> dict[str, dict[str, Any]]:
    return {row["artisan_id"]: row for row in _load_seed("artisans.json")}


@lru_cache(maxsize=1)
def seed_inventory() -> dict[str, dict[str, Any]]:
    return {row["listing_id"]: row for row in _load_seed("inventory.json")}


def artisan(artisan_id: str) -> dict[str, Any]:
    """From C1's seed, else from B2 (an artisan who signed up in Studio)."""
    found = seed_artisans().get(artisan_id) or b2_client.artisan(artisan_id)
    return found or {"artisan_id": artisan_id, "name": artisan_id}


def cluster_members(artisan_id: str) -> list[dict[str, Any]]:
    cluster = artisan(artisan_id).get("cluster_id")
    if not cluster:
        return [artisan(artisan_id)]
    return [a for a in seed_artisans().values() if a.get("cluster_id") == cluster]


# ---------------------------------------------------------------------------
# Priced listings
# ---------------------------------------------------------------------------


def _via_http(listing_id: str, channel: str) -> dict[str, Any] | None:
    try:
        resp = httpx.get(
            f"{config.C1_URL}/api/products/{listing_id}",
            params={"channel": channel},
            timeout=2.0,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code == 404:
        raise KeyError(listing_id)
    if resp.status_code != 200:
        return None
    return resp.json()


@lru_cache(maxsize=1)
def _c1_route():
    """C1's `product` route function and `Channel` enum, or None.

    C1's package is called `app`, which C2 never uses, so putting
    `market/` on sys.path is safe.
    """
    if str(config.MARKET_DIR) not in sys.path:
        sys.path.insert(0, str(config.MARKET_DIR))
    try:
        from app.contracts import Channel  # type: ignore[import-not-found]
        from app.routes.api import product  # type: ignore[import-not-found]
    except Exception:  # missing deps or C1 absent - fall through to seed
        return None
    return product, Channel


def _via_inprocess(listing_id: str, channel: str) -> dict[str, Any] | None:
    loaded = _c1_route()
    if loaded is None:
        return None
    product, Channel = loaded
    try:
        out = product(listing_id, Channel(channel))
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404:
            raise KeyError(listing_id) from exc
        return None
    return out.model_dump(mode="json")


def _via_seed(listing_id: str) -> dict[str, Any]:
    listing = seed_listings().get(listing_id)
    if listing is None:
        raise KeyError(listing_id)
    art = artisan(listing.get("artisan_id", ""))
    return {
        "listing": listing,
        "inventory": seed_inventory().get(listing_id),
        "price": None,
        "artisan_id": listing.get("artisan_id"),
        "artisan_name": art.get("name"),
        "place": ", ".join(p for p in (art.get("village"), art.get("state")) if p) or None,
        "image_url": None,
        "can_buy": False,
        "blocked_reason": "C1 unreachable: no price quote",
        "passport_code": None,
    }


def get_product(listing_id: str, channel: str = "own_store") -> tuple[dict[str, Any], str]:
    """The priced listing as C1's `/api/products/{id}` returns it, plus which
    tier answered. Raises KeyError for an id C1 does not know."""
    found = _via_http(listing_id, channel)
    if found is not None:
        return found, SRC_HTTP
    found = _via_inprocess(listing_id, channel)
    if found is not None:
        return found, SRC_INPROCESS
    return _via_seed(listing_id), SRC_SEED


def all_listing_ids() -> list[str]:
    return list(seed_listings())


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def load_orders() -> list[Order]:
    """Every order in C1's log, oldest first. A malformed line is skipped:
    one bad row must not stop a courier booking for the others."""
    try:
        text = config.ORDERS_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    by_id: dict[str, Order] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            order = Order.model_validate_json(line)
        except ValueError:
            continue
        by_id[order.order_id] = order  # last write wins, as in an append log
    return sorted(by_id.values(), key=lambda o: o.created_at)


_BUYERS = [
    ("Meera Iyer", "+91 98450 11223", "Bengaluru", "Karnataka", "560034", "14, 5th Cross, Koramangala"),
    ("Rohan Kapoor", "+91 98110 44556", "New Delhi", "Delhi", "110017", "B-22, Hauz Khas"),
    ("Anjali Deshmukh", "+91 98220 77889", "Pune", "Maharashtra", "411004", "7, Prabhat Road"),
    ("The Loom Room", "+91 98330 99001", "Mumbai", "Maharashtra", "400050", "Shop 3, Linking Road, Bandra"),
]


def _quote(listing_id: str, channel: str = "own_store") -> dict[str, Any] | None:
    try:
        product, _ = get_product(listing_id, channel)
    except KeyError:
        return None
    if not product.get("price"):
        return None
    return product


def _buyer(n: int) -> dict[str, str]:
    name, phone, city, state, pin, addr = _BUYERS[n]
    return {"name": name, "phone": phone, "city": city, "state": state, "pincode": pin, "address": addr}


def sample_orders() -> list[Order]:
    """Four orders built from the real seed catalogue and C1's real prices.

    Three retail, one bulk with a cluster split. Deterministic: same ids,
    same buyers, dates relative to `today()`. Empty if C1 cannot quote.
    """
    ids = all_listing_ids()
    if len(ids) < 4:
        return []
    base = datetime.combine(today(), time(10, 30), tzinfo=timezone.utc)

    def retail(n: int, listing_id: str, qty: int, days_ago: int, channel: str) -> Order | None:
        product = _quote(listing_id, channel)
        if product is None:
            return None
        price = product["price"]
        return Order.model_validate(
            {
                "order_id": f"ord_c2sample{n + 1:02d}",
                "kind": "retail",
                "buyer": _buyer(n),
                "lines": [
                    {
                        "listing_id": listing_id,
                        "title": product["listing"].get("title_en") or listing_id,
                        "quantity": qty,
                        "unit_price_inr": price["price_inr"],
                        "channel": channel,
                        "artisan_take_home_inr": price["artisan_take_home_inr"],
                    }
                ],
                "channel": channel,
                "source": "c2_sample",
                "created_at": base - timedelta(days=days_ago),
            }
        )

    orders: list[Order | None] = [
        retail(0, ids[0], 2, 1, "own_store"),
        retail(1, ids[2], 1, 2, "amazon"),
        retail(2, ids[0], 3, 3, "own_store"),
    ]

    # Bulk: pool one listing across every member of its artisan's cluster.
    bulk_id = ids[2]
    product = _quote(bulk_id, "b2b")
    if product is not None:
        members = cluster_members(product["artisan_id"])
        qty = 60
        shares = [qty // len(members)] * len(members)
        shares[0] += qty - sum(shares)
        take_home = product["price"]["artisan_take_home_inr"]
        allocations = [
            {
                "artisan_id": m["artisan_id"],
                "artisan_name": m["name"],
                "village": m.get("village"),
                "quantity": share,
                "take_home_inr": take_home,
            }
            for m, share in zip(members, shares)
            if share > 0
        ]
        orders.append(
            Order.model_validate(
                {
                    "order_id": "ord_c2sample04",
                    "kind": "bulk",
                    "buyer": _buyer(3),
                    "lines": [
                        {
                            "listing_id": bulk_id,
                            "title": product["listing"].get("title_en") or bulk_id,
                            "quantity": qty,
                            "unit_price_inr": product["price"]["price_inr"],
                            "channel": "b2b",
                            "artisan_take_home_inr": take_home,
                            "split": {"allocations": allocations, "feasible": True},
                        }
                    ],
                    "channel": "b2b",
                    "source": "c2_sample",
                    "deadline": base + timedelta(days=21),
                    "created_at": base - timedelta(days=4),
                }
            )
        )
    return [o for o in orders if o is not None]


def orders_or_sample() -> tuple[list[Order], str]:
    """B2's orders when C2 has B2's service token (even if there are none
    yet), else C1's local order log, else the sample set. The second value
    says which, so nothing downstream passes samples off as real."""
    from_b2 = b2_client.orders()
    if from_b2 is not None:
        return from_b2, SRC_B2
    real = load_orders()
    if real:
        return real, "c1_orders_jsonl"
    return sample_orders(), "c2_sample"
