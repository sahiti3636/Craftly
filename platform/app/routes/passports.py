"""Passport lookup and QR images.

`/passports/by-code/{code}` is the most important unauthenticated endpoint
in this service. Someone is standing at a mela holding a pot, and the
question is "did a person really make this, and who". Everything about
this router is shaped by that: no auth, no rate limit that could refuse a
genuine scan, generous parsing of a code someone typed by hand, and a
resolvable answer even for a passport that has been revoked.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import passports
from app.contracts import Passport
from app.db import session

router = APIRouter(tags=["passports"])


@router.get("/passports/by-listing/{listing_id}", response_model=Passport)
def by_listing(listing_id: str, db: Session = Depends(session)) -> Passport:
    row = passports.by_listing(db, listing_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No passport for that listing.")
    return passports.to_contract(row)


@router.get("/passports/by-code/{code}", response_model=Passport)
def by_code(code: str, db: Session = Depends(session)) -> Passport:
    row = passports.by_code(db, code)
    if row is None:
        # C1 renders this 404 as "we cannot verify this" with a box to
        # retype the code, not as a generic not-found page. Being checkable
        # is the entire value of the passport, so a failed check has to
        # look like a failed check rather than a broken link.
        raise HTTPException(status_code=404, detail="We cannot verify that code.")
    return passports.to_contract(row)


@router.get("/qr/{code}.png", response_class=FileResponse)
def qr_png(code: str, db: Session = Depends(session)) -> FileResponse:
    """The QR image for a code, minted on first request and cached.

    Minted for known codes only. Generating a QR for any string someone
    asks for would turn this into an open image generator pointed at a URL
    an attacker chooses, and those QRs would carry this project's domain.
    """
    row = passports.by_code(db, code)
    if row is None:
        raise HTTPException(status_code=404, detail="We cannot verify that code.")
    path = passports.render_qr_png(row.verification_code)
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )
