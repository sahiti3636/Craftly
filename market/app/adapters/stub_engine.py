"""A placeholder order engine. Stands in for B1's capacity pooling.

The real question a B2B buyer asks is "can you make me 500 of these by
Diwali?" and the only honest answers are a plan or a number short. This
module produces a plan of the shape B1 is expected to produce, so that
the B2B portal could be built and tested before the splitter existed.

How the stub splits, which is roughly what a cluster co-ordinator does on
paper:

* The work is **spread proportionally to capacity**, not poured into the
  first artisan until she overflows. Greedy filling looks fine in a test
  and is wrong twice over: 500 diyas given to one woman who makes 900 a
  month is seventeen days in which she can make nothing else, and the
  order takes far longer than the same 500 spread across the village.
  Pooling capacity is the whole reason a cluster exists.
* The artisan whose listing the buyer is looking at gets a weighted share
  rather than an equal one. She put the listing up; she gets more of it.
* Nobody is given a share so small it is not worth the packing —
  participants are capped so each gets a meaningful number of units.
* Anyone whose lead time is longer than the deadline is left out
  entirely, rather than being given work they cannot finish.
* Capacity is prorated by the deadline — a 400-a-month artisan with a
  15-day deadline is a 200 artisan.

What it deliberately does **not** do, and B1 must: balance earnings
across a cluster over time, respect who is mid-order on something else,
or know that two artisans share one kiln and cannot both fire at once.
The stub will happily allocate to two people who are the same bottleneck.
"""

from __future__ import annotations

import math

from app.contracts import Allocation, Channel, SplitPlan
from app.ports import CatalogEntry, CatalogSource, PriceEngine

DAYS_PER_MONTH = 30

#: How much more of an order the listing's own artisan gets than a peer of
#: the same capacity.
PRIMARY_BIAS = 1.6

#: Nobody is added to a split for fewer units than this. Packing, a
#: courier pickup and a phone call cost the same for three pieces as for
#: thirty, and a two-unit share is not worth an artisan's afternoon.
MIN_SHARE = 5


class StubOrderEngine:
    def __init__(self, catalog: CatalogSource, prices: PriceEngine) -> None:
        self._catalog = catalog
        self._prices = prices

    def split(
        self, entry: CatalogEntry, quantity: int, deadline_days: int | None = None
    ) -> SplitPlan:
        if quantity <= 0:
            return SplitPlan(
                listing_id=entry.listing_id,
                quantity_requested=quantity,
                quantity_allocated=0,
                feasible=False,
                shortfall=0,
                notes=["Quantity must be at least 1."],
            )

        notes: list[str] = []
        candidates = self._candidates(entry, deadline_days, notes)

        take_home = self._prices.quote(entry, Channel.B2B, quantity).artisan_take_home_inr

        usable = [c for c in candidates if c["capacity"] > 0]
        shares = self._shares(usable, quantity)

        allocations = [
            Allocation(
                artisan_id=cand["artisan_id"],
                artisan_name=cand["name"],
                village=cand["village"],
                quantity=share,
                lead_time_days=self._days_for(share, cand),
                take_home_inr=take_home,
            )
            for cand, share in zip(usable, shares)
            if share > 0
        ]

        allocated = sum(a.quantity for a in allocations)
        remaining = quantity - allocated
        lead_time = max((a.lead_time_days or 0) for a in allocations) if allocations else None

        if remaining:
            notes.append(
                f"The cluster can commit {allocated} of {quantity} units"
                + (f" within {deadline_days} days." if deadline_days else ".")
            )
        if deadline_days and lead_time and lead_time > deadline_days:
            notes.append(
                f"Production needs about {lead_time} days; the deadline is {deadline_days}."
            )

        feasible = remaining == 0 and not (
            deadline_days and lead_time and lead_time > deadline_days
        )

        return SplitPlan(
            listing_id=entry.listing_id,
            quantity_requested=quantity,
            quantity_allocated=allocated,
            allocations=allocations,
            lead_time_days=lead_time,
            feasible=feasible,
            shortfall=remaining,
            notes=notes,
        )

    # -- internals -------------------------------------------------------

    @staticmethod
    def _shares(candidates: list[dict], quantity: int) -> list[int]:
        """Spread `quantity` across candidates in proportion to capacity.

        Returns a share per candidate, in the order given, each within
        that candidate's capacity. If the cluster cannot cover the order
        everyone ends up maxed out and the caller reports the shortfall.
        """
        if not candidates:
            return []

        capacities = [c["capacity"] for c in candidates]
        if sum(capacities) <= quantity:
            return list(capacities)

        # How many people to involve: enough that each gets a share worth
        # having, and enough that their combined capacity covers the order.
        wanted = max(1, quantity // MIN_SHARE)
        count = min(len(candidates), wanted)
        while count < len(candidates) and sum(capacities[:count]) < quantity:
            count += 1

        caps = capacities[:count]
        weights = [cap * (PRIMARY_BIAS if i == 0 else 1.0) for i, cap in enumerate(caps)]
        total_weight = sum(weights) or 1.0

        shares = []
        remainders = []
        for i, cap in enumerate(caps):
            ideal = quantity * weights[i] / total_weight
            give = min(int(ideal), cap)
            shares.append(give)
            remainders.append((ideal - give, i))

        # Hand out what flooring left over, one unit at a time, biggest
        # fractional claim first — so the rounding does not all land on
        # whoever happens to be first in the list.
        left = quantity - sum(shares)
        for _, i in sorted(remainders, key=lambda pair: -pair[0]):
            if left <= 0:
                break
            if shares[i] < caps[i]:
                shares[i] += 1
                left -= 1

        # Capacity caps can still leave a few units homeless; sweep.
        while left > 0:
            progressed = False
            for i, cap in enumerate(caps):
                if left <= 0:
                    break
                if shares[i] < cap:
                    take = min(cap - shares[i], left)
                    shares[i] += take
                    left -= take
                    progressed = True
            if not progressed:
                break

        return shares + [0] * (len(candidates) - count)

    def _candidates(
        self, entry: CatalogEntry, deadline_days: int | None, notes: list[str]
    ) -> list[dict]:
        """The artisan first, then the rest of her cluster by capacity."""
        cluster_id = entry.artisan.cluster_id
        own_monthly = entry.inventory.monthly_capacity or entry.inventory.sellable_now
        own_lead = entry.inventory.lead_time_days or 0

        candidates: list[dict] = [
            {
                "artisan_id": entry.artisan.artisan_id,
                "name": entry.artisan.name,
                "village": entry.artisan.village,
                "monthly": own_monthly,
                "lead": own_lead,
                "stock": entry.inventory.sellable_now,
            }
        ]

        if cluster_id:
            peers = self._catalog.cluster_entries(cluster_id, entry.listing.craft_type)
            seen = {entry.artisan.artisan_id}
            for peer in peers:
                if peer.artisan.artisan_id in seen:
                    continue
                seen.add(peer.artisan.artisan_id)
                candidates.append(
                    {
                        "artisan_id": peer.artisan.artisan_id,
                        "name": peer.artisan.name,
                        "village": peer.artisan.village,
                        "monthly": peer.inventory.monthly_capacity
                        or peer.inventory.sellable_now,
                        "lead": peer.inventory.lead_time_days or 0,
                        "stock": peer.inventory.sellable_now,
                    }
                )

            # Cluster members with no listing of their own for this craft
            # still have hands. The cluster-level capacity on the listing is
            # what is left over for them, shared equally — a crude stand-in
            # for B1 knowing each member's real throughput.
            members = self._catalog.cluster_members(cluster_id)
            unlisted = [m for m in members if m.artisan_id not in seen]
            cluster_total = entry.inventory.cluster_capacity
            listed_total = sum(c["monthly"] for c in candidates)
            spare = (cluster_total or 0) - listed_total
            if unlisted and spare > 0:
                each = spare // len(unlisted)
                if each > 0:
                    notes.append(
                        f"{len(unlisted)} more artisans in this cluster make the same "
                        "craft without a listing of their own; their capacity is "
                        "estimated from the cluster total."
                    )
                    for member in unlisted:
                        candidates.append(
                            {
                                "artisan_id": member.artisan_id,
                                "name": member.name,
                                "village": member.village,
                                "monthly": each,
                                "lead": own_lead,
                                "stock": 0,
                            }
                        )

        for cand in candidates:
            cand["capacity"] = self._usable_capacity(cand, deadline_days)

        head, tail = candidates[0], candidates[1:]
        tail.sort(key=lambda c: c["capacity"], reverse=True)
        return [head, *tail]

    @staticmethod
    def _usable_capacity(cand: dict, deadline_days: int | None) -> int:
        monthly = max(int(cand["monthly"]), 0)
        stock = max(int(cand["stock"]), 0)
        if deadline_days is None:
            return monthly + stock
        if cand["lead"] and cand["lead"] > deadline_days:
            # Cannot finish even one new unit in time; only what is already
            # made counts.
            return stock
        made_in_window = int(monthly * (deadline_days / DAYS_PER_MONTH))
        return stock + made_in_window

    @staticmethod
    def _days_for(quantity: int, cand: dict) -> int:
        """Lead time for the first unit, plus time to make the rest."""
        monthly = max(int(cand["monthly"]), 1)
        from_stock = min(quantity, max(int(cand["stock"]), 0))
        to_make = quantity - from_stock
        if to_make <= 0:
            return 2  # packing and pickup
        per_day = monthly / DAYS_PER_MONTH
        return int(cand["lead"] or 0) + math.ceil(to_make / max(per_day, 0.1))
