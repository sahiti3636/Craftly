"""Semantic product-field extraction from a transcript, via Groq.

Extracts ONLY category, craft_type, material, colours, dimensions, and
product_description_facts. Money and hours are deliberately out of scope
here — app/numbers.py handles those deterministically, and
app/merge.py combines the two into one ExtractedFields object.

The model is instructed (prompts/extract.txt) never to invent a fact: a
field the artisan didn't state must come back null, not guessed from
other fields. That instruction is backed by a validation-level guardrail
here too — the LLM's JSON is parsed into a schema that forbids extra keys
(so a hallucinated "material_cost_inr" fails validation) and constrains
confidence to [0, 1] — rather than trusting the prompt alone.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.groq_client import DEFAULT_MODEL, get_client

_GROQ_MODEL = DEFAULT_MODEL
_LOG_PATH = Path(os.environ.get("CRAFTLY_EXTRACT_LOG_PATH", "logs/extract.jsonl"))
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extract.txt"
_MAX_ATTEMPTS = 2


class ExtractionError(RuntimeError):
    """Raised when Groq's output couldn't be parsed/validated after one retry."""


class ExtractedFields(BaseModel):
    """The fields this pipeline knows about a listing, from any source.

    extract_fields() only ever populates category through
    product_description_facts. material_cost_inr and hours_worked start
    out null here and are filled in by app/merge.py from app/numbers.py's
    output — this model is the shared shape both steps write into.

    validate_assignment=True so that app/pipeline.py can apply artisan
    corrections via plain attribute assignment (updated.category = ...)
    and get a real validation error for a bad value, instead of silently
    storing something the wrong type.
    """

    model_config = ConfigDict(validate_assignment=True)

    category: str | None = None
    craft_type: str | None = None
    material: str | None = None
    colours: list[str] | None = None
    dimensions: str | None = None
    product_description_facts: list[str] = Field(default_factory=list)

    material_cost_inr: int | float | None = None
    hours_worked: float | None = None

    confidence: dict[str, float] = Field(default_factory=dict)
    needs_confirmation: list[str] = Field(default_factory=list)


class _LLMExtraction(BaseModel):
    """Strict shape for what the LLM is allowed to return.

    extra="forbid" means a hallucinated key (e.g. the model deciding to
    also report "material_cost_inr" despite being told not to) fails
    validation instead of silently passing through.
    """

    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    craft_type: str | None = None
    material: str | None = None
    colours: list[str] | None = None
    dimensions: str | None = None
    product_description_facts: list[str] = Field(default_factory=list)
    confidence: dict[str, float] = Field(default_factory=dict)

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, value: dict[str, float]) -> dict[str, float]:
        for field_name, score in value.items():
            if not (0.0 <= score <= 1.0):
                raise ValueError(
                    f"confidence for {field_name!r} must be between 0 and 1, got {score}"
                )
        return value


def _build_system_prompt(detected_language: str | None) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    language_label = detected_language or "an unspecified language"
    return template.replace("<LANGUAGE>", language_label)


def _call_groq(transcript: str, detected_language: str | None) -> tuple[str, str]:
    """Returns (system_prompt, raw_response_content)."""
    client = get_client()
    system_prompt = _build_system_prompt(detected_language)
    response = client.chat.completions.create(
        model=_GROQ_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript},
        ],
    )
    return system_prompt, response.choices[0].message.content or ""


def _parse_llm_response(raw: str) -> ExtractedFields | None:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        llm = _LLMExtraction.model_validate(data)
    except ValidationError:
        return None
    return ExtractedFields(
        category=llm.category,
        craft_type=llm.craft_type,
        material=llm.material,
        colours=llm.colours,
        dimensions=llm.dimensions,
        product_description_facts=llm.product_description_facts,
        confidence=llm.confidence,
    )


def _log_interaction(
    *,
    attempt: int,
    transcript: str,
    detected_language: str | None,
    system_prompt: str | None,
    raw_response: str | None,
    error: str | None,
) -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": _GROQ_MODEL,
        "attempt": attempt,
        "detected_language": detected_language,
        "transcript": transcript,
        "system_prompt": system_prompt,
        "raw_response": raw_response,
        "error": error,
    }
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def extract_fields(transcript: str, detected_language: str | None) -> ExtractedFields:
    """Extract category/craft_type/material/colours/dimensions/
    product_description_facts from `transcript` via Groq.

    Does not touch material_cost_inr or hours_worked — those are left at
    their defaults (null) for app/merge.py to fill in from
    app/numbers.py's output.

    Retries once if the response isn't valid JSON matching the expected
    schema (including confidence out of [0, 1] or an unexpected extra
    key). Raises ExtractionError if it's still invalid after the retry,
    or if the Groq request itself fails.

    Every attempt (prompt and raw response) is logged to a local JSONL
    file for debugging (see CRAFTLY_EXTRACT_LOG_PATH, default
    logs/extract.jsonl).
    """
    if not transcript or not transcript.strip():
        return ExtractedFields()

    last_raw: str | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            system_prompt, raw = _call_groq(transcript, detected_language)
        except Exception as exc:
            _log_interaction(
                attempt=attempt,
                transcript=transcript,
                detected_language=detected_language,
                system_prompt=None,
                raw_response=None,
                error=f"request failed: {exc}",
            )
            raise ExtractionError(f"Groq request failed: {exc}") from exc

        last_raw = raw
        parsed = _parse_llm_response(raw)
        _log_interaction(
            attempt=attempt,
            transcript=transcript,
            detected_language=detected_language,
            system_prompt=system_prompt,
            raw_response=raw,
            error=None if parsed is not None else "malformed or schema-invalid JSON",
        )
        if parsed is not None:
            return parsed

    raise ExtractionError(
        f"Groq returned malformed/invalid JSON after {_MAX_ATTEMPTS} attempts. "
        f"Last raw response: {last_raw!r}"
    )
