import json
import sys

import pytest

from c2 import c1_client, config
from tests.conftest import make_order


def _write(path, rows):
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_reads_c1_log_ignoring_unknown_fields_and_bad_lines(monkeypatch, tmp_path):
    good = json.loads(make_order("ord_ok").model_dump_json())
    good["some_future_c1_field"] = 1
    log = tmp_path / "orders.jsonl"
    _write(log, ["{not json", good, ""])
    monkeypatch.setattr(config, "ORDERS_PATH", log)
    assert [o.order_id for o in c1_client.load_orders()] == ["ord_ok"]


def test_last_write_wins_per_order_id(monkeypatch, tmp_path):
    a = json.loads(make_order("ord_1").model_dump_json())
    b = {**a, "status": "dispatched"}
    log = tmp_path / "orders.jsonl"
    _write(log, [a, b])
    monkeypatch.setattr(config, "ORDERS_PATH", log)
    (only,) = c1_client.load_orders()
    assert only.status == "dispatched"


def test_missing_log_is_empty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ORDERS_PATH", tmp_path / "nope.jsonl")
    assert c1_client.load_orders() == []


def test_real_orders_beat_samples(monkeypatch, tmp_path):
    log = tmp_path / "orders.jsonl"
    _write(log, [json.loads(make_order("ord_real").model_dump_json())])
    monkeypatch.setattr(config, "ORDERS_PATH", log)
    orders, source = c1_client.orders_or_sample()
    assert source == "c1_orders_jsonl" and orders[0].order_id == "ord_real"


def test_falls_back_to_samples_when_log_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ORDERS_PATH", tmp_path / "nope.jsonl")
    orders, source = c1_client.orders_or_sample()
    assert source == "c2_sample" and orders and all(o.source == "c2_sample" for o in orders)
    assert any(o.kind == "bulk" and len(o.lines[0].split.allocations) > 1 for o in orders)


def test_parity_with_c1_order():
    """An Order serialised by C1's own model must parse in C2's mirror."""
    sys.path.insert(0, str(config.MARKET_DIR))
    try:
        from app.contracts import Buyer, Channel, Order, OrderLine  # type: ignore[import-not-found]
    except Exception:
        pytest.skip("C1 (market/) or its dependencies not importable")
    finally:
        sys.path.remove(str(config.MARKET_DIR))
    c1 = Order(
        order_id="ord_x", buyer=Buyer(name="A", phone="1", city="Pune", state="Maharashtra"),
        lines=[OrderLine(listing_id="lst_1", title="T", quantity=3, unit_price_inr=100,
                         channel=Channel.AMAZON, artisan_take_home_inr=70)],
    )
    mine = c1_client.Order.model_validate_json(c1.model_dump_json())
    assert (mine.order_id, mine.total_inr, mine.unit_count) == (c1.order_id, c1.total_inr, c1.unit_count)
    assert mine.lines[0].channel == "amazon" and mine.buyer.state == "Maharashtra"
