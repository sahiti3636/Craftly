"""Orchestrates the full audio+image -> Listing pipeline, and applies
artisan corrections during the confirmation step.

Kept separate from app/main.py so the HTTP layer stays thin (request
parsing, response shaping, status codes) while the actual business logic
— which steps run, in what order, and what a correction triggers — lives
in one place that's easy to read start to finish and easy to test without
spinning up FastAPI.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app.asr import transcribe
from app.describe import (
    GeneratedDescriptions,
    _has_anything_to_report,
    generate_descriptions,
    generate_spoken_summary,
)
from app.extract import ExtractedFields, extract_fields
from app.image import clean_product_image
from app.merge import merge_extraction
from app.numbers import extract_duration, extract_money
from app.schema import Listing
from app.tts import speak

# Fields corrections may target. Split matters for the confirm rule:
# a semantic-field correction re-runs full description generation
# (title/description/summary); a numbers-only correction only refreshes
# summary_spoken (cheap, deterministic for en/hi) since title/description
# never mention cost or hours in the first place (see app/describe.py).
SEMANTIC_FIELDS = frozenset({"category", "craft_type", "material", "colours", "dimensions"})
NUMERIC_FIELDS = frozenset({"material_cost_inr", "hours_worked"})
CORRECTABLE_FIELDS = SEMANTIC_FIELDS | NUMERIC_FIELDS


class UnknownCorrectionFieldError(ValueError):
    """Raised when a corrections payload targets a field that isn't correctable."""


def build_draft(
    artisan_id: str,
    audio_path: Path,
    language_hint: str | None,
    image_original_url: str | None,
) -> tuple[Listing, ExtractedFields]:
    """Runs ASR -> number parsing -> LLM extraction -> merge -> description
    generation, and assembles the resulting Listing.

    Returns (listing, extracted_fields) — the caller (app/main.py) is
    expected to persist both via app/store.py, and to synthesize
    listing.summary_spoken into audio via app/tts.py.
    """
    asr_result = transcribe(audio_path, language_hint=language_hint)
    transcript = asr_result["text"]
    language = asr_result["detected_language"]

    llm_result = extract_fields(transcript, language)
    money_matches = extract_money(transcript, language)
    duration_matches = extract_duration(transcript, language)
    merged = merge_extraction(llm_result, money_matches, duration_matches)

    generated = generate_descriptions(merged, language)

    listing = Listing(
        listing_id=f"lst_{uuid.uuid4().hex[:12]}",
        artisan_id=artisan_id,
        source_language=language,
        transcript_raw=transcript,
        title_en=generated.title_en,
        title_hi=generated.title_hi,
        description_en=generated.description_en,
        description_hi=generated.description_hi,
        summary_spoken=generated.summary_spoken,
        category=merged.category,
        craft_type=merged.craft_type,
        material=merged.material,
        colours=merged.colours,
        dimensions=merged.dimensions,
        material_cost_inr=_whole_rupees(merged.material_cost_inr),
        hours_worked=merged.hours_worked,
        confidence=merged.confidence,
        needs_confirmation=merged.needs_confirmation,
        image_original_url=image_original_url,
        image_clean_url=None,
    )
    return listing, merged


def _whole_rupees(value: int | float | None) -> int | None:
    """The listing, B2 and the shop hold costs in whole rupees. A spoken
    "saadhe baarah rupaye" is 12.5; rounding it up keeps the wage floor
    from being understated (and a fraction here used to crash the request)."""
    return None if value is None else math.ceil(value - 1e-9)


def _summary_or(fallback: str | None, fields: ExtractedFields, language: str | None) -> str | None:
    try:
        return generate_spoken_summary(fields, language)
    except Exception:  # noqa: BLE001 — only non-hi/en summaries call the LLM
        return fallback


def apply_corrections(
    listing: Listing,
    extracted_fields: ExtractedFields,
    corrections: dict[str, object],
    confirmed: bool,
) -> tuple[Listing, ExtractedFields]:
    """Apply artisan-submitted corrections and decide what to regenerate.

    Rule: a correction to any semantic field (category/craft_type/
    material/colours/dimensions) re-runs full description generation —
    title_en/hi, description_en/hi, and summary_spoken. A correction
    touching only numeric fields (material_cost_inr/hours_worked)
    re-runs nothing but summary_spoken (which is the only generated text
    that ever mentions cost/hours) — title/description are left as-is.
    No corrections at all (e.g. confirming a draft outright) regenerates
    nothing.

    `confirmed=True` clears any remaining needs_confirmation entries —
    corrected or not, the artisan has signed off on the listing as it now
    stands. `confirmed=False` leaves whatever wasn't corrected still
    flagged, for a later round of corrections.

    Raises UnknownCorrectionFieldError if corrections targets a field
    that isn't one of CORRECTABLE_FIELDS.
    """
    unknown = set(corrections) - CORRECTABLE_FIELDS
    if unknown:
        raise UnknownCorrectionFieldError(
            f"Unsupported correction field(s): {sorted(unknown)}. "
            f"Correctable fields: {sorted(CORRECTABLE_FIELDS)}"
        )

    updated_fields = extracted_fields.model_copy(deep=True)
    for field, value in corrections.items():
        setattr(updated_fields, field, value)
        if field in updated_fields.needs_confirmation:
            updated_fields.needs_confirmation.remove(field)

    semantic_changed = bool(set(corrections) & SEMANTIC_FIELDS)
    numeric_changed = bool(set(corrections) & NUMERIC_FIELDS)

    if semantic_changed:
        try:
            generated = generate_descriptions(updated_fields, listing.source_language)
        except Exception:  # noqa: BLE001 — same degrade-not-crash rule as create_listing
            # The LLM step failed (no key, network, a refusal). Her corrected
            # fields still save; the title and description she already
            # heard stay, and the spoken summary is rebuilt from the
            # corrected fields so it never reads out a stale number.
            generated = GeneratedDescriptions(
                title_en=listing.title_en,
                title_hi=listing.title_hi,
                description_en=listing.description_en,
                description_hi=listing.description_hi,
                summary_spoken=_summary_or(listing.summary_spoken, updated_fields, listing.source_language),
            )
    elif numeric_changed:
        generated = GeneratedDescriptions(
            title_en=listing.title_en,
            title_hi=listing.title_hi,
            description_en=listing.description_en,
            description_hi=listing.description_hi,
            summary_spoken=generate_spoken_summary(updated_fields, listing.source_language),
        )
    else:
        generated = GeneratedDescriptions(
            title_en=listing.title_en,
            title_hi=listing.title_hi,
            description_en=listing.description_en,
            description_hi=listing.description_hi,
            summary_spoken=listing.summary_spoken,
        )

    if confirmed:
        updated_fields.needs_confirmation = []

    updated_listing = listing.model_copy(
        update={
            "title_en": generated.title_en,
            "title_hi": generated.title_hi,
            "description_en": generated.description_en,
            "description_hi": generated.description_hi,
            "summary_spoken": generated.summary_spoken,
            "category": updated_fields.category,
            "craft_type": updated_fields.craft_type,
            "material": updated_fields.material,
            "colours": updated_fields.colours,
            "dimensions": updated_fields.dimensions,
            "material_cost_inr": _whole_rupees(updated_fields.material_cost_inr),
            "hours_worked": updated_fields.hours_worked,
            "confidence": updated_fields.confidence,
            "needs_confirmation": updated_fields.needs_confirmation,
        }
    )

    return updated_listing, updated_fields


# ---------------------------------------------------------------------------
# End-to-end path: POST /listing/create
# ---------------------------------------------------------------------------


async def _run_stage(code: str, fn: Callable, *args, **kwargs) -> tuple[Any, float, str | None, str | None]:
    """Run a (synchronous, potentially blocking/slow) stage function in a
    worker thread — so it doesn't block the event loop, and so two stages
    can genuinely run concurrently via asyncio.gather despite Python's
    GIL: the heavy work in each of these (OpenCV/ONNX, ffmpeg subprocess
    + ctranslate2, HTTP calls) happens in native code or another process,
    which releases the GIL while it runs.

    Times the call and catches any exception, so one stage failing
    degrades gracefully (result=None, a short stable `code`_failed error
    code for the app to key off, and a detailed message for debugging)
    instead of crashing the whole request.

    Returns (result, elapsed_seconds, error_code, error_detail).
    """
    start = time.monotonic()
    try:
        result = await asyncio.to_thread(fn, *args, **kwargs)
        return result, time.monotonic() - start, None, None
    except Exception as exc:  # noqa: BLE001 — deliberately broad: any stage must degrade, not crash
        elapsed = time.monotonic() - start
        return None, elapsed, f"{code}_failed", f"{type(exc).__name__}: {exc}"


def _parse_numbers(transcript: str, language: str) -> tuple[list[dict], list[dict]]:
    return extract_money(transcript, language), extract_duration(transcript, language)


async def create_listing(
    artisan_id: str,
    image_path: Path,
    audio_path: Path,
    image_original_url: str,
    language_hint: str | None = None,
    language_preference: str | None = None,
) -> dict:
    """The end-to-end path behind POST /listing/create.

    Stage order:
      1. Image cleanup (app.image.clean_product_image) and transcription
         (app.asr.transcribe) — run concurrently, neither depends on the
         other.
      2. Numbers parsing (app.numbers) and LLM field extraction
         (app.extract.extract_fields) — also run concurrently; both only
         need the transcript from stage 1, and don't depend on each other.
      3. Merge (app.merge.merge_extraction).
      4. Description generation (app.describe.generate_descriptions).
      5. TTS of the spoken summary (app.tts.speak) — skipped (not an
         error) if there's no summary_spoken to read.

    Every stage is individually timed and individually fault-tolerant:
    if a stage's function raises, that stage's contribution to the
    result is degraded to its safest empty/null value and a `<stage>_failed`
    code is added to `errors`, but every stage that doesn't depend on the
    failed one still runs normally. E.g. if transcription fails, the
    image is still cleaned, and description generation still runs (on an
    empty transcript, which already degrades to all-null fields per
    app.extract.extract_fields's own documented behaviour) rather than
    the whole request failing.

    Returns a dict:
      {"listing": Listing, "extracted_fields": ExtractedFields,
       "clean_path": str | None, "alpha_path": str | None,
       "summary_audio_path": Path | None,
       "errors": list[str], "debug": dict[str, float | str]}
    `listing.image_clean_url` is left null here — the caller (app/main.py)
    fills it in from `clean_path` once it has a Request to build a URL
    from, same pattern build_draft already uses for image_original_url.
    """
    errors: list[str] = []
    debug: dict[str, float | str] = {}
    overall_start = time.monotonic()

    # --- Stage 1: image cleanup + transcription, concurrently ---
    (image_result, debug["image_cleanup_sec"], image_code, image_detail), (
        asr_result,
        debug["transcription_sec"],
        asr_code,
        asr_detail,
    ) = await asyncio.gather(
        _run_stage("image_cleanup", clean_product_image, image_path),
        _run_stage(
            "transcription", transcribe, audio_path,
            language_hint=language_hint, language_preference=language_preference,
        ),
    )

    clean_path: str | None = None
    alpha_path: str | None = None
    if image_code:
        errors.append(image_code)
        debug["image_cleanup_error"] = image_detail
    else:
        clean_path = image_result["clean_path"]
        alpha_path = image_result["alpha_path"]
        if image_result["warnings"]:
            # Reuse clean_product_image's own vocabulary (e.g. "retake")
            # rather than inventing a parallel code for the same thing.
            errors.extend(image_result["warnings"])

    if asr_code:
        errors.append(asr_code)
        debug["transcription_error"] = asr_detail
        transcript = ""
        detected_language = language_hint or language_preference or "en"
    else:
        transcript = asr_result["text"]
        detected_language = asr_result["detected_language"]
        if not transcript:
            # Silence or noise only: say so, rather than an empty listing
            # that looks like the pipeline worked.
            errors.append("transcription_failed")
            debug["transcription_error"] = "no speech detected"

    # --- Stage 2: numbers parsing + LLM extraction, concurrently ---
    (numbers_result, debug["numbers_parsing_sec"], numbers_code, numbers_detail), (
        llm_fields,
        debug["llm_extraction_sec"],
        llm_code,
        llm_detail,
    ) = await asyncio.gather(
        _run_stage("numbers_parsing", _parse_numbers, transcript, detected_language),
        _run_stage("extraction", extract_fields, transcript, detected_language),
    )

    if numbers_code:
        errors.append(numbers_code)
        debug["numbers_parsing_error"] = numbers_detail
        money_matches, duration_matches = [], []
    else:
        money_matches, duration_matches = numbers_result

    if llm_code:
        errors.append(llm_code)
        debug["llm_extraction_error"] = llm_detail
        llm_fields = ExtractedFields()

    # --- Stage 3: merge ---
    merged, debug["merge_sec"], merge_code, merge_detail = await _run_stage(
        "merge", merge_extraction, llm_fields, money_matches, duration_matches
    )
    if merge_code:
        errors.append(merge_code)
        debug["merge_error"] = merge_detail
        merged = llm_fields  # best-effort: keep whatever the LLM stage produced

    # --- Stage 4: description generation ---
    generated, debug["description_generation_sec"], desc_code, desc_detail = await _run_stage(
        "description_generation", generate_descriptions, merged, detected_language
    )
    if desc_code:
        errors.append(desc_code)
        debug["description_generation_error"] = desc_detail
        # The readback does not need the marketing copy: it is what she
        # hears to confirm her own cost and hours, built from a template
        # for Hindi and English. Losing it with the title sent her an
        # English "recording saved" line instead of her Hindi readback.
        generated = GeneratedDescriptions(
            summary_spoken=_summary_or(None, merged, detected_language)
            if _has_anything_to_report(merged)
            else None
        )

    # --- Stage 5: TTS of the spoken summary ---
    summary_audio_path: Path | None = None
    if generated.summary_spoken:
        summary_audio_path, debug["tts_sec"], tts_code, tts_detail = await _run_stage(
            "tts", speak, generated.summary_spoken, detected_language
        )
        if tts_code:
            errors.append(tts_code)
            debug["tts_error"] = tts_detail
    else:
        debug["tts_sec"] = 0.0

    debug["total_sec"] = time.monotonic() - overall_start

    listing = Listing(
        listing_id=f"lst_{uuid.uuid4().hex[:12]}",
        artisan_id=artisan_id,
        source_language=detected_language,
        transcript_raw=transcript or None,
        title_en=generated.title_en,
        title_hi=generated.title_hi,
        description_en=generated.description_en,
        description_hi=generated.description_hi,
        summary_spoken=generated.summary_spoken,
        category=merged.category,
        craft_type=merged.craft_type,
        material=merged.material,
        colours=merged.colours,
        dimensions=merged.dimensions,
        material_cost_inr=_whole_rupees(merged.material_cost_inr),
        hours_worked=merged.hours_worked,
        confidence=merged.confidence,
        needs_confirmation=merged.needs_confirmation,
        image_original_url=image_original_url,
        image_clean_url=None,
    )

    return {
        "listing": listing,
        "extracted_fields": merged,
        "clean_path": clean_path,
        "alpha_path": alpha_path,
        "summary_audio_path": summary_audio_path,
        "errors": errors,
        "debug": debug,
    }
