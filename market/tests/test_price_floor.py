"""The floor rules, asserted against the placeholder engine.

These tests are written against behaviour B1 must also have, not against
the stub's arithmetic. When B1 replaces `stub_price.py`, this file should
keep passing — if it does not, either B1 broke a rule or a rule changed
on purpose and this file is where that gets recorded.
"""

from __future__ import annotations

from app.adapters.stub_price import (
    DAILY_MINIMUM_WAGE_INR,
    HOURS_PER_DAY,
    StubPriceEngine,
    volume_discount,
)
from app.contracts import Channel


def test_price_never_below_floor(catalog, prices):
    for entry in catalog.all_entries():
        for channel in Channel:
            quote = prices.quote(entry, channel)
            assert quote.price_inr >= quote.floor_inr, (
                f"{entry.listing_id} on {channel.value} priced below its own floor"
            )


def test_floor_covers_materials_and_minimum_wage(entry, prices):
    quote = prices.quote(entry)
    listing, state = entry.listing, entry.artisan.state
    hourly = DAILY_MINIMUM_WAGE_INR[state] / HOURS_PER_DAY
    bare_minimum = listing.material_cost_inr + listing.hours_worked * hourly
    assert quote.floor_inr >= bare_minimum


def test_missing_hours_flags_an_incomplete_floor(entry_without_hours, prices):
    quote = prices.quote(entry_without_hours)
    assert entry_without_hours.listing.hours_worked is None
    assert quote.floor_incomplete is True


def test_missing_number_is_never_treated_as_zero_work(entry_without_hours, prices):
    """A null must not quietly become a floor of just the material cost.

    The engine still computes a number — a page cannot explain a refusal
    with no number at all — but it must mark it untrustworthy, and
    `app/view.py` must then refuse the sale. This is the single most
    important invariant in the buyer surfaces.
    """
    from app.view import build_card

    card = build_card(entry_without_hours, prices.quote(entry_without_hours))
    assert card.sellability.can_buy is False
    assert "hours" in (card.sellability.detail or "")


def test_market_below_floor_is_reported_not_matched(entry_below_market, prices):
    quote = prices.quote(entry_below_market)
    assert quote.market_high_inr < quote.floor_inr
    assert quote.below_floor is True
    assert quote.price_inr >= quote.floor_inr
    assert any("market" in line.lower() for line in quote.explanation)


def test_channel_cut_moves_the_price_not_the_take_home(entry, prices):
    """The artisan's take-home is the constant across channels."""
    own = prices.quote(entry, Channel.OWN_STORE)
    amazon = prices.quote(entry, Channel.AMAZON)

    assert own.artisan_take_home_inr == amazon.artisan_take_home_inr
    assert amazon.price_inr > own.price_inr
    assert amazon.channel_fee_inr > own.channel_fee_inr


def test_volume_discount_comes_out_of_margin_never_the_floor(entry, prices):
    single = prices.quote(entry, Channel.B2B, 1)
    bulk = prices.quote(entry, Channel.B2B, 500)

    assert volume_discount(500) > 0
    assert bulk.price_inr < single.price_inr
    assert bulk.artisan_take_home_inr >= bulk.floor_inr
    assert bulk.floor_inr == single.floor_inr


def test_huge_volume_cannot_discount_below_the_floor(catalog):
    prices = StubPriceEngine()
    for entry in catalog.all_entries():
        quote = prices.quote(entry, Channel.B2B, 10_000)
        assert quote.artisan_take_home_inr >= quote.floor_inr


def test_explanation_says_where_the_money_goes(entry, prices):
    quote = prices.quote(entry)
    joined = " ".join(quote.explanation).lower()
    assert "wage floor" in joined
    assert "artisan receives" in joined
