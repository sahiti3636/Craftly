"""Browse, filter, sort."""

from __future__ import annotations

import pytest

from app import deps
from app.contracts import Channel
from app.search import SearchQuery, Sort, run, score, tokenise


@pytest.fixture
def cards():
    return deps.cards(Channel.OWN_STORE)


def test_empty_query_returns_everything(cards):
    result = run(cards, SearchQuery(per_page=100))
    assert result.total == len(cards)


def test_matches_a_craft_name(cards):
    result = run(cards, SearchQuery(q="ajrakh", per_page=100))
    assert result.total >= 2
    assert all("ajrakh" in card.search_text for card in result.cards)


def test_matches_a_place(cards):
    result = run(cards, SearchQuery(q="kutch", per_page=100))
    assert result.total >= 2
    assert all(card.district == "Kutch" for card in result.cards)


def test_matches_an_artisan_by_name(cards):
    result = run(cards, SearchQuery(q="nirmala", per_page=100))
    assert result.total >= 1
    assert all("Nirmala" in card.artisan_name for card in result.cards)


def test_prefix_matching(cards):
    """Someone who stops typing halfway still gets results."""
    assert run(cards, SearchQuery(q="terra", per_page=100)).total >= 2


def test_nonsense_query_returns_nothing(cards):
    assert run(cards, SearchQuery(q="zzzzqqq", per_page=100)).total == 0


def test_craft_scores_above_description(cards):
    """A listing whose craft is the query beats one that merely mentions it."""
    tokens = tokenise("terracotta")
    by_craft = next(c for c in cards if c.listing.material == "terracotta")
    others = [c for c in cards if c.listing.material != "terracotta"]
    assert all(score(by_craft, tokens) > score(o, tokens) for o in others)


def test_category_filter(cards):
    result = run(cards, SearchQuery(category="textiles", per_page=100))
    assert result.total > 0
    assert all(c.listing.category == "textiles" for c in result.cards)


def test_price_range_filter(cards):
    result = run(cards, SearchQuery(min_price_inr=1000, max_price_inr=3000, per_page=100))
    assert all(1000 <= c.price_inr <= 3000 for c in result.cards)


def test_in_stock_filter_excludes_zero_stock(cards):
    result = run(cards, SearchQuery(in_stock_only=True, per_page=100))
    assert all(c.inventory.sellable_now > 0 for c in result.cards)
    assert result.total < len(cards)  # the seed has a sold-out japi


def test_buyable_only_filter_drops_the_unpriceable(cards):
    result = run(cards, SearchQuery(buyable_only=True, per_page=100))
    assert all(c.sellability.can_buy for c in result.cards)
    assert any(not c.sellability.can_buy for c in cards)


def test_sort_by_price(cards):
    low = run(cards, SearchQuery(sort=Sort.PRICE_LOW, per_page=100)).cards
    high = run(cards, SearchQuery(sort=Sort.PRICE_HIGH, per_page=100)).cards
    assert [c.price_inr for c in low] == sorted(c.price_inr for c in low)
    assert low[0].price_inr <= high[0].price_inr
    assert high[0].price_inr == max(c.price_inr for c in cards)


def test_sort_by_artisan_share(cards):
    result = run(cards, SearchQuery(sort=Sort.ARTISAN_SHARE, per_page=100))
    shares = [c.artisan_share_pct for c in result.cards]
    assert shares == sorted(shares, reverse=True)


def test_paging_does_not_repeat_or_lose_items(cards):
    first = run(cards, SearchQuery(page=1, per_page=5))
    second = run(cards, SearchQuery(page=2, per_page=5))
    ids_1 = {c.listing_id for c in first.cards}
    ids_2 = {c.listing_id for c in second.cards}
    assert len(ids_1) == 5
    assert not (ids_1 & ids_2)
    assert first.pages == second.pages


def test_facets_counted_before_structured_filters(cards):
    """Picking a category must not empty the category list.

    Otherwise a buyer who filters to 'textiles' sees no other category and
    has no way back except the browser button.
    """
    unfiltered = run(cards, SearchQuery(per_page=100)).facets["category"]
    filtered = run(cards, SearchQuery(category="textiles", per_page=100)).facets["category"]
    assert {f.value for f in filtered} == {f.value for f in unfiltered}
    assert any(f.selected for f in filtered)


def test_query_with_replaces_one_field():
    query = SearchQuery(q="pottery", category="home decor", page=3)
    changed = query.with_(category=None)
    assert changed.category is None
    assert changed.q == "pottery"
    assert query.category == "home decor"  # original untouched
