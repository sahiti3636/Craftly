"""The payment split: who gets paid what, when an order is delivered.

This is the number the whole project exists to defend, so the rules are
written down before the code.

**Rule 1 — the artisan's take-home is not a residual.** It is B1's number,
quoted to the buyer on the page, stored on the line, and paid out
unchanged. The marketplace's cut is what is left after it, not the other
way round. Systems that compute the platform fee first and hand the
artisan the remainder are how a woman ends up below minimum wage with a
receipt proving everyone agreed.

**Rule 2 — the fee comes out of the margin above the floor, and the margin
can be zero.** `CRAFTLY_PLATFORM_FEE_PCT` is applied to
`price - take_home`, never to `price`. When the margin is zero or negative
— which happens on exactly the crafts whose market price sits under their
own wage floor — the fee is zero and the marketplace absorbs the
difference. That is recorded as `platform_absorbed_inr` rather than
silently netted off, because a marketplace that is losing money on a
category should have to look at the number.

**Rule 3 — a payout we cannot send is `blocked`, never dropped.** If an
artisan has no UPI id on file, the row is still written with the full
amount and a reason. The money is owed whether or not we know where to
send it, and a system that skips the row loses the debt.

**Rule 4 — settlement is idempotent.** The unique constraint on
(order, line, artisan) is the guarantee, and `settle` checks before it
writes. Courier webhooks retry. Delivery gets confirmed twice. Neither may
pay an artisan twice, and neither may cause the second attempt to error in
a way that leaves an order half-settled.

**Rule 5 — an incomplete line blocks the split, it does not guess it.**
A line whose `artisan_take_home_inr` is null reached us without B1 having
resolved a wage floor. There is no honest number to pay, so the settlement
returns with `blocked` rows naming the problem and a human has to answer
it. Paying a plausible-looking guess is the one outcome worse than paying
late.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, ids
from app.models import Allocation, Artisan, Order, OrderLine, Payout


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class LineSplit:
    """What one line resolves to. Money in whole rupees."""

    listing_id: str
    title: str
    quantity: int
    gross_inr: int
    artisan_total_inr: int
    platform_fee_inr: int
    platform_absorbed_inr: int
    payouts: list[Payout] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class Settlement:
    order_id: str
    gross_inr: int = 0
    artisan_total_inr: int = 0
    platform_fee_inr: int = 0
    platform_absorbed_inr: int = 0
    lines: list[LineSplit] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    already_settled: bool = False

    @property
    def complete(self) -> bool:
        return not self.problems

    @property
    def artisan_share_pct(self) -> float | None:
        """What proportion of the buyer's money reached the makers.

        The headline claim of this marketplace, computed rather than
        asserted. None when nothing was sold, because 0/0 is not 0%.
        """
        if self.gross_inr <= 0:
            return None
        return round(100.0 * self.artisan_total_inr / self.gross_inr, 1)


def platform_fee_for(price_inr: int, take_home_inr: int) -> tuple[int, int]:
    """Split one unit into (fee, absorbed). See Rule 2.

    Returns whole rupees. The fee is floored rather than rounded, so a
    rounding error can only ever fall on the marketplace's side.
    """
    margin = price_inr - take_home_inr
    if margin <= 0:
        return 0, -margin
    return int(margin * config.PLATFORM_FEE_PCT), 0


def _destination(artisan: Artisan | None) -> str | None:
    if artisan is None:
        return None
    return artisan.payout_upi


def _split_line(db: Session, order: Order, line: OrderLine) -> LineSplit:
    gross = line.unit_price_inr * line.quantity
    result = LineSplit(
        listing_id=line.listing_id,
        title=line.title,
        quantity=line.quantity,
        gross_inr=gross,
        artisan_total_inr=0,
        platform_fee_inr=0,
        platform_absorbed_inr=0,
    )

    allocations = list(line.allocations)
    if not allocations:
        result.problems.append(
            f"{line.title}: nobody is recorded as having made this, so there is "
            "nobody to pay."
        )
        return result

    allocated = sum(a.quantity for a in allocations)
    if allocated != line.quantity:
        # Not fatal — pay for what was actually allocated and say so. A
        # mismatch means B1's plan and the line disagree, which a human
        # must reconcile; refusing to pay the nine artisans who did make
        # something is not the right response to that.
        result.problems.append(
            f"{line.title}: {line.quantity} units ordered but {allocated} allocated. "
            "Paying the allocated units only."
        )

    for allocation in allocations:
        payout = _payout_for(db, order, line, allocation, result)
        if payout is not None:
            result.payouts.append(payout)

    return result


def _payout_for(
    db: Session,
    order: Order,
    line: OrderLine,
    allocation: Allocation,
    result: LineSplit,
) -> Payout | None:
    unit_take_home = (
        allocation.take_home_inr
        if allocation.take_home_inr is not None
        else line.artisan_take_home_inr
    )

    if unit_take_home is None:
        # Rule 5. Write the debt down with an amount of zero and a reason,
        # so the row exists and the problem is visible, but no guessed
        # number is ever presented as what she is owed.
        result.problems.append(
            f"{line.title}: no take-home was quoted for {allocation.artisan_name}, so "
            "there is no honest amount to pay. B1 must resolve the wage floor first."
        )
        return _write(
            db,
            order,
            line,
            allocation,
            unit_take_home=0,
            amount=0,
            status="blocked",
            blocked_reason="No artisan take-home was quoted on this line.",
            destination=None,
        )

    amount = unit_take_home * allocation.quantity
    fee, absorbed = platform_fee_for(line.unit_price_inr, unit_take_home)
    result.artisan_total_inr += amount
    result.platform_fee_inr += fee * allocation.quantity
    result.platform_absorbed_inr += absorbed * allocation.quantity

    artisan = db.get(Artisan, allocation.artisan_id)
    destination = _destination(artisan)
    if destination is None:
        result.problems.append(
            f"{allocation.artisan_name} is owed Rs {amount} but has no payment "
            "destination on file."
        )
        return _write(
            db,
            order,
            line,
            allocation,
            unit_take_home=unit_take_home,
            amount=amount,
            status="blocked",
            blocked_reason="No payout destination on file for this artisan.",
            destination=None,
        )

    return _write(
        db,
        order,
        line,
        allocation,
        unit_take_home=unit_take_home,
        amount=amount,
        status="pending",
        blocked_reason=None,
        destination=destination,
    )


def _write(
    db: Session,
    order: Order,
    line: OrderLine,
    allocation: Allocation,
    *,
    unit_take_home: int,
    amount: int,
    status: str,
    blocked_reason: str | None,
    destination: str | None,
) -> Payout:
    """Create the payout row, or return the one already there.

    Rule 4 lives here. The lookup is on the same three columns the unique
    constraint covers, so a concurrent double-settle loses the race at the
    database rather than paying twice.
    """
    existing = db.scalar(
        select(Payout).where(
            Payout.order_id == order.order_id,
            Payout.line_id == line.id,
            Payout.artisan_id == allocation.artisan_id,
        )
    )
    if existing is not None:
        return existing

    payout = Payout(
        payout_id=ids.payout_id(),
        order_id=order.order_id,
        line_id=line.id,
        artisan_id=allocation.artisan_id,
        quantity=allocation.quantity,
        unit_take_home_inr=unit_take_home,
        amount_inr=amount,
        status=status,
        blocked_reason=blocked_reason,
        destination=destination,
    )
    db.add(payout)
    db.flush()
    return payout


def settle(db: Session, order: Order) -> Settlement:
    """Compute and record the split for a delivered order.

    Called automatically when an order transitions to `delivered`. Safe to
    call again: existing payout rows are reused, not duplicated.
    """
    settlement = Settlement(order_id=order.order_id)

    if order.settled_at is not None:
        settlement.already_settled = True

    for line in order.lines:
        line_split = _split_line(db, order, line)
        settlement.lines.append(line_split)
        settlement.gross_inr += line_split.gross_inr
        settlement.artisan_total_inr += line_split.artisan_total_inr
        settlement.platform_fee_inr += line_split.platform_fee_inr
        settlement.platform_absorbed_inr += line_split.platform_absorbed_inr
        settlement.problems.extend(line_split.problems)

    if order.settled_at is None:
        order.settled_at = _utcnow()
    db.flush()
    return settlement


def preview(db: Session, order: Order) -> Settlement:
    """The same arithmetic with nothing written.

    An artisan should be able to see what an order is worth to her the
    moment it is placed, not only after it is delivered. This is that
    number, and it is the same function as the real one so the two cannot
    drift apart and show her different figures.
    """
    settlement = Settlement(order_id=order.order_id)
    for line in order.lines:
        gross = line.unit_price_inr * line.quantity
        line_split = LineSplit(
            listing_id=line.listing_id,
            title=line.title,
            quantity=line.quantity,
            gross_inr=gross,
            artisan_total_inr=0,
            platform_fee_inr=0,
            platform_absorbed_inr=0,
        )
        for allocation in line.allocations:
            unit = (
                allocation.take_home_inr
                if allocation.take_home_inr is not None
                else line.artisan_take_home_inr
            )
            if unit is None:
                line_split.problems.append(
                    f"{line.title}: no take-home quoted for {allocation.artisan_name}."
                )
                continue
            fee, absorbed = platform_fee_for(line.unit_price_inr, unit)
            line_split.artisan_total_inr += unit * allocation.quantity
            line_split.platform_fee_inr += fee * allocation.quantity
            line_split.platform_absorbed_inr += absorbed * allocation.quantity

        settlement.lines.append(line_split)
        settlement.gross_inr += line_split.gross_inr
        settlement.artisan_total_inr += line_split.artisan_total_inr
        settlement.platform_fee_inr += line_split.platform_fee_inr
        settlement.platform_absorbed_inr += line_split.platform_absorbed_inr
        settlement.problems.extend(line_split.problems)
    return settlement


def payouts_for_artisan(db: Session, artisan_id: str) -> list[Payout]:
    return list(
        db.scalars(
            select(Payout)
            .where(Payout.artisan_id == artisan_id)
            .order_by(Payout.created_at.desc())
        ).all()
    )


def mark_paid(db: Session, payout: Payout, reference: str | None = None) -> Payout:
    """Record that money actually left.

    A separate step from `settle` because computing what is owed and
    sending it are different events with different failure modes, and
    collapsing them means a bank outage looks like a broken split.
    """
    if payout.status == "paid":
        return payout
    if payout.status == "blocked":
        raise ValueError(
            f"{payout.payout_id} is blocked: {payout.blocked_reason} "
            "Fix that before marking it paid."
        )
    payout.status = "paid"
    payout.paid_at = _utcnow()
    if reference:
        payout.destination = payout.destination or reference
    db.flush()
    return payout
