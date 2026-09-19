from datetime import datetime, timezone

from c2 import demand
from tests.conftest import make_order


def day(d: int) -> datetime:  # today is the 16th
    return datetime(2026, 9, d, 10, tzinfo=timezone.utc)


def _line(listing="lst_1", qty=3):
    return {"listing_id": listing, "title": "T", "quantity": qty, "unit_price_inr": 100}


def test_no_orders_means_no_alert_and_no_marketing():
    alert = demand.build([], "c1_orders_jsonl")
    assert alert.items == [] and "Nothing to report" in alert.message_en
    assert "season" not in alert.message_en.lower()


def test_counts_only_the_last_seven_days():
    orders = [
        make_order("in", lines=[_line(qty=4)], created=day(15)),
        make_order("edge", lines=[_line(qty=1)], created=day(10)),     # first day of window
        make_order("old", lines=[_line(qty=99)], created=day(9)),      # outside
        make_order("future", lines=[_line(qty=99)], created=day(17)),  # outside
    ]
    alert = demand.build(orders, "c1_orders_jsonl")
    assert alert.orders_counted == 2 and alert.items[0].units_ordered == 5


def test_cancelled_orders_are_ignored():
    alert = demand.build([make_order(lines=[_line()], status="cancelled", created=day(15))], "x")
    assert alert.items == []


def test_ranked_by_units_with_restock_action():
    orders = [make_order("a", lines=[_line("lst_1", 2), _line("lst_2", 9)], created=day(15))]
    alert = demand.build(orders, "c1_orders_jsonl")
    assert [i.listing_id for i in alert.items] == ["lst_2", "lst_1"]
    assert "restock now" in alert.items[0].action and "Lead time is 10 days" in alert.items[0].action
    assert "covers this pace" in alert.items[1].action  # 50 in stock vs 2 sold


def test_sample_data_is_flagged_simulated(monkeypatch):
    sample = ([make_order(lines=[_line()], created=day(15))], "c2_sample")
    monkeypatch.setattr("c2.c1_client.orders_or_sample", lambda: sample)
    assert demand.weekly_alert().simulated is True
