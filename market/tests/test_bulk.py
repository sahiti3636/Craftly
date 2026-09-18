"""The B2B quote, and the refusals it has to be able to make."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import bulk
from app.adapters import registry
from app.adapters.stub_engine import StubOrderEngine

TODAY = date(2026, 9, 18)


@pytest.fixture
def engine(catalog, prices):
    return StubOrderEngine(catalog, prices)


def quote_for(entry, engine, prices, quantity, deadline=None):
    return bulk.quote(
        entry,
        quantity=quantity,
        prices=prices,
        engine=engine,
        deadline=deadline,
        today=TODAY,
    )


def test_small_order_is_refused_with_a_reason(entry, engine, prices):
    quoted = quote_for(entry, engine, prices, 3)
    assert not quoted.can_order
    assert any(str(bulk.MIN_BULK_QUANTITY) in e for e in quoted.errors)


def test_feasible_order_names_the_households(catalog, engine, prices):
    entry = catalog.get_entry("lst_9f8a2e1c4b3d")  # terracotta, big cluster
    quoted = quote_for(entry, engine, prices, 500)
    assert quoted.can_order
    assert quoted.split.quantity_allocated == 500
    assert quoted.artisans_involved >= 2
    assert sum(a.quantity for a in quoted.split.allocations) == 500


def test_impossible_quantity_returns_a_number_not_an_error(catalog, engine, prices):
    """The refusal a procurement officer can act on."""
    entry = catalog.get_entry("lst_ba2f6019c7d5")  # one artisan, 5 a month
    quoted = quote_for(entry, engine, prices, 400, deadline=TODAY + timedelta(days=30))
    assert not quoted.can_order
    assert quoted.split.shortfall > 0
    assert quoted.max_by_deadline is not None
    assert quoted.max_by_deadline < 400


def test_deadline_shrinks_what_a_cluster_can_commit(catalog, engine, prices):
    entry = catalog.get_entry("lst_9f8a2e1c4b3d")
    loose = bulk.max_feasible_quantity(entry, engine, None)
    tight = bulk.max_feasible_quantity(entry, engine, 7)
    assert tight < loose


def test_past_deadline_is_rejected(entry, engine, prices):
    quoted = quote_for(entry, engine, prices, 50, deadline=TODAY - timedelta(days=2))
    assert not quoted.can_order
    assert any("passed" in e for e in quoted.errors)


def test_unpriceable_listing_cannot_be_quoted(entry_without_hours, engine, prices):
    quoted = quote_for(entry_without_hours, engine, prices, 100)
    assert not quoted.can_order
    assert any("wage floor" in e for e in quoted.errors)


def test_bulk_is_cheaper_per_unit_than_retail(catalog, engine, prices):
    entry = catalog.get_entry("lst_c40b92e7da1a")
    quoted = quote_for(entry, engine, prices, 500)
    assert quoted.unit_price_inr < quoted.retail_unit_price_inr
    assert quoted.saving_pct > 0


def test_artisan_total_is_per_unit_take_home_times_quantity(catalog, engine, prices):
    entry = catalog.get_entry("lst_5b8e17cd240a")
    quoted = quote_for(entry, engine, prices, 200)
    assert quoted.artisan_total_inr == quoted.artisan_take_home_inr * 200
    assert quoted.artisan_total_inr < quoted.subtotal_inr


def test_split_respects_stock_and_capacity(catalog, engine, prices):
    entry = catalog.get_entry("lst_3c7d1b9e5a02")
    plan = engine.split(entry, 20, deadline_days=None)
    for allocation in plan.allocations:
        assert allocation.quantity > 0
    assert plan.quantity_allocated == sum(a.quantity for a in plan.allocations)


def test_listing_artisan_is_offered_the_work_first(catalog, engine, prices):
    entry = catalog.get_entry("lst_9f8a2e1c4b3d")
    plan = engine.split(entry, 300, deadline_days=None)
    assert plan.allocations[0].artisan_id == entry.artisan.artisan_id


def test_zero_quantity_is_not_feasible(entry, engine):
    plan = engine.split(entry, 0)
    assert not plan.feasible
    assert plan.quantity_allocated == 0


def test_households_line_reads_as_a_sentence(catalog, engine, prices):
    entry = catalog.get_entry("lst_9f8a2e1c4b3d")
    quoted = quote_for(entry, engine, prices, 800)
    assert "household" in quoted.households_line
