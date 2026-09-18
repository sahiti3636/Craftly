"""The basket cookie, and Indian rupee formatting."""

from __future__ import annotations

from app import cart, deps
from app.contracts import Channel
from app.money import group_inr, rupees

GOOD = "lst_51ea90cb7d24"
NO_FLOOR = "lst_77b0c4d8e913"


# -- money -----------------------------------------------------------------


def test_indian_digit_grouping():
    assert group_inr(999) == "999"
    assert group_inr(1234) == "1,234"
    assert group_inr(12345) == "12,345"
    assert group_inr(123456) == "1,23,456"
    assert group_inr(1234567) == "12,34,567"
    assert group_inr(12345678) == "1,23,45,678"


def test_rupees_renders_a_symbol():
    assert rupees(1234) == "₹1,234"


def test_missing_price_is_a_dash_not_zero():
    """A price of zero is a claim; a missing price is a question."""
    assert rupees(None) == "—"
    assert rupees(0) == "₹0"


# -- cart cookie -----------------------------------------------------------


def test_round_trip():
    raw = cart.write({"lst_a": 2, "lst_b": 1})
    assert cart.read(raw) == {"lst_a": 2, "lst_b": 1}


def test_tampered_cookie_is_discarded_not_trusted():
    raw = cart.write({"lst_a": 2})
    assert cart.read(raw[:-3] + "xyz") == {}


def test_garbage_cookie_is_an_empty_basket():
    assert cart.read("not-a-cookie") == {}
    assert cart.read(None) == {}


def test_quantities_are_clamped():
    raw = cart.write({"lst_a": 5000, "lst_b": -1})
    assert cart.read(raw) == {}


def test_add_accumulates_and_caps():
    items = cart.add({}, "lst_a", 3)
    items = cart.add(items, "lst_a", 2)
    assert items["lst_a"] == 5
    assert cart.add(items, "lst_a", 500)["lst_a"] == cart.MAX_QUANTITY_PER_LINE


def test_setting_zero_removes_the_line():
    assert cart.set_quantity({"lst_a": 2}, "lst_a", 0) == {}


# -- building a cart -------------------------------------------------------


def test_unsellable_items_are_dropped_with_a_reason():
    cards = deps.cards_by_id(Channel.OWN_STORE)
    basket = cart.build({GOOD: 1, NO_FLOOR: 2}, cards)

    assert [line.card.listing_id for line in basket.lines] == [GOOD]
    assert len(basket.removed) == 1
    assert "hours of work" in basket.removed[0][1]


def test_vanished_listing_is_dropped_with_a_reason():
    basket = cart.build({"lst_gone": 1}, deps.cards_by_id(Channel.OWN_STORE))
    assert basket.is_empty
    assert "no longer listed" in basket.removed[0][1]


def test_totals_and_artisan_share():
    cards = deps.cards_by_id(Channel.OWN_STORE)
    basket = cart.build({GOOD: 2}, cards)
    card = cards[GOOD]

    assert basket.count == 2
    assert basket.total_inr == card.price_inr * 2
    assert basket.artisan_total_inr == card.quote.artisan_take_home_inr * 2
    assert basket.artisan_total_inr < basket.total_inr
    assert basket.artisan_count == 1


def test_cart_holds_no_prices():
    """Prices are re-fetched every render, so a stale basket cannot underpay."""
    raw = cart.write({GOOD: 1})
    assert "price" not in raw.lower()
    assert set(cart.read(raw).keys()) == {GOOD}
