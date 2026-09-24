"""What C2 (integrations) can do with CRAFTLY_SERVICE_TOKEN, and cannot without it."""

from __future__ import annotations

import pytest

from app import config
from tests.conftest import order_payload

SERVICE = "svc-test-token-0123456789"


@pytest.fixture
def service_headers(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_TOKEN", SERVICE)
    return {"Authorization": f"Bearer {SERVICE}"}


def test_the_service_lists_every_order(client, artisan_headers, a_listing, service_headers):
    client.post("/orders", json=order_payload())
    response = client.get("/orders", headers=service_headers)
    assert response.status_code == 200
    assert [o["order_id"] for o in response.json()] == ["ord_test_0001"]


def test_the_order_list_filters_by_status(client, artisan_headers, a_listing, service_headers):
    client.post("/orders", json=order_payload())
    assert client.get("/orders?status=accepted", headers=service_headers).json() == []
    assert len(client.get("/orders?status=placed", headers=service_headers).json()) == 1


def test_the_order_list_is_not_public(client, artisan_headers, a_listing, service_headers):
    """It is every buyer's name, phone and address."""
    client.post("/orders", json=order_payload())
    assert client.get("/orders").status_code == 401
    assert client.get("/orders", headers=artisan_headers).status_code == 403


def test_a_wrong_service_token_is_just_a_bad_token(client, service_headers):
    assert client.get("/orders", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_no_service_token_configured_means_no_service_access(client, monkeypatch):
    monkeypatch.setattr(config, "SERVICE_TOKEN", "")
    assert client.get("/orders", headers={"Authorization": "Bearer "}).status_code == 401


def test_the_courier_integration_delivers_and_that_settles(client, artisan_headers, a_listing, service_headers):
    client.post("/orders", json=order_payload())
    client.post("/orders/ord_test_0001/status", json={"status": "accepted"}, headers=artisan_headers)
    for status in ("dispatched", "delivered"):
        response = client.post("/orders/ord_test_0001/status", json={"status": status}, headers=service_headers)
        assert response.status_code == 200, response.text
    settlement = client.get("/orders/ord_test_0001/settlement").json()
    assert settlement["settled"] is True
    assert settlement["payouts"]


def test_an_artisan_profile_is_public(client, artisan_id):
    response = client.get(f"/artisans/{artisan_id}")
    assert response.status_code == 200
    assert response.json()["artisan_id"] == artisan_id
    assert client.get("/artisans/art_nobody").status_code == 404


def test_contact_and_consent_are_for_the_service_only(client, artisan_headers, artisan_id, service_headers):
    assert client.get(f"/artisans/{artisan_id}/contact").status_code == 401
    before = client.get(f"/artisans/{artisan_id}/contact", headers=service_headers).json()
    assert before["has_account"] is True and before["phone"]
    assert before["ai_call_consent"] is False  # off unless she says yes

    client.patch("/auth/me", json={"ai_call_consent": True}, headers=artisan_headers)
    after = client.get(f"/artisans/{artisan_id}/contact", headers=service_headers).json()
    assert after["ai_call_consent"] is True
