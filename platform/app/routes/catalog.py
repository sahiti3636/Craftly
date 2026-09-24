"""The catalogue endpoints C1 reads.

Shapes here are not negotiable from this side: they are exactly what
`market/app/adapters/http_platform.py` already parses, down to the key
names. C1 wrote that adapter against a service that did not exist; this
router is the half of that conversation that was missing.

No auth. A catalogue is a shop window, and a shop window that needs a
token is a shop nobody walks past.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from pydantic import BaseModel
from sqlalchemy import select

from app import auth, catalog
from app.contracts import Artisan, CatalogEntry
from app.db import session
from app.models import Account

router = APIRouter(tags=["catalog"])


@router.get("/catalog/entries", response_model=list[CatalogEntry])
def all_entries(db: Session = Depends(session)) -> list[CatalogEntry]:
    """Every published listing, joined with its maker and its stock.

    Returns the whole catalogue because C1 filters in-process — honest at
    a few thousand items and wrong at a million. When it becomes wrong the
    query pushes down into here and C1's screens do not notice, which is
    what the seam was for.
    """
    return catalog.all_entries(db)


@router.get("/catalog/entries/{listing_id}", response_model=CatalogEntry)
def one_entry(listing_id: str, db: Session = Depends(session)) -> CatalogEntry:
    entry = catalog.get_entry(db, listing_id)
    if entry is None:
        # 404 for "no such listing" and for "exists but is a draft" alike.
        # Distinguishing them would leak an artisan's unfinished work to
        # anyone who could guess an id.
        raise HTTPException(status_code=404, detail="No such listing.")
    return entry


class ArtisanContact(BaseModel):
    """How to reach an artisan, and whether she agreed to be called."""

    artisan_id: str
    has_account: bool
    phone: str | None = None
    language: str | None = None
    ai_call_consent: bool = False


@router.get("/artisans/{artisan_id}", response_model=Artisan)
def artisan(artisan_id: str, db: Session = Depends(session)) -> Artisan:
    """The public profile a passport shows: name, village, craft, languages."""
    found = catalog.artisan(db, artisan_id)
    if found is None:
        raise HTTPException(status_code=404, detail="No such artisan.")
    return found


@router.get("/artisans/{artisan_id}/contact", response_model=ArtisanContact)
def artisan_contact(
    artisan_id: str,
    _: Account = Depends(auth.current_service),
    db: Session = Depends(session),
) -> ArtisanContact:
    """For C2's order calls. Service token only — a phone number is not
    public. An artisan who has never signed in has no account, so no phone
    and no consent: C2 does not call her."""
    if catalog.artisan(db, artisan_id) is None:
        raise HTTPException(status_code=404, detail="No such artisan.")
    account = db.scalar(select(Account).where(Account.artisan_id == artisan_id, Account.role == "artisan"))
    if account is None:
        return ArtisanContact(artisan_id=artisan_id, has_account=False)
    return ArtisanContact(
        artisan_id=artisan_id,
        has_account=True,
        phone=account.phone,
        language=account.language,
        ai_call_consent=bool(account.ai_call_consent),
    )


@router.get("/clusters/{cluster_id}/members", response_model=list[Artisan])
def cluster_members(cluster_id: str, db: Session = Depends(session)) -> list[Artisan]:
    return catalog.cluster_members(db, cluster_id)


@router.get("/clusters/{cluster_id}/entries", response_model=list[CatalogEntry])
def cluster_entries(
    cluster_id: str,
    craft_type: str | None = Query(None),
    db: Session = Depends(session),
) -> list[CatalogEntry]:
    return catalog.cluster_entries(db, cluster_id, craft_type)
