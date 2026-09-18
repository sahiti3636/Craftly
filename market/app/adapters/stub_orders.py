"""Where a placed order goes when B2 is not up yet.

Keeps orders in memory so the confirmation page can read back what was
just placed, and appends each one as a line of JSON to
`CRAFTLY_ORDERS_PATH` so that an order survives the process and can be
handed to whoever is building the artisan-side fulfilment view.

The JSONL file is the useful part during integration: it is exactly the
payload C1 will POST to B2, written down, so B2 can build against real
traffic before the two services are wired together.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from app import config
from app.contracts import Order, OrderStatus


def new_order_id() -> str:
    return f"ord_{uuid4().hex[:12]}"


class StubOrderSink:
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path or config.ORDERS_PATH)
        self._orders: dict[str, Order] = {}

    def place(self, order: Order) -> Order:
        stored = order.model_copy(update={"status": OrderStatus.PLACED})
        self._orders[stored.order_id] = stored
        self._append(stored)
        return stored

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    # -- extras the stub exposes that the real B2 need not --------------

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())

    def _append(self, order: Order) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(order.model_dump_json() + "\n")
        except OSError:
            # A read-only or missing disk must not lose a buyer their
            # order — the in-memory copy is already saved and the
            # confirmation page will still render.
            pass
