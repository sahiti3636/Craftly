"""Every buyer surface, walked the way a buyer walks it."""

from __future__ import annotations

import re

from app import deps
from app.adapters import registry
from app.adapters.stub_passport import verification_code
from app.contracts import Channel

GOOD = "lst_51ea90cb7d24"       # priced, in stock
NO_FLOOR = "lst_77b0c4d8e913"   # hours_worked is null
BELOW = "lst_9f8a2e1c4b3d"      # market pays under the floor


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_browse_lists_the_catalogue(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Ajrakh" in response.text


def test_browse_search_narrows(client):
    response = client.get("/?q=dhokra")
    assert response.status_code == 200
    assert "Dhokra" in response.text
    assert "Ajrakh Dupatta" not in response.text


def test_browse_facet_link_filters(client):
    response = client.get("/?category=textiles")
    assert response.status_code == 200
    assert "Pattachitra" not in response.text


def test_product_page_shows_price_and_maker(client):
    response = client.get(f"/product/{GOOD}")
    assert response.status_code == 200
    assert "Hansaben Vankar" in response.text
    assert "Wage floor" in response.text
    assert "to the maker" in response.text or "to Hansaben" in response.text


def test_product_page_404(client):
    assert client.get("/product/lst_nope").status_code == 404


def test_unpriceable_product_cannot_be_bought(client):
    response = client.get(f"/product/{NO_FLOOR}")
    assert response.status_code == 200
    assert "Not yet for sale" in response.text
    assert "Add to basket" not in response.text


def test_unpriceable_product_rejected_even_by_direct_post(client):
    """The page hides the button; the handler has to refuse anyway."""
    response = client.post(
        "/cart/add", data={"listing_id": NO_FLOOR, "quantity": 1}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/product/{NO_FLOOR}"
    assert client.get("/cart").text.count("Nothing in the basket") == 1


def test_below_floor_listing_says_so(client):
    response = client.get(f"/product/{BELOW}")
    assert response.status_code == 200
    assert "below what the hours in it are worth" in response.text


def test_cart_flow_add_update_remove(client):
    client.post("/cart/add", data={"listing_id": GOOD, "quantity": 2})
    cart = client.get("/cart")
    assert "Ajrakh Dupatta" in cart.text
    assert 'value="2"' in cart.text

    client.post("/cart/update", data={"listing_id": GOOD, "quantity": 0})
    assert "Nothing in the basket" in client.get("/cart").text


def test_checkout_redirects_when_basket_empty(client):
    response = client.get("/checkout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/cart"


def test_place_an_order_end_to_end(client):
    client.post("/cart/add", data={"listing_id": GOOD, "quantity": 1})
    response = client.post(
        "/checkout",
        data={
            "name": "Meera Iyer",
            "phone": "9876543210",
            "email": "meera@example.com",
            "address": "12 Residency Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pincode": "560025",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    order_id = response.headers["location"].rsplit("/", 1)[-1]

    confirmation = client.get(f"/order/{order_id}")
    assert confirmation.status_code == 200
    assert "Order placed" in confirmation.text
    assert "Ajrakh Dupatta" in confirmation.text

    stored = registry.orders().get(order_id)
    assert stored is not None
    assert stored.buyer.name == "Meera Iyer"
    assert stored.unit_count == 1
    assert stored.artisan_total_inr > 0
    assert stored.artisan_total_inr < stored.total_inr

    # The basket is emptied by the order, not left to be placed twice.
    assert "Nothing in the basket" in client.get("/cart").text


def test_order_404(client):
    assert client.get("/order/ord_nope").status_code == 404


# ---------------------------------------------------------------------------
# B2B
# ---------------------------------------------------------------------------


def test_b2b_catalogue(client):
    response = client.get("/b2b")
    assert response.status_code == 200
    assert "Bulk orders" in response.text


def test_b2b_quote_feasible(client):
    response = client.get(f"/b2b/{BELOW}?quantity=400")
    assert response.status_code == 200
    assert "Who makes it" in response.text
    assert "households" in response.text


def test_b2b_quote_infeasible_offers_a_smaller_number(client):
    response = client.get("/b2b/lst_ba2f6019c7d5?quantity=900")
    assert response.status_code == 200
    assert "What this cluster can actually do" in response.text
    assert "Quote" in response.text


def test_b2b_order_placed(client):
    response = client.post(
        f"/b2b/{BELOW}/order",
        data={
            "quantity": "300",
            "deadline": "",
            "name": "Ravi Menon",
            "organisation": "Taj Hotels",
            "phone": "9812345678",
            "email": "ravi@example.com",
            "address": "Apollo Bunder",
            "city": "Mumbai",
            "state": "Maharashtra",
            "pincode": "400001",
            "notes": "Gift boxes for Diwali",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    order_id = response.headers["location"].rsplit("/", 1)[-1]
    order = registry.orders().get(order_id)
    assert order is not None
    assert order.kind.value == "bulk"
    assert order.lines[0].split is not None
    assert order.lines[0].split.quantity_allocated == 300
    assert "Who is making your" in client.get(f"/order/{order_id}").text


def test_b2b_order_below_minimum_is_rejected(client):
    response = client.post(
        f"/b2b/{BELOW}/order",
        data={
            "quantity": "2",
            "deadline": "",
            "name": "Ravi Menon",
            "organisation": "Taj Hotels",
            "phone": "9812345678",
        },
    )
    assert response.status_code == 409
    assert "Bulk orders start at" in response.text


# ---------------------------------------------------------------------------
# passport and QR
# ---------------------------------------------------------------------------


def test_passport_page_by_code(client):
    code = verification_code(GOOD)
    response = client.get(f"/p/{code}")
    assert response.status_code == 200
    assert "Craft passport verified" in response.text
    assert "Hansaben Vankar" in response.text
    assert code in response.text


def test_passport_page_accepts_a_code_without_dashes(client):
    code = verification_code(GOOD)
    assert client.get(f"/p/{code.replace('-', '')}").status_code == 200


def test_unknown_code_says_it_cannot_verify(client):
    response = client.get("/p/CR-ZZZZ-ZZZZ")
    assert response.status_code == 404
    assert "cannot verify" in response.text


def test_passport_has_a_reorder_route_back_to_the_shop(client):
    code = verification_code(GOOD)
    response = client.get(f"/p/{code}")
    assert f"/product/{GOOD}?ref=qr" in response.text

    reorder = client.get(f"/product/{GOOD}?ref=qr")
    assert "You scanned the tag" in reorder.text


def test_qr_image_renders(client):
    response = client.get(f"/qr/{verification_code(GOOD)}.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_printable_tag(client):
    response = client.get(f"/tag/{GOOD}")
    assert response.status_code == 200
    assert verification_code(GOOD) in response.text


def test_scan_index_lists_every_code(client):
    response = client.get("/scan")
    assert response.status_code == 200
    assert len(re.findall(r"CR-[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}", response.text)) >= 12


def test_reel_directory_traversal_is_refused(client):
    assert client.get("/reels/..%2F..%2F.env").status_code == 404


# ---------------------------------------------------------------------------
# language
# ---------------------------------------------------------------------------


def test_language_switch_sets_a_cookie_and_translates(client):
    client.get("/lang/hi", follow_redirects=False)
    response = client.get(f"/product/{GOOD}")
    assert "अजरख" in response.text


def test_language_falls_back_when_hindi_is_missing(client):
    """A2 fills title_hi when it can. When it cannot, the page shows English."""
    card = deps.card(GOOD, Channel.OWN_STORE)
    card.listing.title_hi = None
    assert card.title("hi") == card.listing.title_en
