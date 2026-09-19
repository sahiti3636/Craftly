import pytest

from c2 import courier, pipeline, voice
from c2.models import Payout
from tests.conftest import make_order


def _payout(**kw):
    base = dict(artisan_id="art_a", artisan_name="Asha", languages=["hi"], quantity=2,
                titles_en=["Diya"], titles_hi=["दीया"], amount_inr=500)
    return Payout(**{**base, **kw})


def test_language_choice():
    assert voice.pick_language(["bn", "hi"]) == "hi"
    assert voice.pick_language(["gu", "en"]) == "en"
    assert voice.pick_language(["bn"]) == "hi"


def test_all_three_events_are_scripted_in_both_languages():
    order = make_order()
    ship = courier.book(order)
    for event in voice.EVENTS:
        for lang in ("hi", "en"):
            assert order.order_id in voice.script(event, order, _payout(), ship, lang)


def test_hindi_call_carries_the_payout_and_hindi_title():
    order = make_order()
    call = voice.place_call("order_confirmed", order, _payout(), courier.book(order))
    assert call.language == "hi" and call.simulated
    assert "₹500" in call.script and "दीया" in call.script
    assert "You will receive ₹500" in call.script_en


def test_missing_amount_is_never_spoken_as_zero():
    order = make_order()
    ship = courier.book(order)
    for event in ("order_confirmed", "payment_credited"):
        call = voice.place_call(event, order, _payout(amount_inr=None), ship)
        assert "₹0" not in call.script and "₹0" not in call.script_en
        assert "₹" not in call.script_en


def test_pickup_call_names_the_artisans_own_waybill():
    order = make_order()
    ship = courier.book(order)
    call = voice.place_call("pickup_scheduled", order, _payout(), ship)
    assert ship.pickups[0].waybill in call.script


def test_pickup_call_without_shipment_is_an_error():
    with pytest.raises(ValueError):
        voice.place_call("pickup_scheduled", make_order(), _payout(), None)


def test_unknown_event():
    with pytest.raises(ValueError):
        voice.place_call("nope", make_order(), _payout())


def test_calls_are_idempotent():
    order = make_order()
    ship = courier.book(order)
    a = voice.place_call("order_confirmed", order, _payout(), ship)
    b = voice.place_call("order_confirmed", order, _payout(), ship)
    assert a is b and len(voice.calls_for(order.order_id)) == 1


def test_language_gap_is_reported():
    assert voice.note_language_gaps([_payout(languages=["bn"])])
    assert not voice.note_language_gaps([_payout(languages=["bn", "hi"])])


def test_confirm_then_deliver_places_three_calls_per_artisan():
    order = make_order()
    _, owed, calls = pipeline.confirm(order)
    assert [c.event for c in calls] == ["order_confirmed", "pickup_scheduled"]
    pipeline.deliver(order)
    assert [c.event for c in voice.calls_for(order.order_id)] == list(voice.EVENTS)
    assert owed[0].amount_inr == 500  # 2 x 250 from C1's own take-home


def test_phone_is_a_ten_digit_mobile():
    digits = voice._phone("art_a").replace("+91", "").replace(" ", "")
    assert len(digits) == 10 and digits[0] == "9"
