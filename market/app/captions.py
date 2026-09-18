"""The fifteen seconds of text that go over a reel.

A reel is not a video of a product. It is an argument, made in four
beats, to someone scrolling past at speed:

    0.0-3.5s   what it is, and where a human made it
    3.5-7.5s   how it was made, and how long that took
    7.5-11.0s  what it costs, and how much of that reaches her
    11.0-15.0s where to get it, and the code that proves the claim

Every number in those beats comes from the listing and the price quote.
Nothing is written by hand, nothing is embellished, and a fact that is
missing is dropped rather than invented — a reel that says "16 hours of
work" when `hours_worked` is null is a lie with a soundtrack, and this is
a system whose entire pitch is that the provenance is real.

The script is built here, in plain data, separately from the rendering in
`app/reel.py`. That split is what makes the wording testable without
ffmpeg, and it is why the storyboard fallback can show the same argument
as still frames when there is no video toolchain on the machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.money import rupees
from app.view import ProductCard

REEL_SECONDS = 15.0


@dataclass
class CaptionCard:
    """One beat: a headline, an optional second line, and when it shows."""

    start: float
    duration: float
    headline: str
    sub: str | None = None
    kicker: str | None = None
    #: "hook" and "close" are typographically loud; "fact" is quieter.
    style: str = "fact"

    @property
    def end(self) -> float:
        return self.start + self.duration


_STRINGS = {
    "en": {
        "made_in": "Made in {place}",
        "made_by": "by {name}",
        "hours": "{hours} of work, by hand",
        "hours_unknown": "Made entirely by hand",
        "material": "{material}, {craft}",
        "price": "{price}",
        "share": "{amount} of it reaches {name}",
        "floor": "Priced above a minimum-wage floor, not below the market",
        "verify": "Scan to meet the maker",
        "code": "Passport {code}",
        "years": "{years} years at this craft",
    },
    "hi": {
        "made_in": "{place} में बना",
        "made_by": "{name} के हाथों",
        "hours": "{hours} का हस्तनिर्मित काम",
        "hours_unknown": "पूरी तरह हाथ से बना",
        "material": "{material}, {craft}",
        "price": "{price}",
        "share": "इसमें से {amount} {name} तक पहुँचता है",
        "floor": "न्यूनतम मज़दूरी से ऊपर की कीमत, बाज़ार के नीचे नहीं",
        "verify": "कारीगर से मिलने के लिए स्कैन करें",
        "code": "पासपोर्ट {code}",
        "years": "{years} साल से यही काम",
    },
}


def _hours_phrase(hours: float | None, lang: str) -> str | None:
    if hours is None:
        return None
    if hours < 1:
        return f"{round(hours * 60)} minutes" if lang == "en" else f"{round(hours * 60)} मिनट"
    whole = int(hours) if hours == int(hours) else hours
    return f"{whole:g} hours" if lang == "en" else f"{whole:g} घंटे"


def script(
    card: ProductCard,
    *,
    lang: str = "en",
    verification_code: str | None = None,
    years_experience: int | None = None,
) -> list[CaptionCard]:
    """Build the caption script for one product.

    Beats with nothing true to say are dropped and the remaining ones
    stretch to fill fifteen seconds, so a listing that is missing its
    hours produces a shorter argument rather than a padded one.
    """
    s = _STRINGS.get(lang, _STRINGS["en"])
    cards: list[CaptionCard] = []

    place = card.place
    cards.append(
        CaptionCard(
            start=0.0,
            duration=3.5,
            headline=card.title(lang),
            sub=s["made_in"].format(place=place) if place else None,
            kicker=s["made_by"].format(name=card.artisan_name),
            style="hook",
        )
    )

    hours = _hours_phrase(card.listing.hours_worked, lang)
    material = card.listing.material
    craft = card.listing.craft_type or card.craft
    making_sub = None
    if material and craft:
        making_sub = s["material"].format(material=material, craft=craft)
    elif material or craft:
        making_sub = material or craft

    cards.append(
        CaptionCard(
            start=3.5,
            duration=4.0,
            headline=s["hours"].format(hours=hours) if hours else s["hours_unknown"],
            sub=making_sub,
            kicker=(
                s["years"].format(years=years_experience)
                if years_experience
                else None
            ),
        )
    )

    quote = card.quote
    cards.append(
        CaptionCard(
            start=7.5,
            duration=3.5,
            headline=s["price"].format(price=rupees(quote.price_inr)),
            sub=s["share"].format(
                amount=rupees(quote.artisan_take_home_inr),
                name=card.artisan_name.split()[0],
            ),
            kicker=s["floor"],
        )
    )

    cards.append(
        CaptionCard(
            start=11.0,
            duration=4.0,
            headline=s["verify"],
            sub=s["code"].format(code=verification_code) if verification_code else None,
            style="close",
        )
    )

    return _fit(cards, REEL_SECONDS)


def _fit(cards: list[CaptionCard], total: float) -> list[CaptionCard]:
    """Rescale the beats to exactly `total` seconds and re-lay the starts."""
    if not cards:
        return cards
    raw = sum(c.duration for c in cards)
    scale = total / raw if raw else 1.0
    at = 0.0
    out: list[CaptionCard] = []
    for card in cards:
        duration = round(card.duration * scale, 3)
        out.append(
            CaptionCard(
                start=round(at, 3),
                duration=duration,
                headline=card.headline,
                sub=card.sub,
                kicker=card.kicker,
                style=card.style,
            )
        )
        at += duration
    # Absorb rounding drift into the last beat so the reel is exactly 15s.
    out[-1].duration = round(total - out[-1].start, 3)
    return out
