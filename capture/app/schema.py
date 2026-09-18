"""The Listing contract.

This is the one artifact the extraction pipeline produces and every
downstream consumer (storefront, artisan review UI, search indexer) reads.
No AI/extraction logic lives here — this module only defines the shape of
the data.

Fields fall into three groups:
  - Identity/system fields (never null): listing_id, artisan_id, created_at.
  - Raw input fields (nullable — capture can fail or not have run yet):
    source_language, transcript_raw, image_original_url, image_clean_url.
  - Extracted fields (nullable — extraction can fail or be uncertain):
    everything from title_en through hours_worked.

Rule: a null on any extracted field means "ask the artisan". Never default
a missing number to 0 — material_cost_inr=0 and material_cost_inr=None are
different facts, and collapsing them loses information a human needs to
fix.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class Listing(BaseModel):
    listing_id: str = Field(
        ...,
        description="Unique identifier for this listing, assigned when the record is created.",
    )
    artisan_id: str = Field(
        ...,
        description="Identifier of the artisan who owns this listing.",
    )

    source_language: str | None = Field(
        None,
        description=(
            "BCP-47 language code of the artisan's spoken input (e.g. 'hi', 'ta'). "
            "Null if language detection has not run yet or failed."
        ),
    )
    transcript_raw: str | None = Field(
        None,
        description=(
            "Verbatim speech-to-text transcript of the artisan's description, "
            "in source_language. Null until transcription has run."
        ),
    )

    title_en: str | None = Field(None, description="Generated product title in English.")
    title_hi: str | None = Field(None, description="Generated product title in Hindi.")
    description_en: str | None = Field(
        None, description="Generated product description in English."
    )
    description_hi: str | None = Field(
        None, description="Generated product description in Hindi."
    )

    summary_spoken: str | None = Field(
        None,
        description=(
            "Short, plain-text readback of the listing in source_language, "
            "meant to be read aloud (e.g. via TTS) to the artisan for confirmation."
        ),
    )

    category: str | None = Field(
        None, description="Top-level product category, e.g. 'textiles', 'pottery'."
    )
    craft_type: str | None = Field(
        None, description="Specific craft technique, e.g. 'block printing', 'wheel-thrown'."
    )
    material: str | None = Field(
        None, description="Primary material used, e.g. 'terracotta', 'cotton'."
    )
    colours: list[str] | None = Field(
        None,
        description=(
            "Colours present in the product. Null if colour has not been "
            "extracted yet; an empty list is a valid extracted result meaning "
            "'no distinct colours identified'."
        ),
    )
    dimensions: str | None = Field(
        None,
        description="Free-text physical dimensions as stated or inferred, e.g. '12cm x 8cm x 5cm'.",
    )

    material_cost_inr: int | None = Field(
        None,
        description=(
            "Cost of materials in INR, as stated by the artisan. "
            "Null means unknown — never defaulted to 0."
        ),
    )
    hours_worked: float | None = Field(
        None,
        description=(
            "Hours the artisan spent making the item. "
            "Null means unknown — never defaulted to 0."
        ),
    )

    confidence: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-field extraction confidence in [0, 1], keyed by field name "
            "(e.g. {'title_en': 0.92}). Only fields that were actually "
            "extracted appear here; a field absent from this map was never "
            "attempted."
        ),
    )
    needs_confirmation: list[str] = Field(
        default_factory=list,
        description=(
            "Names of Listing fields the artisan should be asked to confirm "
            "before publishing, e.g. because confidence was low or the field "
            "came back null."
        ),
    )

    image_original_url: str | None = Field(
        None,
        description="URL of the artisan's original, unedited product photo. Null until uploaded.",
    )
    image_clean_url: str | None = Field(
        None,
        description=(
            "URL of the cleaned-up (background-removed/enhanced) product photo. "
            "Null until image cleanup has run."
        ),
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when this listing record was created.",
    )
