"""Generate customer-facing titles/descriptions and a spoken summary for
a listing — from STRUCTURED FIELDS ONLY, never the raw transcript.

Grounding generation in the already-extracted fields (see app/extract.py,
app/merge.py), rather than the transcript, means this step can't
reintroduce a detail the artisan never said, or that earlier extraction
already discarded as uncertain — it only has what survived those steps.

Two kinds of guardrails back the "don't invent facts" rule beyond the
prompt text (prompts/describe_en.txt, describe_hi.txt,
describe_summary.txt):
  - a closed colour vocabulary check: if the generated text mentions a
    colour word not in extracted_fields.colours, generation is retried
    (then fails cleanly) — this is the single most concrete, checkable
    hallucination pattern for this kind of copy.
  - a cost/hours leak check: material_cost_inr and hours_worked must
    never appear in the customer-facing title/description (that
    information is for the artisan only).

summary_spoken is handled differently from title/description: its
content is narrowly scoped to product + material cost + hours (per the
spec), and for English/Hindi it's built with a plain deterministic
template rather than an LLM call — a template grounded directly in
extracted_fields cannot hallucinate. Only source languages without a
template (i.e. not "en"/"hi") fall back to an LLM call.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.extract import ExtractedFields
from app.groq_client import DEFAULT_MODEL, get_client

_GROQ_MODEL = DEFAULT_MODEL
_LOG_PATH = Path("logs/describe.jsonl")
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_MAX_ATTEMPTS = 2

_TITLE_MAX_CHARS = 70
_DESCRIPTION_MIN_WORDS = 60
_DESCRIPTION_MAX_WORDS = 90
_SUMMARY_MIN_WORDS = 6
_SUMMARY_MAX_WORDS = 25

# A closed, enumerable vocabulary — the one fact category concrete enough
# to mechanically check for invention (material/craft_type are open
# vocabulary and can't be checked this way). Not exhaustive; extend as
# needed.
# Romanized and Devanagari: extraction may record the colour as she said it
# ("सफेद"), and the English copy then rightly says "white".
_COLOUR_WORDS: dict[str, list[str]] = {
    "red": ["red", "laal", "lal", "लाल"],
    "gold": ["gold", "golden", "sunehra", "sunehri", "सुनहरा", "सुनहरी", "सुनहरे"],
    "green": ["green", "hara", "hari", "हरा", "हरी", "हरे"],
    "blue": ["blue", "neela", "neeli", "nila", "नीला", "नीली", "नीले"],
    "yellow": ["yellow", "peela", "peeli", "पीला", "पीली", "पीले"],
    "black": ["black", "kala", "kaala", "kali", "काला", "काले"],
    "white": ["white", "safed", "safaid", "सफेद", "सफ़ेद", "श्वेत"],
    "orange": ["orange", "naranji", "santari", "नारंगी", "केसरिया"],
    "pink": ["pink", "gulabi", "गुलाबी"],
    "purple": ["purple", "baingani", "jamuni", "बैंगनी", "जामुनी"],
    "brown": ["brown", "bhura", "bhoora", "भूरा", "भूरी", "भूरे"],
    "silver": ["silver", "chandi", "चांदी", "चाँदी"],
    "grey": ["grey", "gray", "स्लेटी", "सलेटी"],
    "maroon": ["maroon", "मैरून"],
    "beige": ["beige"],
    "turquoise": ["turquoise", "firozi", "फ़िरोज़ी", "फिरोजी"],
    "cream": ["cream", "क्रीम"],
    "multicolour": ["multicolour", "multicolor", "rangbirangi", "rangberangi", "रंगबिरंगी", "रंग-बिरंगी", "रंगबिरंगा"],
}


class DescriptionError(RuntimeError):
    """Raised when Groq's output couldn't be produced validly after one retry."""


class GeneratedDescriptions(BaseModel):
    title_en: str | None = None
    title_hi: str | None = None
    description_en: str | None = None
    description_hi: str | None = None
    summary_spoken: str | None = None


class _MarketingCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str


class _SpokenSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _has_descriptive_facts(fields: ExtractedFields) -> bool:
    """Whether there's enough to write a title/description about."""
    return bool(
        fields.product_type
        or fields.category
        or fields.craft_type
        or fields.material
        or fields.colours
        or fields.dimensions
        or fields.product_description_facts
    )


def _has_anything_to_report(fields: ExtractedFields) -> bool:
    """Whether there's anything at all to say in summary_spoken — including
    a pending question for a field in needs_confirmation, even though that
    field's value is null.
    """
    return bool(
        _has_descriptive_facts(fields)
        or fields.material_cost_inr is not None
        or fields.hours_worked is not None
        or fields.needs_confirmation
    )


def _word_count(text: str) -> int:
    return len(text.split())


def _format_number(value: int | float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _mentioned_colours(text: str) -> set[str]:
    # Whole words only: as substrings "red" was found in "textured" and
    # "inspired", "gold" in "marigold" and "kala" in "Kalamkari", so good
    # copy was rejected for colours it never named.
    text_lower = text.lower()
    return {
        canonical
        for canonical, variants in _COLOUR_WORDS.items()
        if any(re.search(rf"(?<![a-z0-9ऀ-ॿ]){re.escape(variant)}(?![a-z0-9ऀ-ॿ])", text_lower) for variant in variants)
    }


def _allowed_colours(fields: ExtractedFields) -> set[str]:
    allowed = set()
    for colour in fields.colours or []:
        colour_lower = colour.lower()
        for canonical, variants in _COLOUR_WORDS.items():
            if colour_lower == canonical or colour_lower in variants:
                allowed.add(canonical)
    return allowed


def _invented_colours(text: str, fields: ExtractedFields) -> set[str]:
    return _mentioned_colours(text) - _allowed_colours(fields)


# A number is a leak only when it is said as money or as time: a bare "4"
# in "set of 4" or "4 inches" is not her 4 hours of work.
_MONEY = r"(?:₹|rs\.?|inr|rupees?|rupaye|rupye|रुपये|रुपए|रुपया)"
_TIME = r"(?:hours?|hrs?|ghante|ghanta|घंटे|घंटा|घंटों)"


def _number_pattern(value: int | float) -> str:
    """The number as text may write it: 1500, 1,500 or 1,50,000."""
    digits = _format_number(value)
    return re.escape(digits) if "." in digits else ",?".join(digits)


def _leaks_cost_or_hours(text: str, fields: ExtractedFields) -> bool:
    if fields.material_cost_inr is not None:
        cost = _number_pattern(fields.material_cost_inr)
        if re.search(rf"{_MONEY}\s*{cost}\b|\b{cost}\s*(?:/-\s*)?{_MONEY}", text, re.IGNORECASE):
            return True
    if fields.hours_worked is not None:
        hours = _number_pattern(fields.hours_worked)
        if re.search(rf"\b{hours}\s*-?\s*{_TIME}", text, re.IGNORECASE):
            return True
    return False


def _log_interaction(
    *,
    task: str,
    attempt: int,
    system_prompt: str | None,
    user_content: str | None,
    raw_response: str | None,
    error: str | None,
) -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": _GROQ_MODEL,
        "task": task,
        "attempt": attempt,
        "system_prompt": system_prompt,
        "user_content": user_content,
        "raw_response": raw_response,
        "error": error,
    }
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Marketing copy (title + description), English and Hindi
# ---------------------------------------------------------------------------


def _facts_for_marketing(fields: ExtractedFields) -> dict:
    # Deliberately excludes material_cost_inr and hours_worked: that
    # information is for the artisan only, never customer-facing copy.
    return {
        "product_type": fields.product_type,
        "category": fields.category,
        "craft_type": fields.craft_type,
        "material": fields.material,
        "colours": fields.colours,
        "dimensions": fields.dimensions,
        "product_description_facts": fields.product_description_facts,
    }


def _call_groq_marketing(lang: str, facts: dict) -> tuple[str, str, str]:
    """Returns (system_prompt, user_content, raw_response_content)."""
    client = get_client()
    system_prompt = (_PROMPTS_DIR / f"describe_{lang}.txt").read_text(encoding="utf-8")
    user_content = json.dumps(facts, ensure_ascii=False)
    response = client.chat.completions.create(
        model=_GROQ_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return system_prompt, user_content, response.choices[0].message.content or ""


def _validate_marketing_copy(copy: _MarketingCopy, fields: ExtractedFields) -> str | None:
    """Returns an error message if invalid, else None."""
    if not copy.title.strip():
        return "title is empty"
    if len(copy.title) >= _TITLE_MAX_CHARS:
        return f"title is {len(copy.title)} chars, must be under {_TITLE_MAX_CHARS}"

    word_count = _word_count(copy.description)
    if not (_DESCRIPTION_MIN_WORDS <= word_count <= _DESCRIPTION_MAX_WORDS):
        return (
            f"description is {word_count} words, must be "
            f"{_DESCRIPTION_MIN_WORDS}-{_DESCRIPTION_MAX_WORDS}"
        )

    combined = f"{copy.title} {copy.description}"
    invented = _invented_colours(combined, fields)
    if invented:
        return f"mentions colour(s) not in extracted fields: {sorted(invented)}"
    if _leaks_cost_or_hours(combined, fields):
        return "mentions material_cost_inr or hours_worked, which must not appear in customer-facing copy"

    return None


def _parse_marketing_response(raw: str, fields: ExtractedFields) -> tuple[_MarketingCopy | None, str | None]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, "response was not valid JSON"
    try:
        copy = _MarketingCopy.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError
        return None, f"response did not match schema: {exc}"
    error = _validate_marketing_copy(copy, fields)
    if error is not None:
        return None, error
    return copy, None


def _generate_marketing_copy(fields: ExtractedFields, lang: str) -> tuple[str, str]:
    facts = _facts_for_marketing(fields)
    task = f"title_description_{lang}"
    last_error = "no attempts made"

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            system_prompt, user_content, raw = _call_groq_marketing(lang, facts)
        except Exception as exc:
            _log_interaction(
                task=task, attempt=attempt, system_prompt=None, user_content=None,
                raw_response=None, error=f"request failed: {exc}",
            )
            raise DescriptionError(f"Groq request failed ({task}): {exc}") from exc

        copy, error = _parse_marketing_response(raw, fields)
        _log_interaction(
            task=task, attempt=attempt, system_prompt=system_prompt,
            user_content=user_content, raw_response=raw, error=error,
        )
        if copy is not None:
            return copy.title, copy.description
        last_error = error or "unknown validation failure"

    raise DescriptionError(
        f"Groq output for {task} was invalid after {_MAX_ATTEMPTS} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Spoken summary — deterministic template for en/hi, LLM for everything else
# ---------------------------------------------------------------------------


def _product_label(fields: ExtractedFields) -> str | None:
    # What she made, as she named it ("vase") — not the technique: "This is
    # your hand painting" for a painted vase was the old readback.
    return fields.product_type or fields.category or fields.craft_type or fields.material


def _build_summary_en(fields: ExtractedFields) -> str:
    # A field in needs_confirmation is always null here (that's how
    # app/merge.py flags it), so it must be asked about as a question,
    # never stated as if its value were known.
    product = _product_label(fields) or "product"
    sentences = [f"This is your {product}."]

    if "material_cost_inr" in fields.needs_confirmation:
        sentences.append("What was the material cost?")
    elif fields.material_cost_inr is not None:
        sentences.append(f"Material cost is {_format_number(fields.material_cost_inr)} rupees.")

    if "hours_worked" in fields.needs_confirmation:
        sentences.append("How many hours did it take to make?")
    elif fields.hours_worked is not None:
        sentences.append(f"It took {_format_number(fields.hours_worked)} hours to make.")

    return " ".join(sentences)


def _build_summary_hi(fields: ExtractedFields) -> str:
    # Devanagari, not romanized Hinglish: this is read aloud by a Hindi
    # voice, which pronounces Latin letters as English — "yeh aapka product
    # hai" came out sounding like English. Product names stay as extracted
    # (often English craft words, which is how they are spoken anyway).
    product = _product_label(fields) or "प्रोडक्ट"
    sentences = [f"यह आपका {product} है।"]

    if "material_cost_inr" in fields.needs_confirmation:
        sentences.append("सामान की लागत कितनी थी?")
    elif fields.material_cost_inr is not None:
        sentences.append(f"सामान की लागत {_format_number(fields.material_cost_inr)} रुपये है।")

    if "hours_worked" in fields.needs_confirmation:
        sentences.append("इसे बनाने में कितने घंटे लगे?")
    elif fields.hours_worked is not None:
        sentences.append(f"इसे बनाने में {_format_number(fields.hours_worked)} घंटे लगे।")

    return " ".join(sentences)


def _facts_for_summary(fields: ExtractedFields) -> dict:
    return {
        "product": _product_label(fields),
        "material_cost_inr": fields.material_cost_inr,
        "hours_worked": fields.hours_worked,
        "ask_about_material_cost": "material_cost_inr" in fields.needs_confirmation,
        "ask_about_hours_worked": "hours_worked" in fields.needs_confirmation,
    }


def _call_groq_summary(facts: dict, source_language: str) -> tuple[str, str, str]:
    client = get_client()
    template = (_PROMPTS_DIR / "describe_summary.txt").read_text(encoding="utf-8")
    system_prompt = template.replace("<LANGUAGE>", source_language)
    user_content = json.dumps(facts, ensure_ascii=False)
    response = client.chat.completions.create(
        model=_GROQ_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return system_prompt, user_content, response.choices[0].message.content or ""


def _validate_summary(summary: _SpokenSummary) -> str | None:
    if not summary.summary.strip():
        return "summary is empty"
    word_count = _word_count(summary.summary)
    if not (_SUMMARY_MIN_WORDS <= word_count <= _SUMMARY_MAX_WORDS):
        return f"summary is {word_count} words, expected roughly {_SUMMARY_MIN_WORDS}-{_SUMMARY_MAX_WORDS}"
    return None


def _parse_summary_response(raw: str) -> tuple[_SpokenSummary | None, str | None]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, "response was not valid JSON"
    try:
        summary = _SpokenSummary.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError
        return None, f"response did not match schema: {exc}"
    error = _validate_summary(summary)
    if error is not None:
        return None, error
    return summary, None


def _generate_summary_llm(fields: ExtractedFields, source_language: str) -> str:
    facts = _facts_for_summary(fields)
    task = f"summary_{source_language}"
    last_error = "no attempts made"

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            system_prompt, user_content, raw = _call_groq_summary(facts, source_language)
        except Exception as exc:
            _log_interaction(
                task=task, attempt=attempt, system_prompt=None, user_content=None,
                raw_response=None, error=f"request failed: {exc}",
            )
            raise DescriptionError(f"Groq request failed ({task}): {exc}") from exc

        summary, error = _parse_summary_response(raw)
        _log_interaction(
            task=task, attempt=attempt, system_prompt=system_prompt,
            user_content=user_content, raw_response=raw, error=error,
        )
        if summary is not None:
            return summary.summary
        last_error = error or "unknown validation failure"

    raise DescriptionError(
        f"Groq output for {task} was invalid after {_MAX_ATTEMPTS} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_spoken_summary(extracted_fields: ExtractedFields, source_language: str) -> str:
    """Build summary_spoken on its own, without touching title/description.

    Exposed separately from generate_descriptions() so callers that only
    need to refresh the spoken summary (e.g. the confirm flow after a
    numbers-only correction — see app/pipeline.py) don't pay for the two
    marketing-copy LLM calls just to get this.

    Any field in `extracted_fields.needs_confirmation` is spoken back as a
    question, never stated as fact (it's always null when flagged, so
    stating it as fact would mean stating a value we don't actually have).
    """
    if source_language == "en":
        return _build_summary_en(extracted_fields)
    if source_language == "hi":
        return _build_summary_hi(extracted_fields)
    return _generate_summary_llm(extracted_fields, source_language)


def generate_descriptions(
    extracted_fields: ExtractedFields, source_language: str
) -> GeneratedDescriptions:
    """Generate title_en, title_hi, description_en, description_hi, and
    summary_spoken from `extracted_fields` ONLY — the raw transcript is
    never consulted here, so this step cannot reintroduce a detail that
    didn't survive extraction.

    title_en/title_hi are always under 70 characters; description_en/
    description_hi are always 60-90 words, naturally mention material and
    craft_type when known, and never mention cost or hours (that's for
    the artisan, not the customer). summary_spoken is a short (~15 word)
    plain-text readback in `source_language` covering only product,
    material cost, and hours (see generate_spoken_summary).

    title/description are only generated when there's descriptive content
    to write about (category/craft_type/material/colours/dimensions/
    product_description_facts). summary_spoken is generated whenever
    there's anything to report at all — including a pending question for
    a field in needs_confirmation, even with no other facts known.

    If there's nothing to report at all, returns an all-null result
    without calling Groq.

    Raises DescriptionError if Groq's output is still invalid (wrong
    length, invented colour, or a cost/hours leak) after one retry, or if
    a Groq request itself fails.
    """
    if not _has_anything_to_report(extracted_fields):
        return GeneratedDescriptions()

    title_en = title_hi = description_en = description_hi = None
    if _has_descriptive_facts(extracted_fields):
        title_en, description_en = _generate_marketing_copy(extracted_fields, "en")
        title_hi, description_hi = _generate_marketing_copy(extracted_fields, "hi")

    summary_spoken = generate_spoken_summary(extracted_fields, source_language)

    return GeneratedDescriptions(
        title_en=title_en,
        title_hi=title_hi,
        description_en=description_en,
        description_hi=description_hi,
        summary_spoken=summary_spoken,
    )
