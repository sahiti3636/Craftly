import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schema import Listing

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "schema" / "listing.example.json"

MINIMAL_REQUIRED = {"listing_id": "lst_1", "artisan_id": "art_1"}

NULLABLE_EXTRACTED_FIELDS = [
    "source_language",
    "transcript_raw",
    "title_en",
    "title_hi",
    "description_en",
    "description_hi",
    "summary_spoken",
    "category",
    "craft_type",
    "material",
    "colours",
    "dimensions",
    "material_cost_inr",
    "hours_worked",
    "image_original_url",
    "image_clean_url",
]


def test_example_file_matches_schema():
    data = json.loads(EXAMPLE_PATH.read_text())
    listing = Listing(**data)
    assert listing.listing_id == data["listing_id"]


def test_minimal_listing_only_requires_id_fields():
    listing = Listing(**MINIMAL_REQUIRED)
    assert listing.listing_id == "lst_1"
    assert listing.artisan_id == "art_1"


@pytest.mark.parametrize("field", NULLABLE_EXTRACTED_FIELDS)
def test_extracted_fields_are_nullable(field):
    listing = Listing(**MINIMAL_REQUIRED, **{field: None})
    assert getattr(listing, field) is None


def test_missing_extracted_fields_default_to_none_not_zero_or_empty():
    listing = Listing(**MINIMAL_REQUIRED)
    assert listing.material_cost_inr is None
    assert listing.hours_worked is None
    assert listing.colours is None


def test_material_cost_and_hours_worked_are_never_silently_zeroed():
    # A None must round-trip as None, not get coerced to 0 anywhere in
    # (de)serialization.
    listing = Listing(**MINIMAL_REQUIRED, material_cost_inr=None, hours_worked=None)
    dumped = listing.model_dump()
    assert dumped["material_cost_inr"] is None
    assert dumped["hours_worked"] is None


def test_confidence_and_needs_confirmation_default_to_empty():
    listing = Listing(**MINIMAL_REQUIRED)
    assert listing.confidence == {}
    assert listing.needs_confirmation == []


def test_listing_id_and_artisan_id_are_required():
    with pytest.raises(ValidationError):
        Listing(artisan_id="art_1")
    with pytest.raises(ValidationError):
        Listing(listing_id="lst_1")


def test_created_at_defaults_when_omitted():
    listing = Listing(**MINIMAL_REQUIRED)
    assert listing.created_at is not None
