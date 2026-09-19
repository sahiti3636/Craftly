"""Marketplace channel adapters (simulated).

The interface is what a real integration would implement: `build_request`
turns C1's priced listing into the marketplace's payload, `submit` sends it
and returns the marketplace's reply. Here `submit` never leaves the
process - it returns the reply a marketplace would, with deterministic ids,
so a demo is repeatable. A live adapter replaces `submit` (and nothing
else) with a real HTTP call.

What is real: the payload is built from C1's `/api/products/{id}?channel=`
answer, so the price on the channel is the price C1 computed with the
artisan's take-home held constant. The rules C1 and B1 live by are enforced
here too, because a marketplace push is the one place a bad listing leaves
the building:

* no price quote                -> refused (never push a guessed price)
* `floor_incomplete`            -> refused (the wage floor is a guess)
* C1 says it cannot be bought   -> refused, with C1's reason
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Protocol

from c2 import c1_client, config
from c2.models import ChannelPush


def _digest(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest().upper()


def _abs_url(url: str | None) -> str | None:
    if not url:
        return None
    return url if url.startswith("http") else f"{config.C1_URL}{url}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChannelAdapter(Protocol):
    name: str

    def build_request(self, product: dict[str, Any]) -> dict[str, Any]: ...

    def submit(self, request: dict[str, Any]) -> dict[str, Any]: ...


class AmazonAdapter:
    """Shaped like an Amazon Listings Items API `putListingsItem` call."""

    name = "amazon"

    def build_request(self, product: dict[str, Any]) -> dict[str, Any]:
        listing, price, inv = product["listing"], product["price"], product.get("inventory") or {}
        return {
            "sku": listing["listing_id"],
            "productType": "HANDMADE_PRODUCT",
            "requirements": "LISTING",
            "attributes": {
                "item_name": [{"value": listing.get("title_en"), "language_tag": "en_IN"}],
                "product_description": [{"value": listing.get("description_en"), "language_tag": "en_IN"}],
                "bullet_point": [
                    {"value": f"Made by {product.get('artisan_name')} in {product.get('place')}"},
                    {"value": f"Material: {listing.get('material')}"},
                    {"value": f"Craft: {listing.get('craft_type')}"},
                ],
                "country_of_origin": [{"value": "IN"}],
                "purchasable_offer": [{"currency": "INR", "our_price": [{"schedule": [{"value_with_tax": price["price_inr"]}]}]}],
                "fulfillment_availability": [
                    {"fulfillment_channel_code": "DEFAULT", "quantity": inv.get("in_stock", 0),
                     "lead_time_to_ship_max_days": inv.get("lead_time_days")}
                ],
                "main_product_image_locator": [{"media_location": _abs_url(product.get("image_url"))}],
            },
        }

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        sku = request["sku"]
        return {
            "status": "ACCEPTED",
            "submissionId": f"SUB-{_digest('amazon', sku)[:12]}",
            "sku": sku,
            "asin": "B0" + _digest("asin", sku)[:8],
            "issues": [],
        }


class FlipkartAdapter:
    """Shaped like a Flipkart Seller Hub listing create."""

    name = "flipkart"

    def build_request(self, product: dict[str, Any]) -> dict[str, Any]:
        listing, price, inv = product["listing"], product["price"], product.get("inventory") or {}
        return {
            "skuId": listing["listing_id"],
            "listingStatus": "ACTIVE",
            "attributes": {
                "title": listing.get("title_en"),
                "description": listing.get("description_en"),
                "brand": "Craftly Artisans",
                "sold_by_artisan": product.get("artisan_name"),
                "material": listing.get("material"),
                "country_of_origin": "India",
                "image_url": _abs_url(product.get("image_url")),
            },
            "pricing": {"selling_price": price["price_inr"], "currency": "INR"},
            "inventory": {"stock": inv.get("in_stock", 0), "dispatch_sla_days": inv.get("lead_time_days")},
        }

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        sku = request["skuId"]
        return {
            "status": "SUCCESS",
            "listingId": f"LST{_digest('fk', sku)[:13]}",
            "fsn": f"FSN{_digest('fsn', sku)[:10]}",
            "skuId": sku,
            "errors": [],
        }


class EbayAdapter:
    """Shaped like an eBay Inventory API `createOrReplaceInventoryItem` + offer."""

    name = "ebay"

    def build_request(self, product: dict[str, Any]) -> dict[str, Any]:
        listing, price, inv = product["listing"], product["price"], product.get("inventory") or {}
        return {
            "sku": listing["listing_id"],
            "product": {
                "title": listing.get("title_en"),
                "description": listing.get("description_en"),
                "aspects": {
                    "Material": [listing.get("material")],
                    "Country/Region of Manufacture": ["India"],
                    "Handmade": ["Yes"],
                },
                "imageUrls": [u for u in [_abs_url(product.get("image_url"))] if u],
            },
            "availability": {"shipToLocationAvailability": {"quantity": inv.get("in_stock", 0)}},
            "offer": {"format": "FIXED_PRICE", "pricingSummary": {"price": {"value": str(price["price_inr"]), "currency": "INR"}}},
        }

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        sku = request["sku"]
        return {
            "status": "PUBLISHED",
            "offerId": str(int(_digest("ebay", sku)[:10], 16) % 10**12).zfill(12),
            "listingId": str(int(_digest("ebay-l", sku)[:10], 16) % 10**12).zfill(12),
            "sku": sku,
            "warnings": [],
        }


ADAPTERS: dict[str, ChannelAdapter] = {
    a.name: a for a in (AmazonAdapter(), FlipkartAdapter(), EbayAdapter())
}


class ChannelError(Exception):
    """A bad request to C2: unknown channel or unknown listing."""


def refusal_reasons(product: dict[str, Any]) -> list[str]:
    price = product.get("price")
    if not price:
        return ["no price quote from C1 - a listing is never pushed at a guessed price"]
    reasons = []
    if price.get("floor_incomplete"):
        reasons.append("wage floor incomplete (material cost or hours missing) - artisan must be asked first")
    if not product.get("can_buy"):
        reasons.append(product.get("blocked_reason") or "C1 marks this listing as not buyable")
    return reasons


def push(channel: str, listing_id: str) -> ChannelPush:
    adapter = ADAPTERS.get(channel)
    if adapter is None:
        raise ChannelError(f"'{channel}' is not a marketplace channel; use one of {sorted(ADAPTERS)}")
    try:
        product, source = c1_client.get_product(listing_id, channel)
    except KeyError as exc:
        raise ChannelError(f"No such listing: {listing_id}") from exc

    reasons = refusal_reasons(product)
    if reasons:
        return ChannelPush(
            channel=channel,
            listing_id=listing_id,
            price_source=source,
            request={},
            response={"status": "NOT_SENT", "submitted_at": _now()},
            warnings=reasons,
        )

    request = adapter.build_request(product)
    response = {**adapter.submit(request), "simulated": True, "submitted_at": _now()}
    price = product["price"]
    warnings = []
    if price.get("below_floor"):
        warnings.append("market band sits under the wage floor - listed at the floor, not the market price")
    response["settlement_preview"] = {
        "buyer_pays_inr": price["price_inr"],
        "channel_fee_inr": price["channel_fee_inr"],
        "artisan_receives_inr": price["artisan_take_home_inr"],
    }
    return ChannelPush(
        channel=channel,
        listing_id=listing_id,
        price_source=source,
        request=request,
        response=response,
        warnings=warnings,
    )


def push_catalogue(channel: str, limit: int | None = None) -> list[ChannelPush]:
    ids = c1_client.all_listing_ids()
    return [push(channel, i) for i in (ids[:limit] if limit else ids)]
