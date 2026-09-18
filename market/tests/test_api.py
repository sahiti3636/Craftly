"""The JSON surface A1's app and C2's adapters read."""

from __future__ import annotations

from app.adapters.stub_passport import verification_code

GOOD = "lst_51ea90cb7d24"
NO_FLOOR = "lst_77b0c4d8e913"


def test_products_listing(client):
    body = client.get("/api/products?per_page=50").json()
    assert body["total"] >= 12
    first = body["products"][0]
    assert first["listing"]["listing_id"]
    assert first["price"]["floor_inr"] <= first["price"]["price_inr"]
    assert 0 <= first["artisan_share_pct"] <= 100


def test_products_search_and_filter(client):
    body = client.get("/api/products?q=terracotta&per_page=50").json()
    assert body["total"] >= 2
    assert all(
        "terracotta" in (p["listing"]["material"] or "").lower()
        or "terracotta" in (p["listing"]["title_en"] or "").lower()
        for p in body["products"]
    )


def test_product_by_id_carries_the_passport_code(client):
    body = client.get(f"/api/products/{GOOD}").json()
    assert body["passport_code"] == verification_code(GOOD)
    assert body["can_buy"] is True


def test_unbuyable_product_says_why(client):
    body = client.get(f"/api/products/{NO_FLOOR}").json()
    assert body["can_buy"] is False
    assert "hours of work" in body["blocked_reason"]
    assert body["listing"]["hours_worked"] is None


def test_channel_pricing_for_c2_adapters(client):
    """C2 pushes listings to marketplaces and needs that channel's price."""
    own = client.get(f"/api/products/{GOOD}?channel=own_store").json()
    amazon = client.get(f"/api/products/{GOOD}?channel=amazon").json()
    assert amazon["price"]["price_inr"] > own["price"]["price_inr"]
    assert amazon["price"]["artisan_take_home_inr"] == own["price"]["artisan_take_home_inr"]


def test_passport_endpoint(client):
    code = verification_code(GOOD)
    body = client.get(f"/api/passports/{code}").json()
    assert body["listing_id"] == GOOD
    assert body["artisan"]["name"] == "Hansaben Vankar"
    assert len(body["chain"]) >= 3


def test_passport_404(client):
    assert client.get("/api/passports/CR-0000-0000").status_code == 404


def test_bulk_quote_endpoint(client):
    body = client.get("/api/bulk/quote?listing_id=lst_9f8a2e1c4b3d&quantity=500").json()
    assert body["can_order"] is True
    assert body["split"]["quantity_allocated"] == 500
    assert body["artisan_total_inr"] > 0


def test_bulk_quote_reports_infeasibility(client):
    body = client.get("/api/bulk/quote?listing_id=lst_ba2f6019c7d5&quantity=900").json()
    assert body["can_order"] is False
    assert body["split"]["shortfall"] > 0
    assert body["max_by_deadline"] is not None or body["split"]["quantity_allocated"] > 0


def test_bulk_quote_404(client):
    assert client.get("/api/bulk/quote?listing_id=nope&quantity=50").status_code == 404
