"""The whole C2 flow in the terminal, no server needed.

    cd integrations && uv run python demo.py            # every step
    cd integrations && uv run python demo.py --order ord_xxx --channel flipkart

Steps: (1) push a listing to a marketplace  (2) book a courier for an order
(3) call the artisan(s)  (4) simulate delivery and the payout call
(5) build the weekly demand alert.

Orders come from C1's `market/orders.jsonl`; if there are none, sample
orders built from C1's catalogue are used and labelled as such. Priced
listings come from C1 (HTTP if it is running, else loaded in-process).
"""

from __future__ import annotations

import argparse
import sys

from c2 import c1_client, channels, demand, pipeline, voice
from c2.fmt import inr


def _h(n: int, text: str) -> None:
    print(f"\n{'=' * 72}\n  STEP {n}: {text}\n{'=' * 72}")


def _print_calls(calls) -> None:
    for c in calls:
        print(f"\n  [{c.event}] -> {c.artisan_name} ({c.to_phone}, {c.language}, {c.duration_sec}s, {c.outcome})")
        print(f"    {c.script}")
        if c.language != "en":
            print(f"    EN: {c.script_en}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--order", help="order id to fulfil (default: the first available)")
    parser.add_argument("--channel", default="amazon", choices=sorted(channels.ADAPTERS))
    parser.add_argument("--listing", help="listing id to push (default: the first line of the order)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Hindi scripts and the rupee sign

    orders, source = c1_client.orders_or_sample()
    if not orders:
        print("No orders in market/orders.jsonl and C1 could not build sample orders "
              "(is market/seed present, and are C1's dependencies installed?).")
        return 1
    order = next((o for o in orders if o.order_id == args.order), None) if args.order else orders[0]
    if order is None:
        print(f"No such order: {args.order}. Available: {', '.join(o.order_id for o in orders)}")
        return 1
    tag = "SAMPLE (built from C1 catalogue)" if source == "c2_sample" else "from C1 orders.jsonl"
    print(f"Order {order.order_id} [{tag}] - {order.buyer.name}, {order.buyer.city}, "
          f"{order.unit_count} unit(s), {inr(order.total_inr)}, kind={order.kind}")

    # 1. Marketplace push -------------------------------------------------
    listing_id = args.listing or order.lines[0].listing_id
    _h(1, f"Push {listing_id} to {args.channel} (simulated)")
    push = channels.push(args.channel, listing_id)
    print(f"  price from : {push.price_source}")
    print(f"  response   : {push.response.get('status')}  "
          f"{push.response.get('asin') or push.response.get('fsn') or push.response.get('offerId') or ''}")
    if s := push.response.get("settlement_preview"):
        print(f"  buyer pays {inr(s['buyer_pays_inr'])} | {args.channel} fee {inr(s['channel_fee_inr'])} "
              f"| artisan receives {inr(s['artisan_receives_inr'])}")
    for w in push.warnings:
        print(f"  ! {w}")

    # 2-3. Courier + calls ------------------------------------------------
    _h(2, "Book courier (simulated)")
    shipment, owed, calls = pipeline.confirm(order)
    print(f"  courier    : {shipment.courier} ({shipment.service})")
    print(f"  master AWB : {shipment.master_waybill}")
    print(f"  pickup     : {shipment.pickup_date} {shipment.pickup_window}")
    for stop in shipment.pickups:
        print(f"    - {stop.artisan_name} ({stop.village}, {stop.state}) x{stop.quantity}  waybill {stop.waybill}")
    print(f"  deliver to : {shipment.deliver_to}")
    print(f"  est. arrival: {shipment.est_delivery}")
    for p in owed:
        print(f"  payout     : {p.artisan_name} -> {inr(p.amount_inr)}")
    for gap in voice.note_language_gaps(owed):
        print(f"  ! {gap}")

    _h(3, "AI calls to artisans - order confirmed, pickup scheduled (simulated)")
    _print_calls(calls)

    # 4. Delivery ---------------------------------------------------------
    _h(4, "Delivery -> payment credited call (simulated)")
    _print_calls(pipeline.deliver(order))

    # 5. Demand alert -----------------------------------------------------
    _h(5, "Weekly demand alert")
    alert = demand.weekly_alert()
    print(f"  data: {alert.orders_source} ({alert.orders_counted} order(s), "
          f"{alert.window_start} to {alert.window_end})")
    print(f"  {alert.message_en}")
    print(f"  {alert.message_hi}")
    for item in alert.items:
        print(f"    - {item.title}: {item.units_ordered} sold, {item.action}")

    print("\nDone. Logs: integrations/logs/shipments.jsonl, integrations/logs/calls.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
