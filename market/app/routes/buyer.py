"""The storefront: browse, one product, basket, checkout.

The one rule that shapes every handler here is in `app/view.py`: an item
whose wage floor is incomplete cannot be bought. It is checked when the
page renders, again when something goes into the basket, and again at
checkout — not because the first check is unreliable, but because the
three happen at different times and a listing can lose its floor between
them.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import cart, deps
from app.adapters import registry
from app.contracts import Buyer, Channel, Order, OrderKind, OrderLine
from app.adapters.stub_orders import new_order_id
from app.search import Sort, SearchQuery, run
from app.templating import page

router = APIRouter()


def _query_from_request(request: Request) -> SearchQuery:
    params = request.query_params

    def as_int(name: str) -> int | None:
        raw = params.get(name)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    try:
        sort = Sort(params.get("sort", Sort.RELEVANCE.value))
    except ValueError:
        sort = Sort.RELEVANCE

    return SearchQuery(
        q=params.get("q", "").strip(),
        category=params.get("category") or None,
        craft_type=params.get("craft_type") or None,
        material=params.get("material") or None,
        colour=params.get("colour") or None,
        state=params.get("state") or None,
        min_price_inr=as_int("min_price"),
        max_price_inr=as_int("max_price"),
        in_stock_only=params.get("in_stock") == "1",
        sort=sort,
        page=max(1, as_int("page") or 1),
        per_page=12,
    )


@router.get("/classic")
def browse(request: Request):
    query = _query_from_request(request)
    result = run(deps.cards(Channel.OWN_STORE), query)
    return page(request, "browse.html", result=result, query=query)


@router.get("/product/{listing_id}")
def product(request: Request, listing_id: str):
    card = deps.card(listing_id, Channel.OWN_STORE)
    if card is None:
        return page(request, "not_found.html", what="piece", status_code=404)

    passport = registry.passports().by_listing(listing_id)
    entry_cards = [
        c
        for c in deps.cards(Channel.OWN_STORE)
        if c.artisan_id == card.artisan_id and c.listing_id != listing_id
    ]

    # What the same piece would cost on each channel the artisan sells
    # through. Shown because the honest version of "buy direct" is a
    # comparison the buyer can check, not a slogan.
    entry = registry.catalog().get_entry(listing_id)
    channel_quotes = {}
    if entry is not None:
        for channel in (Channel.OWN_STORE, Channel.AMAZON, Channel.FLIPKART):
            channel_quotes[channel.value] = registry.prices().quote(entry, channel)

    return page(
        request,
        "product.html",
        card=card,
        passport=passport,
        more_from_artisan=entry_cards[:3],
        channel_quotes=channel_quotes,
        came_from_qr=request.query_params.get("ref") == "qr",
    )


@router.get("/artisan/{artisan_id}")
def artisan(request: Request, artisan_id: str):
    cards = [c for c in deps.cards(Channel.OWN_STORE) if c.artisan_id == artisan_id]
    if not cards:
        return page(request, "not_found.html", what="artisan", status_code=404)
    return page(request, "artisan.html", cards=cards, artisan=cards[0])


# ---------------------------------------------------------------------------
# basket
# ---------------------------------------------------------------------------


def _save_cart(response, items: dict[str, int]):
    response.set_cookie(
        cart.COOKIE_NAME,
        cart.write(items),
        max_age=cart.COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/cart/add")
def cart_add(
    request: Request,
    listing_id: str = Form(...),
    quantity: int = Form(1),
    then: str = Form("/cart"),
):
    card = deps.card(listing_id, Channel.OWN_STORE)
    if card is None or not card.sellability.can_buy:
        # Nothing that cannot be sold is allowed into a basket, even by a
        # hand-written POST.
        return RedirectResponse(f"/product/{listing_id}", status_code=303)

    items = cart.add(cart.read(request.cookies.get(cart.COOKIE_NAME)), listing_id, quantity)
    return _save_cart(RedirectResponse(then, status_code=303), items)


@router.post("/cart/update")
def cart_update(
    request: Request, listing_id: str = Form(...), quantity: int = Form(...)
):
    items = cart.set_quantity(
        cart.read(request.cookies.get(cart.COOKIE_NAME)), listing_id, quantity
    )
    return _save_cart(RedirectResponse("/cart", status_code=303), items)


@router.get("/cart")
def view_cart(request: Request):
    items = cart.read(request.cookies.get(cart.COOKIE_NAME))
    basket = cart.build(items, deps.cards_by_id(Channel.OWN_STORE))
    return page(request, "cart.html", basket=basket)


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------


@router.get("/checkout")
def checkout(request: Request):
    items = cart.read(request.cookies.get(cart.COOKIE_NAME))
    basket = cart.build(items, deps.cards_by_id(Channel.OWN_STORE))
    if basket.is_empty:
        return RedirectResponse("/cart", status_code=303)
    return page(request, "checkout.html", basket=basket)


@router.post("/checkout")
def place_order(
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(""),
    address: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    pincode: str = Form(""),
):
    items = cart.read(request.cookies.get(cart.COOKIE_NAME))
    basket = cart.build(items, deps.cards_by_id(Channel.OWN_STORE))

    if basket.is_empty:
        return RedirectResponse("/cart", status_code=303)

    order = Order(
        order_id=new_order_id(),
        kind=OrderKind.RETAIL,
        buyer=Buyer(
            name=name.strip(),
            phone=phone.strip(),
            email=email.strip() or None,
            address=address.strip() or None,
            city=city.strip() or None,
            state=state.strip() or None,
            pincode=pincode.strip() or None,
        ),
        lines=[
            OrderLine(
                listing_id=line.card.listing_id,
                title=line.card.title(),
                quantity=line.quantity,
                unit_price_inr=line.card.price_inr,
                channel=Channel.OWN_STORE,
                artisan_take_home_inr=line.card.quote.artisan_take_home_inr,
            )
            for line in basket.lines
        ],
        channel=Channel.OWN_STORE,
        source="storefront",
    )

    placed = registry.orders().place(order)

    response = RedirectResponse(f"/order/{placed.order_id}", status_code=303)
    return _save_cart(response, {})


@router.get("/order/{order_id}")
def order_confirmation(request: Request, order_id: str):
    order = registry.orders().get(order_id)
    if order is None:
        return page(request, "not_found.html", what="order", status_code=404)

    cards = deps.cards_by_id(Channel.OWN_STORE)
    return page(request, "order.html", order=order, cards=cards)
