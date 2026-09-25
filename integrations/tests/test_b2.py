"""C2 against B2: orders from B2, delivery recorded there, calls only with consent.

B2 itself is faked at the `b2_client` boundary; `platform/tests` covers
B2's side of these endpoints.
"""

from __future__ import annotations

import pytest

from c2 import b2_client, c1_client, config, pipeline, voice
from tests.conftest import make_order


@pytest.fixture
def b2(monkeypatch):
    """A fake B2: one order, a status per order, a settlement, contacts."""
    monkeypatch.setattr(config, "SERVICE_TOKEN", "svc")
    state = {
        "orders": [make_order("ord_b2")],
        "status": {"ord_b2": "placed"},
        "delivered": [],
        "contacts": {"art_a": {"artisan_id": "art_a", "has_account": True, "phone": "+919000000001",
                               "language": "en", "ai_call_consent": True}},
        "payouts": [{"artisan_id": "art_a", "quantity": 2, "amount_inr": 500,
                     "listing_title": "Title lst_1", "status": "pending"}],
    }

    def deliver(order_id):
        state["delivered"].append(order_id)
        state["status"][order_id] = "delivered"
        return "delivered"

    monkeypatch.setattr(b2_client, "orders", lambda: state["orders"])
    monkeypatch.setattr(b2_client, "status", lambda oid: state["status"][oid])
    monkeypatch.setattr(b2_client, "deliver", deliver)
    monkeypatch.setattr(b2_client, "settlement", lambda oid: {"payouts": state["payouts"]})
    monkeypatch.setattr(b2_client, "contact", lambda aid: state["contacts"].get(
        aid, {"artisan_id": aid, "has_account": False, "ai_call_consent": False}))
    return state


def test_orders_come_from_b2_when_connected(b2):
    orders, source = c1_client.orders_or_sample()
    assert source == "b2"
    assert [o.order_id for o in orders] == ["ord_b2"]


def test_no_courier_until_the_artisan_accepts(b2):
    order = b2["orders"][0]
    with pytest.raises(pipeline.NotReady, match="accepts it in Craftly Studio"):
        pipeline.confirm(order, platform=True)
    b2["status"]["ord_b2"] = "accepted"
    shipment, _, calls = pipeline.confirm(order, platform=True)
    assert shipment.master_waybill and calls


def test_delivery_is_recorded_in_b2_and_the_call_speaks_b2s_payout(b2):
    order = b2["orders"][0]
    b2["status"]["ord_b2"] = "accepted"
    b2["payouts"][0]["amount_inr"] = 480  # B2's settled figure, not C2's 2 x 250
    calls = pipeline.deliver(order, platform=True)
    assert b2["delivered"] == ["ord_b2"]
    assert len(calls) == 1 and "₹480" in calls[0].script_en


def test_a_blocked_payout_is_not_called_credited(b2):
    b2["status"]["ord_b2"] = "accepted"
    b2["payouts"][0]["status"] = "blocked"
    call = pipeline.deliver(b2["orders"][0], platform=True)[0]
    assert "credited" not in call.script_en
    assert "UPI" in call.script_en and "₹500" in call.script_en


def test_calls_use_her_real_phone_and_chosen_language(b2):
    call = voice.place_call("order_confirmed", b2["orders"][0], pipeline.payouts(b2["orders"][0])[0])
    assert call.to_phone == "+919000000001"
    assert call.language == "en"  # her Studio language, though her profile lists Hindi first
    assert call.outcome.startswith("PLACED")


def test_no_consent_no_call(b2):
    b2["contacts"]["art_a"]["ai_call_consent"] = False
    call = voice.place_call("order_confirmed", b2["orders"][0], pipeline.payouts(b2["orders"][0])[0])
    assert call.outcome.startswith("NOT_CALLED") and "switched off" in call.outcome
    assert call.duration_sec == 0


def test_no_account_no_call(b2):
    del b2["contacts"]["art_a"]
    call = voice.place_call("order_confirmed", b2["orders"][0], pipeline.payouts(b2["orders"][0])[0])
    assert call.outcome.startswith("NOT_CALLED") and "no Craftly Studio account" in call.outcome


def test_without_the_token_c2_works_as_before():
    assert not b2_client.enabled()
    assert b2_client.orders() is None and b2_client.contact("art_a") is None
    _, source = c1_client.orders_or_sample()
    assert source in ("c1_orders_jsonl", "c2_sample")


def test_pending_money_is_never_called_credited(b2):
    """B2 records a payout as pending on delivery; no money has moved yet."""
    b2["status"]["ord_b2"] = "accepted"
    call = pipeline.deliver(b2["orders"][0], platform=True)[0]
    assert "credited" not in call.script_en and "will be sent" in call.script_en
    assert "जमा कर दिए" not in voice.script("payment_credited", b2["orders"][0],
                                             pipeline.settled_payouts(b2["orders"][0])[0], None, "hi")


def test_paid_money_is_called_credited(b2):
    b2["status"]["ord_b2"] = "accepted"
    b2["payouts"][0]["status"] = "paid"
    assert "has been credited" in pipeline.deliver(b2["orders"][0], platform=True)[0].script_en


def test_no_call_when_consent_cannot_be_checked(b2, monkeypatch):
    """B2 configured but unreachable: never assume she said yes."""
    monkeypatch.setattr(b2_client, "contact", lambda aid: None)
    call = voice.place_call("order_confirmed", b2["orders"][0], pipeline.payouts(b2["orders"][0])[0])
    assert call.outcome.startswith("NOT_CALLED") and "consent" in call.outcome and call.duration_sec == 0
