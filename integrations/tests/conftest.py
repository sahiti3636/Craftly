"""Hermetic fixtures: no C1 process, no seed files, no network.

`fake_c1` replaces the bridge's product and artisan lookups with two small
fixtures, so these tests pin C2's own behaviour. The one test that touches
the real C1 is `test_orders.py::test_parity_with_c1_order`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from c2 import c1_client, config, courier, voice
from c2.models import Order

ARTISANS = {
    "art_a": {"artisan_id": "art_a", "name": "Asha", "village": "Panchmura", "state": "West Bengal",
              "languages": ["bn", "hi"], "cluster_id": "clu_1"},
    "art_b": {"artisan_id": "art_b", "name": "Bela", "village": "Panchmura", "state": "West Bengal",
              "languages": ["bn"], "cluster_id": "clu_1"},
    "art_c": {"artisan_id": "art_c", "name": "Chitra", "village": "Kutch", "state": "Gujarat",
              "languages": ["gu", "en"], "cluster_id": "clu_2"},
}


def product(listing_id="lst_1", artisan_id="art_a", *, price=310, take_home=250, in_stock=50,
            lead=12, floor_incomplete=False, can_buy=True, below_floor=False, with_price=True):
    return {
        "listing": {"listing_id": listing_id, "artisan_id": artisan_id, "title_en": f"Title {listing_id}",
                    "title_hi": f"शीर्षक {listing_id}", "description_en": "desc", "material": "clay",
                    "craft_type": "pottery"},
        "inventory": {"listing_id": listing_id, "in_stock": in_stock, "lead_time_days": lead},
        "price": {
            "listing_id": listing_id, "price_inr": price, "artisan_take_home_inr": take_home,
            "channel_fee_inr": price - take_home, "floor_incomplete": floor_incomplete,
            "below_floor": below_floor,
        } if with_price else None,
        "artisan_id": artisan_id,
        "artisan_name": ARTISANS[artisan_id]["name"],
        "place": "Panchmura, West Bengal",
        "image_url": "/media/x.jpg",
        "can_buy": can_buy,
        "blocked_reason": None if can_buy else "Not buyable",
    }


@pytest.fixture(autouse=True)
def fake_c1(monkeypatch, tmp_path):
    products = {
        "lst_1": product("lst_1", "art_a"),
        "lst_2": product("lst_2", "art_c", in_stock=2, lead=10),
        "lst_3": product("lst_3", "art_a"),  # bulk sample: art_a's cluster has two members
        "lst_4": product("lst_4", "art_c"),
    }

    def get_product(listing_id, channel="own_store"):
        if listing_id not in products:
            raise KeyError(listing_id)
        return products[listing_id], "c1_http"

    monkeypatch.setattr(c1_client, "get_product", get_product)
    monkeypatch.setattr(c1_client, "seed_artisans", lambda: ARTISANS)
    monkeypatch.setattr(c1_client, "seed_listings", lambda: {k: v["listing"] for k, v in products.items()})
    monkeypatch.setattr(c1_client, "all_listing_ids", lambda: list(products))
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "TODAY_OVERRIDE", "2026-09-16")  # a Wednesday
    courier.reset()
    voice.reset()
    return products


def make_order(order_id="ord_1", *, lines=None, state="Karnataka",
               created=datetime(2026, 9, 15, 10, tzinfo=timezone.utc), status="placed", kind="retail"):
    lines = lines or [{"listing_id": "lst_1", "title": "Title lst_1", "quantity": 2,
                       "unit_price_inr": 310, "artisan_take_home_inr": 250}]
    return Order.model_validate({
        "order_id": order_id, "kind": kind, "status": status,
        "buyer": {"name": "Meera", "phone": "+91 90000 00000", "city": "Bengaluru", "state": state,
                  "pincode": "560034", "address": "14 Cross"},
        "lines": lines, "created_at": created,
    })
