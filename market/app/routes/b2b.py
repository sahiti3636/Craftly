"""The B2B portal: quantity, deadline, and an answer that can be no.

A corporate gifting buyer, a hotel chain, a museum shop and a government
emporium all want the same three things — a unit price at volume, a date,
and the names of the people who will make it. The third is not decoration:
it is the reason a CSR budget can be spent here at all, and it is the
thing a normal wholesale portal cannot produce.

The portal's job is to be honest at the point where honesty costs
something. `app/bulk.py` returns a quote that can be infeasible, and this
module renders that refusal with the two follow-up offers a real
procurement conversation would produce: a smaller quantity by the same
date, or the same quantity by a later one.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import bulk, deps
from app.adapters import registry
from app.adapters.stub_orders import new_order_id
from app.contracts import Buyer, Channel, Order, OrderKind, OrderLine
from app.search import SearchQuery, Sort, run
from app.templating import page

router = APIRouter(prefix="/b2b")


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


@router.get("")
def catalogue(request: Request):
    params = request.query_params
    try:
        page_number = max(1, int(params.get("page") or 1))
    except ValueError:
        page_number = 1
    query = SearchQuery(
        q=params.get("q", "").strip(),
        category=params.get("category") or None,
        state=params.get("state") or None,
        sort=Sort.RELEVANCE,
        page=page_number,
        per_page=12,
    )
    result = run(deps.cards(Channel.B2B), query)
    return page(request, "b2b_browse.html", result=result, query=query)


@router.get("/{listing_id}")
def quote_form(request: Request, listing_id: str):
    entry = registry.catalog().get_entry(listing_id)
    if entry is None:
        return page(request, "not_found.html", what="piece", status_code=404)

    card = deps.card(listing_id, Channel.B2B)
    requested = request.query_params.get("quantity")
    deadline = _parse_date(request.query_params.get("deadline"))

    quote = None
    if requested:
        try:
            quantity = int(requested)
        except ValueError:
            quantity = 0
        quote = bulk.quote(
            entry,
            quantity=quantity,
            prices=registry.prices(),
            engine=registry.order_engine(),
            deadline=deadline,
        )

    # What the cluster could take on at all, so the form can suggest a
    # number instead of letting the buyer guess and be refused.
    ceiling = bulk.max_feasible_quantity(entry, registry.order_engine(), None)

    return page(
        request,
        "b2b_quote.html",
        card=card,
        quote=quote,
        ceiling=ceiling,
        requested_quantity=requested or "",
        requested_deadline=request.query_params.get("deadline", ""),
        min_quantity=bulk.MIN_BULK_QUANTITY,
    )


@router.post("/{listing_id}/order")
def place_bulk_order(
    request: Request,
    listing_id: str,
    quantity: int = Form(...),
    deadline: str = Form(""),
    name: str = Form(...),
    organisation: str = Form(...),
    phone: str = Form(...),
    email: str = Form(""),
    address: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    pincode: str = Form(""),
    notes: str = Form(""),
):
    entry = registry.catalog().get_entry(listing_id)
    if entry is None:
        return page(request, "not_found.html", what="piece", status_code=404)

    deadline_date = _parse_date(deadline)
    quote = bulk.quote(
        entry,
        quantity=quantity,
        prices=registry.prices(),
        engine=registry.order_engine(),
        deadline=deadline_date,
    )

    if not quote.can_order:
        # Re-quote rather than trust the form: the page the buyer filled
        # in may be minutes old, and in those minutes the cluster may have
        # committed its capacity to someone else.
        return page(
            request,
            "b2b_quote.html",
            card=quote.card,
            quote=quote,
            ceiling=bulk.max_feasible_quantity(
                entry, registry.order_engine(), quote.deadline_days
            ),
            requested_quantity=str(quantity),
            requested_deadline=deadline,
            min_quantity=bulk.MIN_BULK_QUANTITY,
            rejected=True,
            status_code=409,
        )

    order = Order(
        order_id=new_order_id(),
        kind=OrderKind.BULK,
        buyer=Buyer(
            name=name.strip(),
            phone=phone.strip(),
            email=email.strip() or None,
            organisation=organisation.strip() or None,
            address=address.strip() or None,
            city=city.strip() or None,
            state=state.strip() or None,
            pincode=pincode.strip() or None,
        ),
        lines=[
            OrderLine(
                listing_id=listing_id,
                title=quote.card.title(),
                quantity=quantity,
                unit_price_inr=quote.unit_price_inr,
                channel=Channel.B2B,
                artisan_take_home_inr=quote.artisan_take_home_inr,
                split=quote.split,
            )
        ],
        channel=Channel.B2B,
        deadline=(
            datetime.combine(deadline_date, datetime.min.time()) if deadline_date else None
        ),
        notes=notes.strip() or None,
        source="b2b",
    )

    placed = registry.orders().place(order)
    return RedirectResponse(f"/order/{placed.order_id}", status_code=303)
