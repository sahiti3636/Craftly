from c2 import pipeline
from tests.conftest import make_order


def test_retail_payout_is_take_home_times_quantity():
    (p,) = pipeline.payouts(make_order())
    assert (p.artisan_id, p.quantity, p.amount_inr) == ("art_a", 2, 500)


def test_unknown_take_home_is_none_not_zero():
    order = make_order(lines=[{"listing_id": "lst_1", "title": "T", "quantity": 2, "unit_price_inr": 310}])
    assert pipeline.payouts(order)[0].amount_inr is None


def test_bulk_pays_each_allocation_separately():
    order = make_order(kind="bulk", lines=[{
        "listing_id": "lst_1", "title": "T", "quantity": 50, "unit_price_inr": 300,
        "split": {"allocations": [
            {"artisan_id": "art_a", "artisan_name": "Asha", "quantity": 30, "take_home_inr": 250},
            {"artisan_id": "art_b", "artisan_name": "Bela", "quantity": 20, "take_home_inr": 240},
        ]},
    }])
    assert {p.artisan_id: p.amount_inr for p in pipeline.payouts(order)} == {"art_a": 7500, "art_b": 4800}


def test_one_artisan_across_two_lines_is_one_payout():
    line = {"listing_id": "lst_1", "title": "T", "quantity": 1, "unit_price_inr": 310, "artisan_take_home_inr": 250}
    (p,) = pipeline.payouts(make_order(lines=[line, line]))
    assert (p.quantity, p.amount_inr) == (2, 500)
