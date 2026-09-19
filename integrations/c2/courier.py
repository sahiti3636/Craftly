"""Courier booking (simulated): an order in, a waybill and a pickup slot out.

Nothing is sent to a courier. The waybill numbers are derived from the
order id, so booking the same order twice gives the same waybill - the same
idempotency a real booking API would need.

What the simulation takes from the order for real:

* **Pickups follow the artisans, not the order.** A bulk order pooled
  across a cluster (`line.split.allocations`) is picked up from every
  artisan in it, one waybill each, under one master waybill. A retail line
  is picked up from the artisan who made it.
* **Pickup waits for making.** If the listing has stock for the quantity,
  pickup is the next business day; a made-to-order line pushes pickup out
  by its lead time. Sundays are skipped.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone

from c2 import c1_client
from c2.logs import append_jsonl
from c2.models import Order, PickupStop, Shipment, TrackingEvent

COURIERS = ("SwiftKart Express", "BharatLine Couriers", "Kesari Logistics")

_BOOKED: dict[str, Shipment] = {}


def _num(*parts: str, digits: int = 10) -> str:
    h = int(hashlib.sha1("|".join(parts).encode()).hexdigest(), 16)
    return str(h % 10**digits).zfill(digits)


def _next_business_day(d: date, plus_days: int = 0) -> date:
    d = d + timedelta(days=max(plus_days, 0) + 1)
    while d.weekday() == 6:  # Sunday
        d += timedelta(days=1)
    return d


def _pickups_for(order: Order) -> tuple[list[PickupStop], int]:
    """(one stop per artisan, days until the slowest line is ready)."""
    qty_by_artisan: dict[str, int] = {}
    wait_days = 0
    for line in order.lines:
        allocations = line.split.allocations if line.split else []
        if allocations:
            for alloc in allocations:
                qty_by_artisan[alloc.artisan_id] = qty_by_artisan.get(alloc.artisan_id, 0) + alloc.quantity
                wait_days = max(wait_days, alloc.lead_time_days or 0)
            continue

        try:
            product, _ = c1_client.get_product(line.listing_id, line.channel)
        except KeyError:
            continue
        artisan_id = product.get("artisan_id")
        if not artisan_id:
            continue
        qty_by_artisan[artisan_id] = qty_by_artisan.get(artisan_id, 0) + line.quantity
        inv = product.get("inventory") or {}
        if (inv.get("in_stock") or 0) < line.quantity:
            wait_days = max(wait_days, inv.get("lead_time_days") or 0)

    stops = []
    for artisan_id, qty in qty_by_artisan.items():
        art = c1_client.artisan(artisan_id)
        stops.append(
            PickupStop(
                artisan_id=artisan_id,
                artisan_name=art.get("name", artisan_id),
                village=art.get("village"),
                state=art.get("state"),
                quantity=qty,
                waybill="CRFT" + _num(order.order_id, artisan_id),
            )
        )
    return stops, wait_days


def _deliver_to(order: Order) -> str:
    b = order.buyer
    return ", ".join(p for p in (b.name, b.address, b.city, b.state, b.pincode) if p)


def book(order: Order) -> Shipment:
    """Book (simulate) a courier for `order`. Idempotent per order id."""
    if order.order_id in _BOOKED:
        return _BOOKED[order.order_id]

    stops, wait_days = _pickups_for(order)
    today = c1_client.today()
    pickup = _next_business_day(today, wait_days)
    window = ("10:00-13:00", "14:00-17:00")[int(_num(order.order_id, digits=3)) % 2]

    origin_states = {s.state for s in stops if s.state}
    same_state = bool(order.buyer.state) and origin_states == {order.buyer.state}
    transit = 2 if same_state else 4
    est_delivery = _next_business_day(pickup, transit - 1)

    def at(d: date, hour: int) -> datetime:
        return datetime.combine(d, time(hour, 0), tzinfo=timezone.utc)

    tracking = [
        TrackingEvent(status="BOOKED", detail="Shipment booked with courier", at=at(today, 12)),
        TrackingEvent(status="PICKUP_SCHEDULED", detail=f"Pickup {pickup.isoformat()} {window} from {len(stops)} artisan(s)", at=at(today, 12)),
        TrackingEvent(status="PICKED_UP", detail="Projected", at=at(pickup, 15)),
        TrackingEvent(status="IN_TRANSIT", detail="Projected", at=at(pickup + timedelta(days=1), 9)),
        TrackingEvent(status="DELIVERED", detail="Projected", at=at(est_delivery, 16)),
    ]

    shipment = Shipment(
        order_id=order.order_id,
        courier=COURIERS[int(_num(order.buyer.state or "", digits=3)) % len(COURIERS)],
        service="Surface" if transit > 2 else "Regional",
        master_waybill="CRFTM" + _num(order.order_id, "master"),
        pickup_date=pickup,
        pickup_window=window,
        pickups=stops,
        deliver_to=_deliver_to(order),
        est_delivery=est_delivery,
        tracking=tracking,
    )
    _BOOKED[order.order_id] = shipment
    append_jsonl("shipments.jsonl", shipment.model_dump(mode="json"))
    return shipment


def get(order_id: str) -> Shipment | None:
    return _BOOKED.get(order_id)


def reset() -> None:
    _BOOKED.clear()
