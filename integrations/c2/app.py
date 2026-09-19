"""C2's service on port 8300: the JSON API and a one-page demo console.

    cd integrations && uv run uvicorn c2.app:app --reload --port 8300

Every response that comes from a simulation carries `"simulated": true`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from c2 import c1_client, channels, courier, demand, pipeline, voice
from c2.models import DemandAlert, Order

app = FastAPI(title="Craftly C2 - integrations (simulated)", version="0.1.0")

_PAGE = Path(__file__).resolve().parent / "console.html"


def _order(order_id: str) -> Order:
    orders, _ = c1_client.orders_or_sample()
    for order in orders:
        if order.order_id == order_id:
            return order
    raise HTTPException(404, f"No such order: {order_id}")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def console() -> str:
    return _PAGE.read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        _, source = c1_client.get_product(c1_client.all_listing_ids()[0], "amazon")
    except (KeyError, IndexError):
        source = "no_catalogue"
    return {"ok": True, "c1_source": source, "c1_url": c1_client.config.C1_URL, "channels": sorted(channels.ADAPTERS)}


@app.get("/api/listings")
def listings() -> list[dict[str, Any]]:
    return [
        {"listing_id": i, "title": l.get("title_en"), "artisan_id": l.get("artisan_id")}
        for i, l in c1_client.seed_listings().items()
    ]


@app.get("/api/orders")
def orders() -> dict[str, Any]:
    found, source = c1_client.orders_or_sample()
    return {
        "source": source,
        "simulated": source == "c2_sample",
        "orders": [
            {
                "order_id": o.order_id,
                "kind": o.kind,
                "buyer": o.buyer.name,
                "city": o.buyer.city,
                "units": o.unit_count,
                "total_inr": o.total_inr,
                "channel": o.channel,
                "created_at": o.created_at,
                "shipment_booked": courier.get(o.order_id) is not None,
            }
            for o in found
        ],
    }


@app.post("/api/channels/{channel}/push/{listing_id}")
def push_listing(channel: str, listing_id: str):
    try:
        return channels.push(channel, listing_id)
    except channels.ChannelError as exc:
        raise HTTPException(404 if "listing" in str(exc) else 400, str(exc)) from exc


@app.post("/api/channels/{channel}/push-all")
def push_all(channel: str, limit: int | None = None):
    try:
        return channels.push_catalogue(channel, limit)
    except channels.ChannelError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/orders/{order_id}/confirm")
def confirm_order(order_id: str) -> dict[str, Any]:
    shipment, owed, calls = pipeline.confirm(_order(order_id))
    return {
        "shipment": shipment,
        "payouts": owed,
        "calls": calls,
        "language_gaps": voice.note_language_gaps(owed),
    }


@app.post("/api/orders/{order_id}/deliver")
def deliver_order(order_id: str) -> dict[str, Any]:
    order = _order(order_id)
    calls = pipeline.deliver(order)
    return {"order_id": order_id, "status": "delivered", "simulated": True, "calls": calls}


@app.get("/api/demand-alert", response_model=DemandAlert)
def demand_alert() -> DemandAlert:
    return demand.weekly_alert()


@app.post("/api/demo/reset")
def reset() -> dict[str, bool]:
    courier.reset()
    voice.reset()
    return {"ok": True}
