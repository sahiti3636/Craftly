"""The payment split. The tests that matter most in this slice.

Each one pins a rule from `app/payments.py`. If one of these goes red,
somebody is about to be underpaid.
"""

from __future__ import annotations

import pytest

from app import config, orders, payments
from app.models import Artisan, Payout
from tests.conftest import order_payload


def _deliver(client, headers, order_id="ord_test_0001"):
    for status in ("accepted", "dispatched", "delivered"):
        client.post(f"/orders/{order_id}/status", json={"status": status}, headers=headers)


# -- Rule 1: the artisan's take-home is not a residual -----------------------


def test_artisan_take_home_is_paid_exactly_as_quoted(client, artisan_headers, a_listing):
    client.post("/orders", json=order_payload())
    _deliver(client, artisan_headers)

    settlement = client.get("/orders/ord_test_0001/settlement").json()
    # 2 units at a quoted take-home of 380.
    assert settlement["artisan_total_inr"] == 760
    assert settlement["payouts"][0]["amount_inr"] == 760
    assert settlement["payouts"][0]["unit_take_home_inr"] == 380


def test_platform_fee_is_whatever_is_left_after_the_artisan(client, artisan_headers, a_listing):
    client.post("/orders", json=order_payload())
    _deliver(client, artisan_headers)

    settlement = client.get("/orders/ord_test_0001/settlement").json()
    gross, artisan = settlement["gross_inr"], settlement["artisan_total_inr"]
    # The fee is a slice of the margin, never of the gross.
    assert settlement["platform_fee_inr"] <= gross - artisan
    assert settlement["artisan_share_pct"] == 76.0


# -- Rule 2: the fee comes out of the margin, and the margin can be zero -----


def test_fee_is_taken_from_the_margin_not_the_price():
    fee, absorbed = payments.platform_fee_for(price_inr=500, take_home_inr=380)
    assert fee == int(120 * config.PLATFORM_FEE_PCT)
    assert absorbed == 0


def test_no_margin_means_no_fee_and_the_marketplace_absorbs_it():
    """The diya case: the market pays less than the wage floor.

    The artisan still gets her floor. The marketplace eats the difference
    and the difference is recorded rather than netted out of her payout.
    """
    fee, absorbed = payments.platform_fee_for(price_inr=100, take_home_inr=140)
    assert fee == 0
    assert absorbed == 40


def test_absorbed_loss_shows_up_on_the_settlement(client, artisan_headers, a_listing):
    payload = order_payload()
    payload["lines"][0]["unit_price_inr"] = 100
    payload["lines"][0]["artisan_take_home_inr"] = 140
    client.post("/orders", json=payload)
    _deliver(client, artisan_headers)

    settlement = client.get("/orders/ord_test_0001/settlement").json()
    assert settlement["platform_fee_inr"] == 0
    assert settlement["platform_absorbed_inr"] == 80  # 40 a unit, 2 units
    assert settlement["artisan_total_inr"] == 280  # she is still paid in full


# -- Rule 3: a payout we cannot send is blocked, never dropped ---------------


def test_missing_payout_destination_blocks_but_keeps_the_amount(
    client, artisan_headers, artisan_id, a_listing, session
):
    artisan = session.get(Artisan, artisan_id)
    artisan.payout_upi = None
    session.commit()

    client.post("/orders", json=order_payload())
    _deliver(client, artisan_headers)

    settlement = client.get("/orders/ord_test_0001/settlement").json()
    payout = settlement["payouts"][0]
    assert payout["status"] == "blocked"
    assert payout["amount_inr"] == 760  # the debt is recorded in full
    assert "destination" in payout["blocked_reason"]
    assert settlement["complete"] is False


def test_blocked_payout_cannot_be_marked_paid(client, artisan_headers, artisan_id, a_listing, session):
    session.get(Artisan, artisan_id).payout_upi = None
    session.commit()
    client.post("/orders", json=order_payload())
    _deliver(client, artisan_headers)

    payout_id = client.get("/payouts/mine", headers=artisan_headers).json()[0]["payout_id"]
    response = client.post(f"/payouts/{payout_id}/paid", headers=artisan_headers)
    assert response.status_code == 409


# -- Rule 4: settlement is idempotent ---------------------------------------


def test_settling_twice_does_not_pay_twice(client, artisan_headers, a_listing, session):
    client.post("/orders", json=order_payload())
    _deliver(client, artisan_headers)

    order = orders.get(session, "ord_test_0001")
    payments.settle(session, order)
    payments.settle(session, order)
    session.commit()

    rows = session.query(Payout).filter(Payout.order_id == "ord_test_0001").all()
    assert len(rows) == 1
    assert sum(r.amount_inr for r in rows) == 760


def test_a_repeated_delivery_webhook_is_not_an_error(client, artisan_headers, a_listing):
    client.post("/orders", json=order_payload())
    _deliver(client, artisan_headers)
    again = client.post(
        "/orders/ord_test_0001/status", json={"status": "delivered"}, headers=artisan_headers
    )
    assert again.status_code == 200
    assert len(client.get("/payouts/mine", headers=artisan_headers).json()) == 1


# -- Rule 5: an incomplete line blocks the split, it does not guess it -------


def test_missing_take_home_blocks_rather_than_guessing(client, artisan_headers, a_listing):
    payload = order_payload()
    payload["lines"][0]["artisan_take_home_inr"] = None
    client.post("/orders", json=payload)
    _deliver(client, artisan_headers)

    settlement = client.get("/orders/ord_test_0001/settlement").json()
    assert settlement["complete"] is False
    assert settlement["artisan_total_inr"] == 0
    payout = settlement["payouts"][0]
    assert payout["status"] == "blocked"
    assert payout["amount_inr"] == 0
    assert any("wage floor" in p for p in settlement["problems"])


# -- Bulk splits ------------------------------------------------------------


def test_a_bulk_line_pays_every_artisan_in_the_split(
    client, artisan_headers, a_listing, seeded, session
):
    """500 units across three women: three payouts, not one."""
    others = session.query(Artisan).limit(3).all()
    payload = order_payload(order_id="ord_bulk_1", kind="bulk")
    payload["lines"][0].update(
        quantity=500,
        unit_price_inr=400,
        artisan_take_home_inr=300,
        split={
            "listing_id": "lst_test_complete",
            "quantity_requested": 500,
            "quantity_allocated": 500,
            "allocations": [
                {
                    "artisan_id": a.artisan_id,
                    "artisan_name": a.name,
                    "quantity": q,
                    "take_home_inr": 300,
                }
                for a, q in zip(others, (200, 180, 120))
            ],
        },
    )
    client.post("/orders", json=payload)
    _deliver(client, artisan_headers, "ord_bulk_1")

    settlement = client.get("/orders/ord_bulk_1/settlement").json()
    assert len(settlement["payouts"]) == 3
    assert sum(p["amount_inr"] for p in settlement["payouts"]) == 500 * 300
    assert settlement["gross_inr"] == 500 * 400


def test_a_retail_line_still_produces_one_allocation(client, artisan_headers, a_listing, session):
    """The implicit single maker is written down, so payout has one shape."""
    client.post("/orders", json=order_payload())
    order = orders.get(session, "ord_test_0001")
    assert len(order.lines[0].allocations) == 1


def test_allocation_mismatch_is_reported_and_the_rest_still_paid(
    client, artisan_headers, a_listing, seeded, session
):
    artisan = session.query(Artisan).first()
    payload = order_payload(order_id="ord_short_1")
    payload["lines"][0].update(
        quantity=10,
        split={
            "listing_id": "lst_test_complete",
            "quantity_requested": 10,
            "quantity_allocated": 6,
            "allocations": [
                {
                    "artisan_id": artisan.artisan_id,
                    "artisan_name": artisan.name,
                    "quantity": 6,
                    "take_home_inr": 380,
                }
            ],
        },
    )
    client.post("/orders", json=payload)
    _deliver(client, artisan_headers, "ord_short_1")

    settlement = client.get("/orders/ord_short_1/settlement").json()
    assert settlement["artisan_total_inr"] == 6 * 380  # paid for what was allocated
    assert any("allocated" in p for p in settlement["problems"])


# -- Preview and the real thing agree ---------------------------------------


def test_preview_matches_what_is_actually_settled(client, artisan_headers, a_listing):
    client.post("/orders", json=order_payload())
    before = client.get("/orders/ord_test_0001/settlement").json()
    assert before["settled"] is False

    _deliver(client, artisan_headers)
    after = client.get("/orders/ord_test_0001/settlement").json()

    assert after["settled"] is True
    for field in ("gross_inr", "artisan_total_inr", "platform_fee_inr", "artisan_share_pct"):
        assert before[field] == after[field], field


def test_share_pct_is_none_rather_than_zero_when_nothing_was_sold(session):
    settlement = payments.Settlement(order_id="ord_empty")
    assert settlement.artisan_share_pct is None


@pytest.mark.parametrize("price,take_home,expected_fee", [(1000, 500, 50), (100, 100, 0), (100, 120, 0)])
def test_fee_is_floored_so_rounding_never_costs_the_artisan(price, take_home, expected_fee):
    fee, _ = payments.platform_fee_for(price, take_home)
    assert fee == expected_fee
    assert take_home + fee <= max(price, take_home)
