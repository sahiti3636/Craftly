"""In-memory store for draft listings, keyed by listing_id.

Holds both the public Listing (the contract in app/schema.py) and the
underlying ExtractedFields it was built from. The two aren't the same
shape: ExtractedFields carries product_description_facts, which Listing
has no field for but which app/describe.py uses when regenerating a
title/description after a correction — losing it between draft and
confirm would silently degrade regeneration quality. Keeping both here
avoids that without changing the Listing contract.

This is a single-process, in-memory placeholder — it does not survive a
restart and won't work across multiple server workers. Replace with a
real database before this goes anywhere near production.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.extract import ExtractedFields
from app.schema import Listing


@dataclass
class DraftRecord:
    listing: Listing
    extracted_fields: ExtractedFields
    summary_audio_url: str | None = None


_records: dict[str, DraftRecord] = {}


def save(
    listing: Listing,
    extracted_fields: ExtractedFields,
    summary_audio_url: str | None = None,
) -> None:
    _records[listing.listing_id] = DraftRecord(
        listing=listing, extracted_fields=extracted_fields, summary_audio_url=summary_audio_url
    )


def get(listing_id: str) -> DraftRecord | None:
    return _records.get(listing_id)
