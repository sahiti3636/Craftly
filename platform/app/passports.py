"""The craft passport: assembly, and the QR that resolves to it.

This is the object the whole project's claim rests on. A marketplace can
say "handmade by artisans" on a banner and nobody can check it. A passport
is the same claim made checkable: a code on a paper tag, a page it
resolves to, a named woman in a named village, and a chain of what
happened to the object with the times it happened at.

Three decisions here are load-bearing.

**The QR encodes C1's `/p/{code}`, not this service's own URL.** A buyer
scanning a tag at a mela should land on a page designed to be read on a
phone with one bar of signal, and that page is C1's. This service mints the
code and answers the lookup; it does not own a buyer-facing screen. So the
QR points at `CRAFTLY_PUBLIC_URL`, which must be the address C1 is served
on and must be reachable from a phone. A QR baked with `localhost` scans to
nothing on anybody's device, which is the single most likely way this
feature fails at a demo.

**The chain is written once, at issue, and stored.** It is a record of
things that happened, not a template rendered over current data. If the
wording of "priced against a wage floor" changes next week, the passport
issued today must still say what it said today.

**A revoked passport resolves.** It returns the passport with its
revocation visible rather than a 404, because the question a buyer is
asking by scanning is "is this real", and silence is a worse answer than
"this tag was withdrawn on the 3rd".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, ids
from app.contracts import Artisan as ArtisanOut
from app.contracts import Passport as PassportOut
from app.contracts import PassportStep as PassportStepOut
from app.models import Listing, Passport, PassportStep


def passport_url(code: str) -> str:
    return f"{config.PUBLIC_URL}/p/{code}"


#: Passports are read by buyers: "Described in Hindi", not "Described in hi".
_LANGUAGE_NAMES = {
    "en": "English", "hi": "Hindi", "te": "Telugu", "ta": "Tamil", "kn": "Kannada",
    "bn": "Bengali", "gu": "Gujarati", "mr": "Marathi", "or": "Odia", "ur": "Urdu",
}


def _chain_for(listing: Listing, code: str, from_voice: bool = True) -> list[tuple[str, str | None, datetime | None]]:
    """The steps, as (label, detail, at).

    A step whose fact is missing says so rather than being dropped or
    guessed. "Hours of work still to be confirmed" is a true sentence about
    an incomplete listing; a passport that silently omits the line reads as
    though the question was never relevant.
    """
    artisan = listing.artisan
    made_at = ", ".join(p for p in (artisan.village, artisan.district) if p) or None

    if listing.hours_worked is not None:
        # Says what the floor is built from, not which wage table the price
        # engine used: that differs between engines and is not a state's
        # notified minimum wage.
        floor_detail = (
            f"{listing.hours_worked:g} hours of her work and the cost of materials "
            "set the lowest price this piece can sell for."
        )
    else:
        floor_detail = "Hours of work still to be confirmed with the artisan."

    return [
        (
            "Made by hand",
            f"{artisan.name}, {made_at}" if made_at else artisan.name,
            None,
        ),
        (
            ("Catalogued from the artisan's own voice",
             f"Described in {_LANGUAGE_NAMES.get(listing.source_language or '', 'her own language')} and "
             "transcribed automatically. No middleman wrote this listing.")
            if from_voice
            # Seeded listings were typed into a file, not spoken; say so.
            else ("Sample listing", "Part of Craftly's sample catalogue, not catalogued from a voice note.")
        ) + (listing.created_at,),
        ("Priced against a wage floor", floor_detail, None),
        ("Passport issued", f"Verification code {code}", None),
    ]


def issue(db: Session, listing: Listing, code: str | None = None, from_voice: bool = True) -> Passport:
    """Mint a passport for a listing, or return the one it already has.

    Idempotent on purpose: publishing a listing twice must not mint a
    second code, because the first one may already be printed on a tag.
    """
    existing = db.get(Passport, listing.listing_id)
    if existing is not None:
        return existing

    code = code or ids.verification_code(listing.listing_id)
    passport = Passport(
        listing_id=listing.listing_id,
        verification_code=code,
        code_key=ids.normalise_code(code),
        issued_at=listing.created_at or datetime.now(timezone.utc),
    )
    db.add(passport)
    for position, (label, detail, at) in enumerate(_chain_for(listing, code, from_voice)):
        db.add(
            PassportStep(
                listing_id=listing.listing_id,
                position=position,
                label=label,
                detail=detail,
                at=at,
            )
        )
    db.flush()
    return passport


def by_listing(db: Session, listing_id: str) -> Passport | None:
    return db.get(Passport, listing_id)


def by_code(db: Session, code: str) -> Passport | None:
    """Resolve a code off a tag, tolerating how it was typed.

    Tries the canonical form first, then single swaps of the characters the
    alphabet deliberately excludes — someone reading `0` for `O` off a
    smudged tag gets their passport rather than a dead end.
    """
    for candidate in ids.code_variants(code):
        row = db.scalar(select(Passport).where(Passport.code_key == candidate))
        if row is not None:
            return row
    return None


def to_contract(passport: Passport) -> PassportOut:
    """The wire shape C1 renders at `/p/{code}`."""
    listing = passport.listing
    artisan = listing.artisan
    made_at = ", ".join(p for p in (artisan.village, artisan.district) if p) or None

    return PassportOut(
        listing_id=listing.listing_id,
        verification_code=passport.verification_code,
        artisan=ArtisanOut(
            artisan_id=artisan.artisan_id,
            name=artisan.name,
            village=artisan.village,
            district=artisan.district,
            state=artisan.state,
            craft=artisan.craft,
            cluster_id=artisan.cluster_id,
            years_experience=artisan.years_experience,
            languages=list(artisan.languages or []),
            photo_url=artisan.photo_url,
            story=artisan.story,
        ),
        craft_type=listing.craft_type,
        material=listing.material,
        hours_worked=listing.hours_worked,
        material_cost_inr=listing.material_cost_inr,
        made_at=made_at,
        issued_at=passport.issued_at,
        making_clip_url=listing.making_clip_url,
        image_url=listing.image_clean_url or listing.image_original_url,
        qr_url=f"/qr/{passport.verification_code}.png",
        chain=[
            PassportStepOut(label=step.label, detail=step.detail, at=step.at)
            for step in passport.steps
        ],
    )


def render_qr_png(code: str, out_dir: Path | None = None) -> Path:
    """Mint and cache the QR image for a code.

    Error correction is Q (25%), not the L most generators default to. A
    hang-tag lives in a bag of pottery, gets rained on at an outdoor mela
    and is scanned in bad light; the extra redundancy costs a slightly
    denser image and buys a code that still reads when a corner is scuffed.
    """
    import qrcode

    directory = Path(out_dir or config.QR_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{code}.png"
    if path.exists():
        return path

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=10,
        border=2,
    )
    qr.add_data(passport_url(code))
    qr.make(fit=True)
    qr.make_image(fill_color="#1b1a17", back_color="white").save(path)
    return path
