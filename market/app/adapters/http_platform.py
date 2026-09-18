"""The real adapters: C1 talking to B1 and B2 over HTTP.

**Status: written against endpoints that do not exist yet.** This file is
half implementation and half request. It says, in code, exactly what the
buyer surfaces need from the other two slices and in what shape, so the
conversation with B1 and B2 can be about a diff instead of a whiteboard.

Endpoints assumed (all JSON, all shapes defined in `app/contracts.py`):

    B2 platform, CRAFTLY_PLATFORM_URL
      GET  /catalog/entries                     -> [{listing, artisan, inventory}]
      GET  /catalog/entries/{listing_id}        -> {listing, artisan, inventory}
      GET  /clusters/{cluster_id}/members       -> [Artisan]
      GET  /clusters/{cluster_id}/entries       -> [{listing, artisan, inventory}]
             ?craft_type=
      GET  /passports/by-listing/{listing_id}   -> Passport
      GET  /passports/by-code/{code}            -> Passport
      POST /orders                              -> Order

    B1 engines, CRAFTLY_ENGINES_URL
      POST /price      {listing, artisan, inventory, channel, quantity} -> PriceQuote
      POST /split      {listing, artisan, inventory, quantity, deadline_days} -> SplitPlan

If B1 or B2 would rather expose something different, change this file and
nothing else. That is the whole point of it being one file.

Failure behaviour: a network error raises. Routes catch it and render a
"the catalogue is unavailable" state — a buyer surface that silently shows
an empty shop when the platform is down is worse than one that says so.
"""

from __future__ import annotations

import httpx

from app.contracts import (
    Artisan,
    Channel,
    Inventory,
    Listing,
    Order,
    Passport,
    PriceQuote,
    SplitPlan,
)
from app.ports import CatalogEntry

TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class PlatformUnavailable(RuntimeError):
    """B1 or B2 could not be reached, or answered with an error."""


def _entry_from_json(payload: dict) -> CatalogEntry:
    listing = Listing.model_validate(payload["listing"])
    return CatalogEntry(
        listing=listing,
        artisan=Artisan.model_validate(payload["artisan"]),
        inventory=Inventory.model_validate(
            payload.get("inventory") or {"listing_id": listing.listing_id}
        ),
        published=payload.get("published", True),
        channels=tuple(Channel(c) for c in payload.get("channels", ["own_store"])),
    )


def _entry_to_json(entry: CatalogEntry) -> dict:
    return {
        "listing": entry.listing.model_dump(mode="json"),
        "artisan": entry.artisan.model_dump(mode="json"),
        "inventory": entry.inventory.model_dump(mode="json"),
    }


class _Client:
    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    def _get(self, path: str, **params: object) -> object:
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.get(f"{self._base}{path}", params=params or None)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise PlatformUnavailable(f"GET {self._base}{path} failed: {exc}") from exc

    def _post(self, path: str, payload: dict) -> object:
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.post(f"{self._base}{path}", json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise PlatformUnavailable(f"POST {self._base}{path} failed: {exc}") from exc


class HttpCatalog(_Client):
    def all_entries(self) -> list[CatalogEntry]:
        payload = self._get("/catalog/entries") or []
        return [_entry_from_json(item) for item in payload]  # type: ignore[union-attr]

    def get_entry(self, listing_id: str) -> CatalogEntry | None:
        payload = self._get(f"/catalog/entries/{listing_id}")
        return _entry_from_json(payload) if payload else None  # type: ignore[arg-type]

    def cluster_members(self, cluster_id: str) -> list[Artisan]:
        payload = self._get(f"/clusters/{cluster_id}/members") or []
        return [Artisan.model_validate(item) for item in payload]  # type: ignore[union-attr]

    def cluster_entries(
        self, cluster_id: str, craft_type: str | None = None
    ) -> list[CatalogEntry]:
        payload = self._get(f"/clusters/{cluster_id}/entries", craft_type=craft_type) or []
        return [_entry_from_json(item) for item in payload]  # type: ignore[union-attr]


class HttpPriceEngine(_Client):
    def quote(
        self, entry: CatalogEntry, channel: Channel = Channel.OWN_STORE, quantity: int = 1
    ) -> PriceQuote:
        payload = _entry_to_json(entry) | {"channel": channel.value, "quantity": quantity}
        return PriceQuote.model_validate(self._post("/price", payload))


class HttpOrderEngine(_Client):
    def split(
        self, entry: CatalogEntry, quantity: int, deadline_days: int | None = None
    ) -> SplitPlan:
        payload = _entry_to_json(entry) | {
            "quantity": quantity,
            "deadline_days": deadline_days,
        }
        return SplitPlan.model_validate(self._post("/split", payload))


class HttpPassportSource(_Client):
    def by_listing(self, listing_id: str) -> Passport | None:
        payload = self._get(f"/passports/by-listing/{listing_id}")
        return Passport.model_validate(payload) if payload else None

    def by_code(self, verification_code: str) -> Passport | None:
        payload = self._get(f"/passports/by-code/{verification_code}")
        return Passport.model_validate(payload) if payload else None


class HttpOrderSink(_Client):
    def place(self, order: Order) -> Order:
        return Order.model_validate(self._post("/orders", order.model_dump(mode="json")))

    def get(self, order_id: str) -> Order | None:
        payload = self._get(f"/orders/{order_id}")
        return Order.model_validate(payload) if payload else None
