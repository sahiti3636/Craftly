"""Placing orders and moving them along."""

from __future__ import annotations

import pytest

from app import orders
from app.contracts import OrderStatus
from app.models import Order
from tests.conftest import order_payload


def test_an_order_is_persisted_with_its_lines(client, artisan_headers, a_listing, session):
    response = client.post("/orders", json=order_payload())
    assert response.status_code == 201
    assert response.json()["status"] == "placed"

    row = session.get(Order, "ord_test_0001")
    assert row.buyer_phone == "9876543210"
    assert len(row.lines) == 1
    assert row.lines[0].unit_price_inr == 500


def test_the_price_on_the_page_is_the_price_stored(client, artisan_headers, a_listing, session):
    """This service does not call B1 to re-check a price.

    The buyer saw a number and agreed to it. A platform that recalculates
    at write time can disagree with its own receipt.
    """
    payload = order_payload()
    payload["lines"][0]["unit_price_inr"] = 12345
    client.post("/orders", json=payload)
    assert session.get(Order, "ord_test_0001").lines[0].unit_price_inr == 12345


def test_a_retried_post_returns_the_same_order(client, artisan_headers, a_listing, session):
    """C1 mints the id, so a retry arrives with the id it already used.
    Placing a second would charge the buyer twice."""
    first = client.post("/orders", json=order_payload()).json()
    second = client.post("/orders", json=order_payload()).json()
    assert first["order_id"] == second["order_id"]
    assert session.query(Order).count() == 1


def test_an_order_for_an_unknown_listing_is_refused_with_a_sentence(client, seeded):
    payload = order_payload(listing_id="lst_nope")
    response = client.post("/orders", json=payload)
    assert response.status_code == 422
    assert "lst_nope" in response.json()["detail"]


def test_an_order_for_a_draft_is_refused(client, artisan_headers):
    client.post(
        "/listings",
        json={"listing": {"listing_id": "lst_notlive", "artisan_id": "x", "title_en": "Draft"}},
        headers=artisan_headers,
    )
    response = client.post("/orders", json=order_payload(listing_id="lst_notlive"))
    assert response.status_code == 422
    assert "not on sale" in response.json()["detail"]


def test_an_empty_order_is_refused(client, seeded):
    response = client.post("/orders", json=order_payload(lines=[]))
    assert response.status_code == 422


def test_a_zero_quantity_line_is_refused(client, artisan_headers, a_listing):
    payload = order_payload()
    payload["lines"][0]["quantity"] = 0
    assert client.post("/orders", json=payload).status_code == 422


def test_an_unknown_order_is_a_404(client, seeded):
    assert client.get("/orders/ord_nope").status_code == 404


# -- auth on placing --------------------------------------------------------


def test_orders_are_open_by_default_because_c1_sends_no_token(client, artisan_headers, a_listing):
    assert client.post("/orders", json=order_payload()).status_code == 201


def test_and_can_be_closed_with_one_setting(client, artisan_headers, a_listing, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "REQUIRE_ORDER_AUTH", True)
    assert client.post("/orders", json=order_payload()).status_code == 401
    assert (
        client.post("/orders", json=order_payload(), headers=artisan_headers).status_code == 201
    )


# -- the status machine -----------------------------------------------------


def test_the_happy_path_runs_forwards(client, artisan_headers, a_listing):
    client.post("/orders", json=order_payload())
    for status in ("accepted", "in_production", "dispatched", "delivered"):
        response = client.post(
            "/orders/ord_test_0001/status", json={"status": status}, headers=artisan_headers
        )
        assert response.status_code == 200, status
        assert response.json()["status"] == status


@pytest.mark.parametrize("bad", ["placed", "accepted", "in_production"])
def test_an_order_does_not_go_backwards(client, artisan_headers, a_listing, bad):
    client.post("/orders", json=order_payload())
    for status in ("accepted", "dispatched", "delivered"):
        client.post(
            "/orders/ord_test_0001/status", json={"status": status}, headers=artisan_headers
        )
    response = client.post(
        "/orders/ord_test_0001/status", json={"status": bad}, headers=artisan_headers
    )
    assert response.status_code == 409


def test_cancelling_is_possible_until_dispatch(client, artisan_headers, a_listing):
    client.post("/orders", json=order_payload())
    cancelled = client.post(
        "/orders/ord_test_0001/status", json={"status": "cancelled"}, headers=artisan_headers
    )
    assert cancelled.status_code == 200


def test_and_not_after(client, artisan_headers, a_listing):
    """Once a courier has the box the question is a return, which is a
    different process with different money in it."""
    client.post("/orders", json=order_payload())
    for status in ("accepted", "dispatched"):
        client.post(
            "/orders/ord_test_0001/status", json={"status": status}, headers=artisan_headers
        )
    response = client.post(
        "/orders/ord_test_0001/status", json={"status": "cancelled"}, headers=artisan_headers
    )
    assert response.status_code == 409


def test_every_step_is_written_down(client, artisan_headers, a_listing, session):
    client.post("/orders", json=order_payload())
    for status in ("accepted", "dispatched", "delivered"):
        client.post(
            "/orders/ord_test_0001/status", json={"status": status}, headers=artisan_headers
        )
    order = session.get(Order, "ord_test_0001")
    assert [event.status for event in order.events] == [
        "placed",
        "accepted",
        "dispatched",
        "delivered",
    ]


def test_changing_status_needs_a_token(client, artisan_headers, a_listing):
    client.post("/orders", json=order_payload())
    assert client.post("/orders/ord_test_0001/status", json={"status": "accepted"}).status_code == 401


# -- the artisan's own view -------------------------------------------------


def test_an_artisan_sees_orders_she_has_work_on(client, artisan_headers, a_listing):
    client.post("/orders", json=order_payload())
    mine = client.get("/orders/mine/artisan", headers=artisan_headers).json()
    assert [order["order_id"] for order in mine] == ["ord_test_0001"]


def test_she_sees_a_bulk_order_she_is_only_a_member_of(
    client, artisan_headers, artisan_id, a_listing, seeded, session
):
    """On a split order, nine of the ten women with work to do do not own
    the listing it was ordered from."""
    from app.models import Artisan

    other = session.query(Artisan).filter(Artisan.artisan_id != artisan_id).first()
    payload = order_payload(order_id="ord_bulk", kind="bulk")
    payload["lines"][0]["split"] = {
        "listing_id": "lst_test_complete",
        "quantity_requested": 2,
        "quantity_allocated": 2,
        "allocations": [
            {
                "artisan_id": other.artisan_id,
                "artisan_name": other.name,
                "quantity": 2,
                "take_home_inr": 380,
            }
        ],
    }
    client.post("/orders", json=payload)

    # The listing's owner is not in the split, so it is not her order.
    assert client.get("/orders/mine/artisan", headers=artisan_headers).json() == []
    assert len(orders.for_artisan(session, other.artisan_id)) == 1


def test_the_split_plan_survives_the_round_trip(client, artisan_headers, a_listing, seeded, session):
    from app.models import Artisan

    other = session.query(Artisan).first()
    payload = order_payload(order_id="ord_rt", kind="bulk")
    payload["lines"][0]["split"] = {
        "listing_id": "lst_test_complete",
        "quantity_requested": 2,
        "quantity_allocated": 2,
        "feasible": True,
        "notes": ["spread across the cluster"],
        "allocations": [
            {
                "artisan_id": other.artisan_id,
                "artisan_name": other.name,
                "quantity": 2,
                "take_home_inr": 380,
            }
        ],
    }
    client.post("/orders", json=payload)
    stored = client.get("/orders/ord_rt").json()
    assert stored["lines"][0]["split"]["notes"] == ["spread across the cluster"]
    assert stored["lines"][0]["split"]["allocations"][0]["quantity"] == 2


def test_transitions_table_has_no_dead_ends_that_should_not_be(client):
    assert orders.TRANSITIONS[OrderStatus.DELIVERED] == set()
    assert orders.TRANSITIONS[OrderStatus.CANCELLED] == set()
    assert OrderStatus.DELIVERED in orders.TRANSITIONS[OrderStatus.DISPATCHED]
