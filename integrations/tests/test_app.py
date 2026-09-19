import pytest
from fastapi.testclient import TestClient

from c2 import c1_client
from c2.app import app
from tests.conftest import make_order

client = TestClient(app)


@pytest.fixture(autouse=True)
def one_order(monkeypatch):
    monkeypatch.setattr(c1_client, "orders_or_sample", lambda: ([make_order("ord_1")], "c2_sample"))


def test_console_page_and_health():
    assert "Craftly C2" in client.get("/").text
    assert client.get("/api/health").json()["channels"] == ["amazon", "ebay", "flipkart"]


def test_orders_listing_flags_samples():
    body = client.get("/api/orders").json()
    assert body["simulated"] is True and body["orders"][0]["order_id"] == "ord_1"


def test_push_and_errors():
    assert client.post("/api/channels/amazon/push/lst_1").json()["response"]["status"] == "ACCEPTED"
    assert client.post("/api/channels/own_store/push/lst_1").status_code == 400
    assert client.post("/api/channels/amazon/push/lst_nope").status_code == 404


def test_confirm_then_deliver():
    r = client.post("/api/orders/ord_1/confirm").json()
    assert r["shipment"]["simulated"]
    assert [c["event"] for c in r["calls"]] == ["order_confirmed", "pickup_scheduled"]
    d = client.post("/api/orders/ord_1/deliver").json()
    assert d["calls"][0]["event"] == "payment_credited"
    assert client.post("/api/orders/nope/confirm").status_code == 404


def test_demand_alert_endpoint():
    assert "message_en" in client.get("/api/demand-alert").json()
