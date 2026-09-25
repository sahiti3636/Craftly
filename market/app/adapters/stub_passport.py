"""Craft passports assembled from the seed catalogue. Stands in for B2.

B2 owns the real passport: it holds the artisan record, the making clip,
the issue date and the minted QR, and it is the thing that makes the
claim "a person in this village made this" checkable rather than
decorative. C1 only renders it — but the renderer needed something to
render, so this module derives a passport from the catalogue join and
mints a QR on demand.

Two things here are real decisions rather than scaffolding, and should
survive into B2:

* **The verification code is short, spoken, and unambiguous.** It goes on
  a paper hang-tag at a mela. The alphabet drops 0/O and 1/I/L, because
  the failure mode is not a cryptographic attack, it is a buyer squinting
  at a smudged tag and reading it down a phone line.

* **The QR points at a code, not at a listing id.** A tag outlives a
  listing. Resolving the code server-side means a sold, renamed or
  re-photographed listing still answers the scan.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app import config
from app.contracts import Passport, PassportStep
from app.ports import CatalogEntry, CatalogSource

#: No 0/O, no 1/I/L. See module docstring.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def verification_code(listing_id: str) -> str:
    """Deterministic, so the same object always carries the same code."""
    digest = hashlib.sha256(listing_id.encode("utf-8")).digest()
    chars = [_ALPHABET[b % len(_ALPHABET)] for b in digest[:8]]
    return f"CR-{''.join(chars[:4])}-{''.join(chars[4:8])}"


def passport_url(code: str) -> str:
    return f"{config.PUBLIC_URL}/p/{code}"


class StubPassportSource:
    def __init__(self, catalog: CatalogSource, media_dir: Path | None = None) -> None:
        self._catalog = catalog
        self._media_dir = Path(media_dir or config.MEDIA_DIR)
        self._by_code: dict[str, str] = {}
        self._index()

    def _index(self) -> None:
        for entry in self._catalog.all_entries():
            self._by_code[verification_code(entry.listing_id)] = entry.listing_id

    # -- PassportSource --------------------------------------------------

    def by_listing(self, listing_id: str) -> Passport | None:
        entry = self._catalog.get_entry(listing_id)
        if entry is None:
            return None
        return self._build(entry)

    def by_code(self, code: str) -> Passport | None:
        listing_id = self._by_code.get(code.strip().upper())
        if listing_id is None:
            # Tolerate someone typing the code without its dashes.
            squashed = code.strip().upper().replace("-", "")
            for known, lid in self._by_code.items():
                if known.replace("-", "") == squashed:
                    listing_id = lid
                    break
        if listing_id is None:
            return None
        return self.by_listing(listing_id)

    # -- internals -------------------------------------------------------

    def _build(self, entry: CatalogEntry) -> Passport:
        listing = entry.listing
        artisan = entry.artisan
        code = verification_code(listing.listing_id)

        making_clip = self._media_dir / f"{listing.listing_id}_making.mp4"
        made_at = ", ".join(p for p in (artisan.village, artisan.district) if p) or None

        chain = [
            PassportStep(
                label="Made by hand",
                detail=(
                    f"{artisan.name}, {made_at}" if made_at else artisan.name
                ),
            ),
            PassportStep(
                # Every listing this stub knows comes from the seed files,
                # typed rather than spoken; B2 says the same of its seed.
                label="Sample listing",
                detail="Part of Craftly's sample catalogue, not catalogued from a voice note.",
                at=listing.created_at,
            ),
            PassportStep(
                label="Priced against a wage floor",
                detail=(
                    f"{listing.hours_worked:g} hours of her work and the cost of materials "
                    "set the lowest price this piece can sell for."
                    if listing.hours_worked is not None
                    else "Hours of work still to be confirmed with the artisan."
                ),
            ),
            PassportStep(
                label="Passport issued",
                detail=f"Verification code {code}",
            ),
        ]

        return Passport(
            listing_id=listing.listing_id,
            verification_code=code,
            artisan=artisan,
            craft_type=listing.craft_type,
            material=listing.material,
            hours_worked=listing.hours_worked,
            material_cost_inr=listing.material_cost_inr,
            made_at=made_at,
            issued_at=listing.created_at,
            making_clip_url=(
                f"/media/{making_clip.name}" if making_clip.exists() else None
            ),
            image_url=listing.image_clean_url or listing.image_original_url,
            qr_url=f"/qr/{code}.png",
            chain=chain,
        )


def render_qr_png(code: str, out_dir: Path | None = None) -> Path:
    """Mint the QR image for a passport code. Cached on disk.

    B2 owns QR minting in the finished system — this exists so that the
    QR landing page can be demonstrated end to end, with a code someone
    can actually scan off a screen, before B2 ships.
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
