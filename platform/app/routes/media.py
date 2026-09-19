"""Uploading and serving photos, making-clips and voice notes.

Upload needs a token; serving does not. A product photo is on a public
shop window and a passport image is scanned by a stranger at a mela, so
gating reads would break both. Writes are gated because disk is finite and
an open upload endpoint is someone else's free file host by the weekend.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import auth, config, ids, media
from app.db import session
from app.models import Account, Listing, Media
from app.schemas import MediaOut

router = APIRouter(tags=["media"])


@router.post("/media", response_model=MediaOut, status_code=201)
async def upload(
    file: UploadFile = File(...),
    listing_id: str | None = Form(None),
    account: Account = Depends(auth.current_artisan),
    db: Session = Depends(session),
) -> MediaOut:
    """Store a file and return the URL to put on a listing.

    Content-addressed, so re-uploading the same bytes is free and returns
    the same URL — see `app/media.py` for why that matters to a phone on a
    patchy connection.
    """
    data = await file.read()
    try:
        stored = media.store().put(data)
    except media.UnsupportedMedia as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    if listing_id is not None:
        listing = db.get(Listing, listing_id)
        if listing is None or listing.artisan_id != account.artisan_id:
            raise HTTPException(status_code=404, detail="No such listing.")

    existing = (
        db.query(Media)
        .filter(Media.sha256 == stored.sha256, Media.artisan_id == account.artisan_id)
        .first()
    )
    if existing is not None:
        return _out(existing, stored.filename, deduplicated=True)

    row = Media(
        media_id=ids.media_id(),
        sha256=stored.sha256,
        filename=stored.filename,
        content_type=stored.content_type,
        bytes=stored.bytes,
        kind=media.kind_for(stored.content_type),
        listing_id=listing_id,
        artisan_id=account.artisan_id,
    )
    db.add(row)
    db.commit()
    return _out(row, stored.filename, deduplicated=False)


@router.get("/media/{filename}", response_class=FileResponse)
def serve(filename: str) -> FileResponse:
    """Serve a stored file.

    The filename *is* the content hash, so it is validated as one before
    anything touches the filesystem: a request for `../../secrets` is not a
    64-character hex string and never becomes a path.

    Immutable caching is safe here for the same reason — the bytes at a
    content address cannot change, so a year-long cache can never go stale.
    """
    path = media.store().resolve(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="No such file.")
    return FileResponse(
        path,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _out(row: Media, filename: str, deduplicated: bool) -> MediaOut:
    url = f"/media/{filename}"
    return MediaOut(
        media_id=row.media_id,
        url=url,
        absolute_url=f"{config.SELF_URL}{url}",
        sha256=row.sha256,
        content_type=row.content_type,
        kind=row.kind,
        bytes=row.bytes,
        deduplicated=deduplicated,
    )
