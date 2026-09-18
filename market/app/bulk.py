"""The B2B quote: what a bulk buyer is actually asking, answered honestly.

A retail page answers "how much". A B2B page answers "can you make me 500
of these by the 14th, and who makes them". That is three separate facts —
a price at volume (B1's price engine), a production plan across a cluster
(B1's order engine), and whether the plan fits the date (here) — and the
portal is only useful if it refuses to fudge the third one.

The rule this module exists to enforce: **a quote that cannot be met says
so, with a number.** "340 of your 500 by 14 October, or all 500 by 2
November" is a sentence a procurement officer can work with. A spinner
that eventually produces an order the cluster cannot fill is how an
artisan ends up eating a penalty clause.

`quote()` composes the two engines and adds: minimum order quantity,
deadline arithmetic, the feasible-quantity fallback, and the comparison
against retail so the buyer can see what volume actually bought them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from app.contracts import Channel, SplitPlan
from app.money import rupees
from app.ports import CatalogEntry, OrderEngine, PriceEngine
from app.view import ProductCard, build_card

#: Below this it is a retail order wearing a suit.
MIN_BULK_QUANTITY = 10

#: A ceiling for "how many could you possibly make" probes.
_PROBE_QUANTITY = 10_000_000


def days_until(deadline: date | None, *, today: date | None = None) -> int | None:
    if deadline is None:
        return None
    today = today or datetime.now(timezone.utc).date()
    return (deadline - today).days


@dataclass
class BulkQuote:
    card: ProductCard
    quantity: int
    deadline: date | None
    deadline_days: int | None

    unit_price_inr: int
    retail_unit_price_inr: int
    subtotal_inr: int
    artisan_take_home_inr: int
    artisan_total_inr: int

    split: SplitPlan
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    #: Set when the requested quantity cannot be met by the deadline: the
    #: largest quantity that can be, so the buyer has something to accept.
    max_by_deadline: int | None = None
    #: Set when the full quantity is possible but needs longer.
    days_needed: int | None = None

    @property
    def can_order(self) -> bool:
        return not self.errors and self.split.feasible

    @property
    def unit_price(self) -> str:
        return rupees(self.unit_price_inr)

    @property
    def subtotal(self) -> str:
        return rupees(self.subtotal_inr)

    @property
    def artisan_total(self) -> str:
        return rupees(self.artisan_total_inr)

    @property
    def saving_pct(self) -> int:
        if self.retail_unit_price_inr <= 0:
            return 0
        saved = self.retail_unit_price_inr - self.unit_price_inr
        return max(0, round(100 * saved / self.retail_unit_price_inr))

    @property
    def artisans_involved(self) -> int:
        return len(self.split.allocations)

    @property
    def households_line(self) -> str:
        """The line a CSR buyer is actually shopping for."""
        n = self.artisans_involved
        if n <= 1:
            return "1 artisan household"
        villages = {a.village for a in self.split.allocations if a.village}
        if len(villages) > 1:
            return f"{n} artisan households across {len(villages)} villages"
        return f"{n} artisan households"


def max_feasible_quantity(
    entry: CatalogEntry, engine: OrderEngine, deadline_days: int | None
) -> int:
    """How many this cluster could commit to by that date.

    Asks the splitter for an absurd quantity and reads back how much it
    managed to allocate, rather than binary-searching: the splitter
    already knows the ceiling, and a probe keeps the answer consistent
    with whatever allocation rules it applies.
    """
    probe = engine.split(entry, _PROBE_QUANTITY, deadline_days)
    return probe.quantity_allocated


def quote(
    entry: CatalogEntry,
    *,
    quantity: int,
    prices: PriceEngine,
    engine: OrderEngine,
    deadline: date | None = None,
    today: date | None = None,
) -> BulkQuote:
    errors: list[str] = []
    warnings: list[str] = []

    deadline_days = days_until(deadline, today=today)
    if deadline_days is not None and deadline_days < 0:
        errors.append("That date has already passed.")
        deadline_days = None
    elif deadline_days is not None and deadline_days == 0:
        errors.append("A bulk order needs at least a day to make.")
        deadline_days = None

    if quantity < MIN_BULK_QUANTITY:
        errors.append(
            f"Bulk orders start at {MIN_BULK_QUANTITY} units. "
            "For fewer, buy from the shop page."
        )

    effective_quantity = max(quantity, 1)

    b2b = prices.quote(entry, Channel.B2B, effective_quantity)
    retail = prices.quote(entry, Channel.OWN_STORE, 1)
    card = build_card(entry, b2b)

    if b2b.floor_incomplete:
        errors.append(
            "This piece has no confirmed wage floor yet, so it cannot be quoted. "
            "We are waiting on the artisan to confirm a number."
        )

    split = engine.split(entry, effective_quantity, deadline_days)

    max_by_deadline: int | None = None
    days_needed: int | None = None

    if not split.feasible:
        if split.shortfall:
            max_by_deadline = split.quantity_allocated or None
        if deadline_days is not None:
            unconstrained = engine.split(entry, effective_quantity, None)
            if unconstrained.feasible:
                days_needed = unconstrained.lead_time_days
                if max_by_deadline is None:
                    max_by_deadline = max_feasible_quantity(entry, engine, deadline_days)

    if b2b.below_floor:
        warnings.append(
            "This craft sells for less than its own wage floor on the open market. "
            "The price here is the floor, not the market rate."
        )

    if split.lead_time_days and deadline_days and split.lead_time_days <= deadline_days:
        spare = deadline_days - split.lead_time_days
        if spare < 5:
            warnings.append(
                f"This fits the deadline with {spare} days to spare. "
                "Courier time is not included."
            )

    warnings.extend(split.notes)

    return BulkQuote(
        card=card,
        quantity=quantity,
        deadline=deadline,
        deadline_days=deadline_days,
        unit_price_inr=b2b.price_inr,
        retail_unit_price_inr=retail.price_inr,
        subtotal_inr=b2b.price_inr * effective_quantity,
        artisan_take_home_inr=b2b.artisan_take_home_inr,
        artisan_total_inr=b2b.artisan_take_home_inr * effective_quantity,
        split=split,
        errors=errors,
        warnings=warnings,
        max_by_deadline=max_by_deadline,
        days_needed=days_needed,
    )
