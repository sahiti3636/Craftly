import pytest

from c2 import channels
from tests.conftest import product


def _set(fake_c1, **kw):
    fake_c1["lst_1"] = product("lst_1", "art_a", **kw)


@pytest.mark.parametrize("channel,key", [("amazon", "asin"), ("flipkart", "fsn"), ("ebay", "offerId")])
def test_push_accepted_with_deterministic_ids(channel, key):
    a, b = channels.push(channel, "lst_1"), channels.push(channel, "lst_1")
    assert a.simulated and a.response["simulated"]
    assert a.response[key] == b.response[key]


def test_price_and_take_home_come_from_c1_not_recomputed():
    push = channels.push("amazon", "lst_1")
    price = push.request["attributes"]["purchasable_offer"][0]["our_price"][0]["schedule"][0]["value_with_tax"]
    assert price == 310
    assert push.response["settlement_preview"] == {
        "buyer_pays_inr": 310, "channel_fee_inr": 60, "artisan_receives_inr": 250,
    }


def test_marketplace_payload_does_not_leak_take_home():
    assert "artisan_take_home" not in str(channels.push("flipkart", "lst_1").request)


def test_image_url_made_absolute():
    push = channels.push("flipkart", "lst_1")
    assert push.request["attributes"]["image_url"].startswith("http")


def test_no_price_is_never_pushed(fake_c1):
    _set(fake_c1, with_price=False)
    push = channels.push("amazon", "lst_1")
    assert push.response["status"] == "NOT_SENT" and push.request == {}
    assert "guessed price" in push.warnings[0]


def test_incomplete_floor_is_never_pushed(fake_c1):
    _set(fake_c1, floor_incomplete=True)
    push = channels.push("amazon", "lst_1")
    assert push.response["status"] == "NOT_SENT"
    assert "wage floor" in push.warnings[0]


def test_not_buyable_is_never_pushed(fake_c1):
    _set(fake_c1, can_buy=False)
    push = channels.push("ebay", "lst_1")
    assert push.response["status"] == "NOT_SENT" and push.warnings == ["Not buyable"]


def test_below_floor_is_pushed_with_a_warning(fake_c1):
    _set(fake_c1, below_floor=True)
    push = channels.push("amazon", "lst_1")
    assert push.response["status"] == "ACCEPTED"
    assert any("wage floor" in w for w in push.warnings)


def test_non_marketplace_channel_rejected():
    with pytest.raises(channels.ChannelError):
        channels.push("own_store", "lst_1")


def test_unknown_listing():
    with pytest.raises(channels.ChannelError, match="No such listing"):
        channels.push("amazon", "lst_nope")


def test_push_catalogue_covers_every_listing():
    assert [p.listing_id for p in channels.push_catalogue("amazon")] == ["lst_1", "lst_2", "lst_3", "lst_4"]
