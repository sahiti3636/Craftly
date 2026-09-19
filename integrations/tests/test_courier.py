from datetime import date

from c2 import config, courier
from tests.conftest import make_order


def test_retail_pickup_next_business_day_from_the_artisan():
    ship = courier.book(make_order())
    assert ship.simulated
    assert ship.pickup_date == date(2026, 9, 17)  # day after Wed 16th; stock covers qty 2
    assert [(p.artisan_name, p.quantity) for p in ship.pickups] == [("Asha", 2)]
    assert ship.master_waybill.startswith("CRFTM") and ship.pickups[0].waybill.startswith("CRFT")


def test_made_to_order_line_waits_for_lead_time():
    # lst_2 has 2 in stock and a 10-day lead time; ordering 5 waits: 16th + 10 + 1 = 27th, a
    # Sunday, so the pickup lands on Monday the 28th.
    order = make_order(lines=[{"listing_id": "lst_2", "title": "T", "quantity": 5, "unit_price_inr": 100}])
    assert courier.book(order).pickup_date == date(2026, 9, 28)


def test_sunday_is_skipped(monkeypatch):
    monkeypatch.setattr(config, "TODAY_OVERRIDE", "2026-09-19")  # Saturday
    assert courier.book(make_order()).pickup_date == date(2026, 9, 21)  # Monday


def test_bulk_split_gets_one_pickup_per_artisan():
    order = make_order(kind="bulk", lines=[{
        "listing_id": "lst_1", "title": "T", "quantity": 60, "unit_price_inr": 300,
        "split": {"allocations": [
            {"artisan_id": "art_a", "artisan_name": "Asha", "quantity": 30, "lead_time_days": 5, "take_home_inr": 250},
            {"artisan_id": "art_b", "artisan_name": "Bela", "quantity": 30, "lead_time_days": 8, "take_home_inr": 250},
        ]},
    }])
    ship = courier.book(order)
    assert {p.artisan_id: p.quantity for p in ship.pickups} == {"art_a": 30, "art_b": 30}
    assert len({p.waybill for p in ship.pickups}) == 2
    assert ship.pickup_date == date(2026, 9, 25)  # 16 + slowest allocation (8) + 1


def test_booking_is_idempotent():
    order = make_order()
    assert courier.book(order) is courier.book(order)


def test_same_state_delivers_faster():
    same = courier.book(make_order("ord_s", state="West Bengal"))
    far = courier.book(make_order("ord_f", state="Karnataka"))
    assert (same.est_delivery - same.pickup_date) < (far.est_delivery - far.pickup_date)


def test_shipment_is_logged():
    courier.book(make_order())
    assert (config.LOG_DIR / "shipments.jsonl").read_text(encoding="utf-8").count("\n") == 1
