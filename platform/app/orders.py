"""Placing an order, and moving it through its life.

C1 builds an `Order` and POSTs it. This module writes it down, decides
what is allowed to happen to it next, and hands delivery to
`app/payments.py`.

**The prices arrive with the order and are stored verbatim.** This service
does not call B1 to check them. That is not laziness, it is the point: the
buyer saw a number on a page and agreed to it, and a platform that
recalculates at write time can disagree with the receipt. If B1's price
moved between the page render and the submit, the buyer's number wins and
the difference is the marketplace's problem, not the artisan's and not the
buyer's.

**Allocations are exploded into rows.** A bulk line carries B1's
`SplitPlan`; the plan is kept whole for audit, and each allocation also
becomes an `Allocation` row, because that is what a payout is computed
from and a payout computed by re-parsing a JSON blob months later is a
payout nobody can defend.

**The status machine is explicit and only moves forwards.** Cancellation
is available up to dispatch and not after: once a courier has the box, the
question is a return, which is a different process with different money in
it. Marking an already-delivered order delivered again is a no-op rather
than an error, because a courier webhook that retries is normal and a 500
on it is how an order gets stuck.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import ids
from app.contracts import Allocation as AllocationOut
from app.contracts import Buyer, Channel
from app.contracts import Order as OrderOut
from app.contracts import OrderKind
from app.contracts import OrderLine as OrderLineOut
from app.contracts import OrderStatus
from app.contracts import SplitPlan
from app.models import Allocation, Listing, Order, OrderEvent, OrderLine


class OrderError(ValueError):
    """The order cannot be accepted, with a reason a surface can show."""


class TransitionError(ValueError):
    """That status change is not allowed from where the order is now."""


#: What may follow what. Terminal states map to an empty set.
TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PLACED: {OrderStatus.ACCEPTED, OrderStatus.CANCELLED},
    OrderStatus.ACCEPTED: {OrderStatus.IN_PRODUCTION, OrderStatus.DISPATCHED, OrderStatus.CANCELLED},
    OrderStatus.IN_PRODUCTION: {OrderStatus.DISPATCHED, OrderStatus.CANCELLED},
    OrderStatus.DISPATCHED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Placing
# ---------------------------------------------------------------------------


def place(db: Session, payload: OrderOut, account_id: str | None = None) -> Order:
    """Persist an order C1 has built.

    Raises `OrderError` with a sentence rather than a stack trace when the
    order cannot be taken, because the caller is a checkout page and the
    buyer is standing there.
    """
    if not payload.lines:
        raise OrderError("An order needs at least one line.")

    order_id = payload.order_id or ids.order_id()
    if db.get(Order, order_id) is not None:
        # C1 mints the id, so a retried POST arrives with the id it already
        # used. Returning the stored order is the correct answer to that;
        # placing a second one would charge the buyer twice.
        return get(db, order_id)  # type: ignore[return-value]

    order = Order(
        order_id=order_id,
        kind=payload.kind.value,
        status=OrderStatus.PLACED.value,
        channel=payload.channel.value,
        buyer_name=payload.buyer.name,
        buyer_phone=payload.buyer.phone,
        buyer_email=payload.buyer.email,
        buyer_organisation=payload.buyer.organisation,
        buyer_address=payload.buyer.address,
        buyer_city=payload.buyer.city,
        buyer_state=payload.buyer.state,
        buyer_pincode=payload.buyer.pincode,
        buyer_country=payload.buyer.country,
        account_id=account_id,
        deadline=payload.deadline,
        notes=payload.notes,
        source=payload.source,
        created_at=payload.created_at or _utcnow(),
    )
    db.add(order)

    for position, line_in in enumerate(payload.lines):
        listing = db.get(Listing, line_in.listing_id)
        if listing is None:
            raise OrderError(
                f"{line_in.listing_id} is not a listing this marketplace knows about."
            )
        if not listing.published:
            raise OrderError(f"{line_in.title} is not on sale.")
        if line_in.quantity < 1:
            raise OrderError(f"{line_in.title} was ordered in a quantity of {line_in.quantity}.")

        line = OrderLine(
            order_id=order_id,
            position=position,
            listing_id=line_in.listing_id,
            title=line_in.title,
            quantity=line_in.quantity,
            unit_price_inr=line_in.unit_price_inr,
            artisan_take_home_inr=line_in.artisan_take_home_inr,
            channel=line_in.channel.value,
            split_plan=line_in.split.model_dump(mode="json") if line_in.split else None,
        )
        db.add(line)
        db.flush()  # the line needs an id before allocations can point at it
        _write_allocations(db, line, line_in, listing)

    db.add(
        OrderEvent(
            order_id=order_id,
            status=OrderStatus.PLACED.value,
            actor=payload.source or "storefront",
            note=f"{payload.unit_count} units, {len(payload.lines)} line(s)",
        )
    )
    db.flush()
    return get(db, order_id)  # type: ignore[return-value]


def _write_allocations(
    db: Session, line: OrderLine, line_in: OrderLineOut, listing: Listing
) -> None:
    """Explode B1's split into rows, or make the implicit single one explicit.

    A retail line has no split plan — it is one artisan making one thing.
    Writing that as an allocation anyway means the payout code has exactly
    one shape to handle, instead of a branch that gets tested on bulk
    orders and forgotten on retail ones.
    """
    plan = line_in.split
    if plan is not None and plan.allocations:
        for allocation in plan.allocations:
            db.add(
                Allocation(
                    line_id=line.id,
                    artisan_id=allocation.artisan_id,
                    artisan_name=allocation.artisan_name,
                    village=allocation.village,
                    quantity=allocation.quantity,
                    lead_time_days=allocation.lead_time_days,
                    take_home_inr=(
                        allocation.take_home_inr
                        if allocation.take_home_inr is not None
                        else line_in.artisan_take_home_inr
                    ),
                )
            )
        return

    db.add(
        Allocation(
            line_id=line.id,
            artisan_id=listing.artisan_id,
            artisan_name=listing.artisan.name,
            village=listing.artisan.village,
            quantity=line.quantity,
            lead_time_days=(listing.inventory.lead_time_days if listing.inventory else None),
            take_home_inr=line_in.artisan_take_home_inr,
        )
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _loaded():
    return select(Order).options(
        selectinload(Order.lines).selectinload(OrderLine.allocations),
        selectinload(Order.events),
        selectinload(Order.payouts),
    )


def get(db: Session, order_id: str) -> Order | None:
    return db.scalars(_loaded().where(Order.order_id == order_id)).unique().one_or_none()


def for_artisan(db: Session, artisan_id: str) -> list[Order]:
    """Orders with work in them for this artisan.

    Joined through allocations, not through the listing's owner: on a bulk
    order split across a cluster, the people who owe work are the ones in
    the split, and the listing's own artisan may be only one of ten.
    """
    order_ids = db.scalars(
        select(OrderLine.order_id)
        .join(Allocation, Allocation.line_id == OrderLine.id)
        .where(Allocation.artisan_id == artisan_id)
        .distinct()
    ).all()
    if not order_ids:
        return []
    rows = (
        db.scalars(
            _loaded().where(Order.order_id.in_(order_ids)).order_by(Order.created_at.desc())
        )
        .unique()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------


def transition(
    db: Session,
    order: Order,
    to: OrderStatus,
    actor: str | None = None,
    note: str | None = None,
) -> Order:
    """Advance an order, or say clearly why it cannot advance."""
    current = OrderStatus(order.status)
    if to == current:
        return order  # A retried webhook is normal. Do not punish it.
    if to not in TRANSITIONS[current]:
        raise TransitionError(
            f"An order that is {current.value} cannot become {to.value}."
        )

    order.status = to.value
    if to == OrderStatus.DELIVERED:
        order.delivered_at = _utcnow()
    db.add(OrderEvent(order_id=order.order_id, status=to.value, actor=actor, note=note))
    db.flush()
    return order


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


def to_contract(order: Order) -> OrderOut:
    lines = []
    for line in order.lines:
        plan = None
        if line.split_plan:
            plan = SplitPlan.model_validate(line.split_plan)
        elif len(line.allocations) > 1:
            # Defensive: allocations exist but the plan blob does not.
            # Rebuild enough of it that a fulfilment view still shows who
            # is making what, rather than showing one name for ten people.
            plan = SplitPlan(
                listing_id=line.listing_id,
                quantity_requested=line.quantity,
                quantity_allocated=sum(a.quantity for a in line.allocations),
                allocations=[
                    AllocationOut(
                        artisan_id=a.artisan_id,
                        artisan_name=a.artisan_name,
                        village=a.village,
                        quantity=a.quantity,
                        lead_time_days=a.lead_time_days,
                        take_home_inr=a.take_home_inr,
                    )
                    for a in line.allocations
                ],
            )
        lines.append(
            OrderLineOut(
                listing_id=line.listing_id,
                title=line.title,
                quantity=line.quantity,
                unit_price_inr=line.unit_price_inr,
                channel=Channel(line.channel),
                artisan_take_home_inr=line.artisan_take_home_inr,
                split=plan,
            )
        )

    return OrderOut(
        order_id=order.order_id,
        kind=OrderKind(order.kind),
        status=OrderStatus(order.status),
        buyer=Buyer(
            name=order.buyer_name,
            phone=order.buyer_phone,
            email=order.buyer_email,
            organisation=order.buyer_organisation,
            address=order.buyer_address,
            city=order.buyer_city,
            state=order.buyer_state,
            pincode=order.buyer_pincode,
            country=order.buyer_country,
        ),
        lines=lines,
        channel=Channel(order.channel),
        deadline=order.deadline,
        notes=order.notes,
        source=order.source,
        created_at=order.created_at,
    )
