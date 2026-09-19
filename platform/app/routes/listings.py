"""Listings: the door A2's pipeline and A1's offline queue post through.

This is where `capture/app/store.py` stops being an in-memory dict that
dies with the process.

Two rules enforced here rather than trusted:

* **A phone creates listings for its own artisan.** The `artisan_id` on an
  incoming listing is ignored in favour of the token's. A client-supplied
  owner id is how one artisan's work ends up filed under another's name,
  and the artisan whose name is on it is the person whose wage floor is
  being defended.
* **Publishing mints a passport, once.** A listing goes live and acquires
  its verification code in the same operation, because a live listing
  without a passport is a product on a craft marketplace with no proof
  behind it — the exact thing this project exists not to be. Re-publishing
  reuses the existing code, since it may already be printed on a tag.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import auth, catalog, ids, passports
from app.contracts import Channel
from app.db import session
from app.models import Account, Listing
from app.schemas import ListingIn, ListingOut, ListingPatch

router = APIRouter(tags=["listings"])

#: What a listing is offered on unless the artisan says otherwise.
_DEFAULT_CHANNELS = [c.value for c in catalog.DEFAULT_CHANNELS]


def _out(db: Session, listing: Listing) -> ListingOut:
    passport = passports.by_listing(db, listing.listing_id)
    return ListingOut(
        listing=catalog.listing_out(listing),
        published=listing.published,
        channels=[Channel(c) for c in (listing.channels or _DEFAULT_CHANNELS)],
        verification_code=passport.verification_code if passport else None,
        passport_url=(
            passports.passport_url(passport.verification_code) if passport else None
        ),
        qr_url=f"/qr/{passport.verification_code}.png" if passport else None,
        making_clip_url=listing.making_clip_url,
    )


@router.post("/listings", response_model=ListingOut, status_code=201)
def create_listing(
    payload: ListingIn,
    account: Account = Depends(auth.current_artisan),
    db: Session = Depends(session),
) -> ListingOut:
    """Persist a listing A2 produced from a photo and a voice note.

    Idempotent on `listing_id`. A1's offline queue retries a sync it is not
    sure completed, and the honest answer to "I already have this one" is
    the stored listing, not a duplicate product and not an error the app
    has no way to recover from.
    """
    incoming = payload.listing
    listing_id = incoming.listing_id or ids.listing_id()

    existing = db.get(Listing, listing_id)
    if existing is not None:
        if existing.artisan_id != account.artisan_id:
            raise HTTPException(status_code=409, detail="That listing id is already taken.")
        return _out(db, existing)

    listing = Listing(
        listing_id=listing_id,
        artisan_id=account.artisan_id,
        source_language=incoming.source_language,
        transcript_raw=incoming.transcript_raw,
        title_en=incoming.title_en,
        title_hi=incoming.title_hi,
        description_en=incoming.description_en,
        description_hi=incoming.description_hi,
        summary_spoken=incoming.summary_spoken,
        category=incoming.category,
        craft_type=incoming.craft_type,
        material=incoming.material,
        colours=incoming.colours,
        dimensions=incoming.dimensions,
        # Copied straight through, nulls included. See the null rule.
        material_cost_inr=incoming.material_cost_inr,
        hours_worked=incoming.hours_worked,
        confidence=dict(incoming.confidence or {}),
        needs_confirmation=list(incoming.needs_confirmation or []),
        image_original_url=incoming.image_original_url,
        image_clean_url=incoming.image_clean_url,
        making_clip_url=payload.making_clip_url,
        created_at=incoming.created_at,
        published=False,
        channels=[c.value for c in payload.channels] if payload.channels else _DEFAULT_CHANNELS,
    )
    db.add(listing)
    db.flush()

    if payload.inventory is not None:
        catalog.set_inventory(db, listing, **payload.inventory.model_dump(exclude_none=True))

    if payload.publish:
        _publish(db, listing)

    db.commit()
    db.refresh(listing)
    return _out(db, listing)


@router.get("/listings/mine", response_model=list[ListingOut])
def my_listings(
    account: Account = Depends(auth.current_artisan),
    db: Session = Depends(session),
) -> list[ListingOut]:
    """The artisan's own listings, drafts included.

    Drafts are visible here and nowhere else: she has to see the draft to
    confirm it, and nobody else has any business seeing an unfinished one.
    """
    rows = catalog.entries_for_artisan(db, account.artisan_id or "")
    return [_out(db, row) for row in rows]


@router.patch("/listings/{listing_id}", response_model=ListingOut)
def update_listing(
    listing_id: str,
    payload: ListingPatch,
    account: Account = Depends(auth.current_artisan),
    db: Session = Depends(session),
) -> ListingOut:
    """Apply a correction from the confirmation loop, or publish.

    `exclude_unset`, not `exclude_none`: an artisan correcting a wrong
    material cost back to "I do not know" must be able to send an explicit
    null. Treating null as "no change" would make the one correction that
    protects the wage floor the one correction impossible to make.
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="No such listing.")
    if listing.artisan_id != account.artisan_id:
        raise HTTPException(status_code=403, detail="That is not your listing.")

    fields = payload.model_dump(exclude_unset=True)
    inventory_in = fields.pop("inventory", None)
    publish = fields.pop("published", None)
    channels = fields.pop("channels", None)

    for key, value in fields.items():
        setattr(listing, key, value)

    if channels is not None:
        listing.channels = [Channel(c).value for c in channels]

    if inventory_in:
        catalog.set_inventory(db, listing, **{k: v for k, v in inventory_in.items() if v is not None})

    if publish is True:
        _publish(db, listing)
    elif publish is False:
        # Unpublishing hides the listing; it does not revoke the passport.
        # A tag already in a buyer's hand must still resolve, and "this is
        # no longer for sale" is a different claim from "this was never real".
        listing.published = False

    db.commit()
    db.refresh(listing)
    return _out(db, listing)


@router.get("/listings/{listing_id}", response_model=ListingOut)
def get_listing(
    listing_id: str,
    account: Account = Depends(auth.current_artisan),
    db: Session = Depends(session),
) -> ListingOut:
    listing = db.get(Listing, listing_id)
    if listing is None or listing.artisan_id != account.artisan_id:
        raise HTTPException(status_code=404, detail="No such listing.")
    return _out(db, listing)


def _publish(db: Session, listing: Listing) -> None:
    """Go live, and mint the proof that makes going live honest."""
    listing.published = True
    if not listing.channels:
        listing.channels = _DEFAULT_CHANNELS
    passports.issue(db, listing)
