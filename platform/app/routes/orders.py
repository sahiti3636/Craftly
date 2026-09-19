"""Orders in, orders moved along, money split on delivery.

`POST /orders` is the endpoint C1's `HttpOrderSink` already calls, so its
request and response are both the `Order` from the shared contract and
neither is up for redesign here.

**Auth on `POST /orders` is off by default and that is a deliberate,
temporary, documented state.** C1's adapter was written before this
service existed and sends no Authorization header. Requiring one on day
one would break the only integration this endpoint has. `CRAFTLY_REQUIRE_
ORDER_AUTH=true` turns it on, `app/main.py` warns at startup while it is
off, and the README lists it as the thing to fix before this takes a
rupee.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import auth, config, orders, payments
from app.contracts import Order as OrderOut
from app.contracts import OrderStatus
from app.db import session
from app.models import Account
from app.schemas import (
    LineSplitOut,
    PayoutOut,
    SettlementOut,
    StatusChange,
)

router = APIRouter(tags=["orders"])


@router.post("/orders", response_model=OrderOut, status_code=201)
def place_order(
    payload: OrderOut,
    account: Account | None = Depends(auth.current_account_optional),
    db: Session = Depends(session),
) -> OrderOut:
    if config.REQUIRE_ORDER_AUTH and account is None:
        raise auth.AuthError("Sign in to place an order.")
    try:
        order = orders.place(db, payload, account_id=account.account_id if account else None)
    except orders.OrderError as exc:
        # 422, not 500: the order is well-formed JSON that this
        # marketplace will not accept, and the checkout page has to show
        # the buyer a sentence rather than an apology.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return orders.to_contract(order)


@router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: str, db: Session = Depends(session)) -> OrderOut:
    order = orders.get(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No such order.")
    return orders.to_contract(order)


@router.get("/orders/mine/artisan", response_model=list[OrderOut])
def artisan_orders(
    account: Account = Depends(auth.current_artisan),
    db: Session = Depends(session),
) -> list[OrderOut]:
    """Everything this artisan owes work on. A1's fulfilment view reads this.

    Found through the allocations, not through who owns the listing: on a
    bulk order split across a cluster, nine of the ten women with work to
    do do not own the listing it was ordered from.
    """
    rows = orders.for_artisan(db, account.artisan_id or "")
    return [orders.to_contract(row) for row in rows]


@router.post("/orders/{order_id}/status", response_model=OrderOut)
def change_status(
    order_id: str,
    payload: StatusChange,
    account: Account = Depends(auth.current_account),
    db: Session = Depends(session),
) -> OrderOut:
    """Accept, dispatch, deliver or cancel.

    Delivery settles the order in the same transaction. Not in a background
    job and not on a nightly batch: an artisan whose phone says "delivered"
    and whose payout appears tomorrow has been given a reason to distrust
    the system, and that trust is the thing this whole project is spending.
    """
    order = orders.get(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No such order.")

    try:
        orders.transition(db, order, payload.status, actor=account.account_id, note=payload.note)
    except orders.TransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if payload.status == OrderStatus.DELIVERED:
        payments.settle(db, order)

    db.commit()
    db.refresh(order)
    return orders.to_contract(order)


@router.get("/orders/{order_id}/settlement", response_model=SettlementOut)
def settlement(order_id: str, db: Session = Depends(session)) -> SettlementOut:
    """What this order is worth to each artisan.

    Before delivery this is a preview computed by the same function that
    does the real thing, so the number an artisan is shown when the order
    arrives is the number she is paid. After delivery it is the recorded
    payout rows.
    """
    order = orders.get(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No such order.")

    settled = order.settled_at is not None
    result = payments.settle(db, order) if settled else payments.preview(db, order)

    return SettlementOut(
        order_id=order.order_id,
        status=OrderStatus(order.status),
        gross_inr=result.gross_inr,
        artisan_total_inr=result.artisan_total_inr,
        platform_fee_inr=result.platform_fee_inr,
        platform_absorbed_inr=result.platform_absorbed_inr,
        artisan_share_pct=result.artisan_share_pct,
        settled=settled,
        complete=result.complete,
        lines=[
            LineSplitOut(
                listing_id=line.listing_id,
                title=line.title,
                quantity=line.quantity,
                gross_inr=line.gross_inr,
                artisan_total_inr=line.artisan_total_inr,
                platform_fee_inr=line.platform_fee_inr,
                platform_absorbed_inr=line.platform_absorbed_inr,
                problems=line.problems,
            )
            for line in result.lines
        ],
        payouts=[_payout_out(db, p) for p in order.payouts],
        problems=result.problems,
    )


def _payout_out(db: Session, payout) -> PayoutOut:
    from app.models import Artisan, OrderLine

    artisan = db.get(Artisan, payout.artisan_id)
    line = db.get(OrderLine, payout.line_id)
    return PayoutOut(
        payout_id=payout.payout_id,
        order_id=payout.order_id,
        artisan_id=payout.artisan_id,
        artisan_name=artisan.name if artisan else None,
        listing_title=line.title if line else None,
        quantity=payout.quantity,
        unit_take_home_inr=payout.unit_take_home_inr,
        amount_inr=payout.amount_inr,
        status=payout.status,
        blocked_reason=payout.blocked_reason,
        created_at=payout.created_at,
        paid_at=payout.paid_at,
    )


@router.get("/payouts/mine", response_model=list[PayoutOut])
def my_payouts(
    account: Account = Depends(auth.current_artisan),
    db: Session = Depends(session),
) -> list[PayoutOut]:
    """What this artisan has been paid and what she is still owed.

    Blocked rows are included rather than hidden. A woman who is owed
    ₹4,200 that cannot be sent because nobody collected her UPI id needs to
    see the ₹4,200 and the reason; a list that quietly omits it tells her
    she earned nothing.
    """
    rows = payments.payouts_for_artisan(db, account.artisan_id or "")
    return [_payout_out(db, row) for row in rows]


@router.post("/payouts/{payout_id}/paid", response_model=PayoutOut)
def mark_paid(
    payout_id: str,
    account: Account = Depends(auth.current_account),
    db: Session = Depends(session),
) -> PayoutOut:
    """Record that money actually left for this payout.

    Separate from settlement because computing what is owed and sending it
    are different events with different failure modes. A real payment rail
    (C2's, or a bank file) calls this; until one exists it is the manual
    acknowledgement that a transfer was made.
    """
    from app.models import Payout

    payout = db.get(Payout, payout_id)
    if payout is None:
        raise HTTPException(status_code=404, detail="No such payout.")
    try:
        payments.mark_paid(db, payout)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return _payout_out(db, payout)
