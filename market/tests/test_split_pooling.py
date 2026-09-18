"""Capacity pooling: the order spreads across the cluster, it does not pile up.

Separate from `test_bulk.py` because these assert the *shape* of a split
rather than whether an order is possible, and because the behaviour is
easy to regress into "give it all to the first artisan", which passes a
naive feasibility test while being wrong for everyone involved.
"""

from __future__ import annotations

import pytest

from app.adapters.stub_engine import MIN_SHARE, StubOrderEngine

TERRACOTTA = "lst_9f8a2e1c4b3d"  # Panchmura cluster, three artisans
SOLO = "lst_16d8f720a49c"        # Pedana, one artisan, no cluster peers listed


@pytest.fixture
def engine(catalog, prices):
    return StubOrderEngine(catalog, prices)


def test_a_large_order_is_shared_not_dumped(catalog, engine):
    """One artisan with the capacity to do it alone should still not do it alone."""
    entry = catalog.get_entry(TERRACOTTA)
    assert entry.inventory.monthly_capacity >= 500  # she could, on paper

    plan = engine.split(entry, 500)
    assert plan.feasible
    assert len(plan.allocations) >= 2
    assert max(a.quantity for a in plan.allocations) < 500


def test_the_listing_artisan_gets_the_largest_share(catalog, engine):
    entry = catalog.get_entry(TERRACOTTA)
    plan = engine.split(entry, 500)
    primary = plan.allocations[0]
    assert primary.artisan_id == entry.artisan.artisan_id
    assert primary.quantity == max(a.quantity for a in plan.allocations)


def test_shares_sum_to_the_order(catalog, engine):
    entry = catalog.get_entry(TERRACOTTA)
    for quantity in (10, 47, 250, 500, 1200):
        plan = engine.split(entry, quantity)
        assert sum(a.quantity for a in plan.allocations) == plan.quantity_allocated
        assert plan.quantity_allocated + plan.shortfall == quantity


def test_nobody_gets_a_share_too_small_to_pack(catalog, engine):
    entry = catalog.get_entry(TERRACOTTA)
    plan = engine.split(entry, 40)
    assert len(plan.allocations) <= max(1, 40 // MIN_SHARE)
    assert all(a.quantity > 0 for a in plan.allocations)


def test_spreading_the_work_shortens_the_lead_time(catalog, engine):
    """The practical argument for pooling, asserted.

    The same 500 units through one pair of hands takes longer than through
    three. If this ever inverts, the splitter has stopped pooling.
    """
    entry = catalog.get_entry(TERRACOTTA)
    shared = engine.split(entry, 500)

    solo_days = engine._days_for(  # noqa: SLF001 - asserting against the same maths
        500,
        {
            "monthly": entry.inventory.monthly_capacity,
            "lead": entry.inventory.lead_time_days,
            "stock": entry.inventory.sellable_now,
        },
    )
    assert shared.lead_time_days < solo_days


def test_an_artisan_with_no_cluster_keeps_her_own_order(catalog, engine):
    entry = catalog.get_entry(SOLO)
    plan = engine.split(entry, 20)
    assert len(plan.allocations) == 1
    assert plan.allocations[0].artisan_id == entry.artisan.artisan_id


def test_an_order_beyond_the_cluster_is_capped_and_reports_the_shortfall(catalog, engine):
    """The cluster is not infinitely elastic, and saying so is the feature.

    Not asserted per-artisan, because some cluster members have no listing
    of their own and their capacity is an estimate off the cluster total —
    the invariant that matters to a buyer is that the plan stops at what
    the village can do and names the gap.
    """
    entry = catalog.get_entry(TERRACOTTA)
    # A month of the cluster's production, plus everything already made and
    # sitting in the village.
    peers = catalog.cluster_entries(entry.artisan.cluster_id, entry.listing.craft_type)
    ceiling = entry.inventory.cluster_capacity + sum(p.inventory.sellable_now for p in peers)

    plan = engine.split(entry, 4000)
    assert not plan.feasible
    assert plan.quantity_allocated <= ceiling
    assert plan.shortfall == 4000 - plan.quantity_allocated
    assert plan.shortfall > 0
    assert any(str(plan.quantity_allocated) in note for note in plan.notes)
    assert all(a.quantity <= ceiling for a in plan.allocations)


def test_tight_deadline_excludes_slow_artisans(catalog, engine):
    entry = catalog.get_entry(TERRACOTTA)
    relaxed = engine.split(entry, 300, deadline_days=60)
    urgent = engine.split(entry, 300, deadline_days=3)
    assert urgent.quantity_allocated <= relaxed.quantity_allocated
