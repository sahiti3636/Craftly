"""Weekly demand alert - real data only, never generic marketing.

The one C2 output that is not a simulation: it is a computation over the
orders C1 actually logged. It says what sold in the last seven days, and
for each item whether stock covers another week at that rate. If there
were no orders in the window there is no alert - `items` is empty and the
message says so, rather than filling the space with a "festival season is
coming!" line that no data supports.

Everything in the alert can be traced to an order line or an inventory row:

* units and order counts   <- `market/orders.jsonl`
* stock and lead time      <- C1's inventory (via the priced listing)
* the suggested action     <- units sold vs. stock, nothing else
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from c2 import c1_client
from c2.models import DemandAlert, DemandItem, Order

WINDOW_DAYS = 7


def _action(units: int, in_stock: int | None, lead_time_days: int | None) -> str:
    if in_stock is None:
        return "Stock level unknown - ask the artisan how many are ready."
    if in_stock >= units * 2:
        return f"Stock ({in_stock}) covers this pace for now."
    if in_stock >= units:
        return f"Stock ({in_stock}) covers this week only; start another batch."
    lead = f" Lead time is {lead_time_days} days." if lead_time_days else ""
    return f"Sold {units} but only {in_stock} in stock - restock now.{lead}"


def build(orders: list[Order], source: str, simulated: bool = False) -> DemandAlert:
    end = c1_client.today()
    start = end - timedelta(days=WINDOW_DAYS - 1)
    lo = datetime.combine(start, time.min, tzinfo=timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)

    window = [o for o in orders if lo <= (o.created_at if o.created_at.tzinfo else o.created_at.replace(tzinfo=timezone.utc)) < hi
              and o.status != "cancelled"]

    units: dict[str, int] = {}
    counts: dict[str, int] = {}
    for order in window:
        for line in order.lines:
            units[line.listing_id] = units.get(line.listing_id, 0) + line.quantity
            counts[line.listing_id] = counts.get(line.listing_id, 0) + 1

    items: list[DemandItem] = []
    for listing_id, n in sorted(units.items(), key=lambda kv: -kv[1]):
        try:
            product, _ = c1_client.get_product(listing_id)
        except KeyError:
            continue
        listing, inv = product["listing"], product.get("inventory") or {}
        in_stock = inv.get("in_stock")
        lead = inv.get("lead_time_days")
        items.append(
            DemandItem(
                listing_id=listing_id,
                title=listing.get("title_en") or listing_id,
                craft_type=listing.get("craft_type"),
                artisan_name=product.get("artisan_name"),
                units_ordered=n,
                orders=counts[listing_id],
                in_stock=in_stock,
                lead_time_days=lead,
                action=_action(n, in_stock, lead),
            )
        )

    span = f"{start.isoformat()} to {end.isoformat()}"
    span_hi = f"{start.isoformat()} से {end.isoformat()} तक"
    if not items:
        msg_en = f"No orders from {span}. Nothing to report."
        msg_hi = f"{span_hi} कोई ऑर्डर नहीं आया। बताने के लिए कुछ नहीं है।"
    else:
        top = items[0]
        total = sum(i.units_ordered for i in items)
        msg_en = (
            f"{len(window)} order(s), {total} unit(s) from {span}. "
            f"Top seller: {top.title} ({top.units_ordered} sold). {top.action}"
        )
        msg_hi = (
            f"{span_hi} {len(window)} ऑर्डर, कुल {total} नग। "
            f"सबसे ज़्यादा बिका: {top.title} ({top.units_ordered} नग)।"
        )

    return DemandAlert(
        simulated=simulated,
        window_start=start,
        window_end=end,
        orders_counted=len(window),
        orders_source=source,
        items=items,
        message_en=msg_en,
        message_hi=msg_hi,
    )


def weekly_alert() -> DemandAlert:
    orders, source = c1_client.orders_or_sample()
    return build(orders, source, simulated=(source == "c2_sample"))
