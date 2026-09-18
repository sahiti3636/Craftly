"""Shared helpers for the routes: pricing a catalogue, picking a language.

Routes ask this module for cards; this module asks `registry` for
adapters. No route imports an adapter directly.

A note on cost, because it is a real edge this design has: `cards()` asks
the price engine once per listing. With the stub that is arithmetic, and
with a browse page of twelve items it is twelve calls to B1 over HTTP.
The fix is a batch price endpoint, and it belongs in B1's interface rather
than in a cache here — a stale price in a basket is a worse bug than a
slow page. Flagged in `adapters/http_platform.py` for that conversation.
"""

from __future__ import annotations

from fastapi import Request

from app import config
from app.adapters import registry
from app.contracts import Channel
from app.ports import CatalogEntry
from app.view import ProductCard, build_card

LANG_COOKIE = "craftly_lang"


def language(request: Request) -> str:
    """Resolve the display language: explicit choice, then cookie, then header.

    English is the fallback, not the default in any moral sense — it is
    the one language every generated listing has, because A2 always
    produces `title_en` and fills `title_hi` only when it can.
    """
    chosen = request.query_params.get("lang")
    if chosen in config.LANGUAGES:
        return chosen

    cookie = request.cookies.get(LANG_COOKIE)
    if cookie in config.LANGUAGES:
        return cookie

    header = request.headers.get("accept-language", "")
    for part in header.split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in config.LANGUAGES:
            return code

    return config.DEFAULT_LANGUAGE


def price_card(entry: CatalogEntry, channel: Channel = Channel.OWN_STORE) -> ProductCard:
    return build_card(entry, registry.prices().quote(entry, channel))


def cards(channel: Channel = Channel.OWN_STORE) -> list[ProductCard]:
    return [price_card(entry, channel) for entry in registry.catalog().all_entries()]


def cards_by_id(channel: Channel = Channel.OWN_STORE) -> dict[str, ProductCard]:
    return {card.listing_id: card for card in cards(channel)}


def card(listing_id: str, channel: Channel = Channel.OWN_STORE) -> ProductCard | None:
    entry = registry.catalog().get_entry(listing_id)
    if entry is None:
        return None
    return price_card(entry, channel)
