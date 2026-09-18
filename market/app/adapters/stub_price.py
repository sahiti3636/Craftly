"""A placeholder price engine. Stands in for B1 until B1 exists.

This is **not** the price engine. B1 owns wage floors, comparable-listing
scraping and per-channel adjustment. What lives here is the smallest
thing that lets a buyer surface show a real number with a real
justification, built to exactly the `PriceQuote` shape B1 is expected to
return so that swapping it out touches nothing above `app/adapters/`.

The arithmetic, in the order it happens:

    floor      = material cost + (hours x state hourly minimum wage) + wastage
    anchor     = a point inside the comparable market band
    take-home  = max(floor, anchor) + passport premium
    price      = take-home grossed up for the channel's cut

Three decisions in there are product decisions, not implementation
details, and B1 should keep them:

1. **The floor wins ties.** When the market band sits under the floor the
   item is still offered at the floor and `below_floor` is set, so the
   surface can say the market underpays this craft. Matching a market
   that pays below minimum wage is the thing this project exists not to
   do.

2. **A null is not a zero.** If `material_cost_inr` or `hours_worked` is
   null the floor cannot be computed honestly, so `floor_incomplete` is
   set and the surface must refuse the sale rather than guess. Defaulting
   either to 0 would produce a confident, low, wrong floor — the worst of
   the three outcomes.

3. **Volume discounts come out of margin, never out of the floor.** A
   500-unit tier reduces the premium above the floor and stops there. A
   bulk buyer can negotiate the marketplace's share; they cannot
   negotiate the artisan's wage.

The state wage table below is placeholder data. Real rates vary by
district, by skill classification and by notification date, and B1 needs
the actual gazette numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import config
from app.contracts import Channel, PriceQuote
from app.ports import CatalogEntry

#: Placeholder daily minimum wage in INR by state, unskilled-to-semiskilled
#: band. Replace with B1's real, dated table.
DAILY_MINIMUM_WAGE_INR: dict[str, int] = {
    "Andhra Pradesh": 460,
    "Assam": 400,
    "Bihar": 400,
    "Chhattisgarh": 380,
    "Gujarat": 450,
    "Jammu and Kashmir": 400,
    "Jharkhand": 400,
    "Karnataka": 550,
    "Kerala": 600,
    "Madhya Pradesh": 400,
    "Maharashtra": 500,
    "Odisha": 430,
    "Punjab": 430,
    "Rajasthan": 350,
    "Tamil Nadu": 480,
    "Telangana": 460,
    "Uttar Pradesh": 420,
    "West Bengal": 425,
}
DEFAULT_DAILY_WAGE_INR = 420
HOURS_PER_DAY = 8

#: Fraction of material cost allowed for breakage, spoilt dye lots, a
#: cracked firing. Craft has a real reject rate and pricing that assumes
#: a 100% yield underpays every time a piece fails.
WASTAGE_RATE = 0.08

#: What the artisan gets above the floor for a verified craft passport.
#: Fraction of the base take-home.
PASSPORT_PREMIUM_RATE = 0.12

#: Where inside the comparable band to sit, 0 = bottom, 1 = top.
MARKET_ANCHOR = 0.45

#: The cut each channel takes, as a fraction of the buyer-facing price.
#: Own store is a payment-gateway fee; the marketplaces are their
#: published referral rates, rounded.
CHANNEL_FEE_RATE: dict[Channel, float] = {
    Channel.OWN_STORE: 0.03,
    Channel.B2B: 0.02,
    Channel.MELA_QR: 0.02,
    Channel.AMAZON: 0.18,
    Channel.FLIPKART: 0.20,
    Channel.EBAY: 0.15,
}

#: Volume tiers: (minimum quantity, fraction off the margin above floor).
VOLUME_TIERS: tuple[tuple[int, float], ...] = (
    (500, 0.12),
    (200, 0.08),
    (50, 0.04),
    (20, 0.02),
)


def hourly_wage_inr(state: str | None) -> float:
    daily = DAILY_MINIMUM_WAGE_INR.get(state or "", DEFAULT_DAILY_WAGE_INR)
    return daily / HOURS_PER_DAY


def volume_discount(quantity: int) -> float:
    for threshold, rate in VOLUME_TIERS:
        if quantity >= threshold:
            return rate
    return 0.0


def _round_up_10(value: float) -> int:
    """Prices end in a zero. Buyers read 1,240 faster than 1,237."""
    return int(-(-value // 10) * 10)


class StubPriceEngine:
    def __init__(self, bands_path: Path | None = None) -> None:
        self._bands_path = Path(bands_path or config.SEED_DIR / "market_bands.json")
        self._bands = self._load_bands()

    def _load_bands(self) -> dict[str, dict[str, int]]:
        if not self._bands_path.exists():
            return {}
        raw = json.loads(self._bands_path.read_text(encoding="utf-8"))
        return raw.get("bands", {})

    def band(self, listing_id: str) -> tuple[int | None, int | None]:
        entry = self._bands.get(listing_id)
        if not entry:
            return None, None
        return entry.get("low_inr"), entry.get("high_inr")

    # -- PriceEngine -----------------------------------------------------

    def quote(
        self,
        entry: CatalogEntry,
        channel: Channel = Channel.OWN_STORE,
        quantity: int = 1,
    ) -> PriceQuote:
        listing = entry.listing
        state = entry.artisan.state
        wage = hourly_wage_inr(state)

        material = listing.material_cost_inr
        hours = listing.hours_worked
        incomplete = material is None or hours is None

        # An incomplete floor is still computed — a surface that shows no
        # number at all cannot explain why it is refusing to sell. What it
        # must not do is treat the result as trustworthy, which is what
        # `floor_incomplete` is for.
        material_part = material or 0
        hours_part = (hours or 0.0) * wage
        wastage_part = material_part * WASTAGE_RATE
        floor = _round_up_10(material_part + hours_part + wastage_part)

        low, high = self.band(listing.listing_id)
        below_floor = high is not None and high < floor

        if low is not None and high is not None:
            anchor = low + MARKET_ANCHOR * (high - low)
        else:
            anchor = floor

        base_take_home = max(float(floor), anchor)
        premium = _round_up_10(base_take_home * PASSPORT_PREMIUM_RATE)
        take_home = _round_up_10(base_take_home) + premium

        discount = volume_discount(quantity)
        if discount:
            # The discount is taken out of the margin above the floor, so
            # the wage survives any negotiation.
            margin = take_home - floor
            take_home = floor + _round_up_10(margin * (1 - discount))

        fee_rate = CHANNEL_FEE_RATE.get(channel, 0.03)
        price = _round_up_10(take_home / (1 - fee_rate))
        fee = price - take_home

        explanation = self._explain(
            state=state,
            wage=wage,
            material=material,
            hours=hours,
            wastage=wastage_part,
            floor=floor,
            low=low,
            high=high,
            premium=premium,
            channel=channel,
            fee=fee,
            price=price,
            take_home=take_home,
            quantity=quantity,
            discount=discount,
            below_floor=below_floor,
            incomplete=incomplete,
        )

        return PriceQuote(
            listing_id=listing.listing_id,
            channel=channel,
            floor_inr=floor,
            market_low_inr=low,
            market_high_inr=high,
            passport_premium_inr=premium,
            price_inr=price,
            artisan_take_home_inr=take_home,
            channel_fee_inr=fee,
            below_floor=below_floor,
            floor_incomplete=incomplete,
            explanation=explanation,
        )

    def _explain(
        self,
        *,
        state: str | None,
        wage: float,
        material: int | None,
        hours: float | None,
        wastage: float,
        floor: int,
        low: int | None,
        high: int | None,
        premium: int,
        channel: Channel,
        fee: int,
        price: int,
        take_home: int,
        quantity: int,
        discount: float,
        below_floor: bool,
        incomplete: bool,
    ) -> list[str]:
        """The price breakdown, in the words a buyer reads on the page.

        Built here rather than in a template because the reasoning is the
        engine's, not the screen's: when B1 replaces this module its
        explanation replaces this one, and the storefront keeps rendering
        whatever it is handed.
        """
        lines: list[str] = []

        if material is None:
            lines.append("Material cost has not been confirmed with the artisan yet.")
        else:
            lines.append(f"Materials: Rs {material:,}")

        if hours is None:
            lines.append("Hours of work have not been confirmed with the artisan yet.")
        else:
            lines.append(
                f"Work: {hours:g} hours at Rs {wage:.0f}/hour, the "
                f"{state or 'state'} minimum wage = Rs {hours * wage:,.0f}"
            )

        if material:
            lines.append(
                f"Wastage allowance at {int(WASTAGE_RATE * 100)}% of materials: Rs {wastage:,.0f}"
            )

        lines.append(f"Wage floor: Rs {floor:,} — nothing sells below this.")

        if low is not None and high is not None:
            lines.append(f"Comparable listings elsewhere: Rs {low:,} - Rs {high:,}")

        if below_floor:
            lines.append(
                "The open market pays less for this craft than the hours in it are "
                "worth. This piece is listed at its floor instead of matching that."
            )

        if premium:
            lines.append(f"Verified craft passport: Rs {premium:,}")

        if discount:
            lines.append(
                f"Volume tier at {quantity} units: {int(discount * 100)}% off the margin "
                "above the floor. The wage floor is not discounted."
            )

        lines.append(
            f"Artisan receives Rs {take_home:,} of the Rs {price:,} price; Rs {fee:,} is the "
            f"{channel.value.replace('_', ' ')} channel cost."
        )

        if incomplete:
            lines.append(
                "This floor is incomplete because a number is missing, so the piece "
                "cannot be sold until the artisan confirms it."
            )

        return lines
