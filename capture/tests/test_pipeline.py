"""Tests for app/pipeline.py.

Mocks the network/model-heavy boundaries (app.pipeline.transcribe,
app.pipeline.extract_fields, and app.describe._call_groq_marketing) so
these are fast, deterministic unit tests of the orchestration and
correction logic itself — not of ASR or the LLM. The end-to-end HTTP
integration test lives in tests/test_listing_flow.py.
"""

import json

import pytest

import app.describe as describe_module
import app.pipeline as pipeline_module
from app.extract import ExtractedFields
from app.pipeline import UnknownCorrectionFieldError, apply_corrections, build_draft

AMBIGUOUS_TRANSCRIPT = (
    "cost paanch sau rupaye tha, nahi nahi, teen sau rupaye tha shayad, teen ghante lage"
)
SIMPLE_TRANSCRIPT = "cost paanch sau rupaye tha, teen ghante lage"


def _fake_transcribe(text: str):
    def _inner(audio_path, language_hint=None, **kwargs):
        return {"text": text, "detected_language": "hi", "confidence": 0.95, "duration_sec": 4.0}

    return _inner


def _fake_extract_fields(**overrides):
    def _inner(text, language):
        defaults = dict(category="home decor", craft_type="pottery", material="terracotta")
        defaults.update(overrides)
        return ExtractedFields(**defaults)

    return _inner


def _fake_marketing(calls: list | None = None):
    def _inner(lang, facts):
        if calls is not None:
            calls.append(lang)
        title = "Title" if lang == "en" else "शीर्षक"
        description = " ".join(["word"] * 70)
        return "sys", json.dumps(facts, ensure_ascii=False), json.dumps(
            {"title": title, "description": description}
        )

    return _inner


@pytest.fixture(autouse=True)
def _mock_marketing(monkeypatch):
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fake_marketing())


# ---------------------------------------------------------------------------
# build_draft
# ---------------------------------------------------------------------------


def test_build_draft_flags_ambiguous_cost(monkeypatch):
    monkeypatch.setattr(pipeline_module, "transcribe", _fake_transcribe(AMBIGUOUS_TRANSCRIPT))
    monkeypatch.setattr(pipeline_module, "extract_fields", _fake_extract_fields())

    listing, fields = build_draft("art_1", "/tmp/fake.wav", None, "http://x/img.jpg")

    assert listing.material_cost_inr is None
    assert "material_cost_inr" in listing.needs_confirmation
    assert listing.hours_worked == 3.0
    assert "hours_worked" not in listing.needs_confirmation
    assert fields.needs_confirmation == listing.needs_confirmation


def test_build_draft_asks_question_for_flagged_field_in_summary(monkeypatch):
    monkeypatch.setattr(pipeline_module, "transcribe", _fake_transcribe(AMBIGUOUS_TRANSCRIPT))
    monkeypatch.setattr(pipeline_module, "extract_fields", _fake_extract_fields())

    listing, _ = build_draft("art_1", "/tmp/fake.wav", None, None)

    assert "?" in listing.summary_spoken


def test_build_draft_populates_identity_and_image_fields(monkeypatch):
    monkeypatch.setattr(pipeline_module, "transcribe", _fake_transcribe(SIMPLE_TRANSCRIPT))
    monkeypatch.setattr(pipeline_module, "extract_fields", _fake_extract_fields())

    listing, _ = build_draft("art_42", "/tmp/fake.wav", None, "http://x/img.jpg")

    assert listing.artisan_id == "art_42"
    assert listing.listing_id.startswith("lst_")
    assert listing.image_original_url == "http://x/img.jpg"
    assert listing.image_clean_url is None
    assert listing.source_language == "hi"
    assert listing.transcript_raw == SIMPLE_TRANSCRIPT


def test_build_draft_unambiguous_transcript_has_no_confirmation_needed(monkeypatch):
    monkeypatch.setattr(pipeline_module, "transcribe", _fake_transcribe(SIMPLE_TRANSCRIPT))
    monkeypatch.setattr(pipeline_module, "extract_fields", _fake_extract_fields())

    listing, _ = build_draft("art_1", "/tmp/fake.wav", None, None)

    assert listing.needs_confirmation == []
    assert listing.material_cost_inr == 500
    assert listing.hours_worked == 3.0
    assert "?" not in listing.summary_spoken


# ---------------------------------------------------------------------------
# apply_corrections — numeric-only vs semantic vs no-op
# ---------------------------------------------------------------------------


def _draft(monkeypatch, transcript=AMBIGUOUS_TRANSCRIPT, **field_overrides):
    monkeypatch.setattr(pipeline_module, "transcribe", _fake_transcribe(transcript))
    monkeypatch.setattr(pipeline_module, "extract_fields", _fake_extract_fields(**field_overrides))
    return build_draft("art_1", "/tmp/fake.wav", None, None)


def test_numeric_only_correction_does_not_regenerate_marketing_copy(monkeypatch):
    calls: list = []
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fake_marketing(calls))
    listing, fields = _draft(monkeypatch)
    calls.clear()  # ignore the draft's own 2 calls

    updated_listing, updated_fields = apply_corrections(
        listing, fields, {"material_cost_inr": 500}, confirmed=True
    )

    assert calls == []  # no marketing regeneration
    assert updated_listing.material_cost_inr == 500
    assert updated_listing.title_en == listing.title_en
    assert updated_listing.description_en == listing.description_en


def test_numeric_only_correction_still_refreshes_summary_spoken(monkeypatch):
    listing, fields = _draft(monkeypatch)
    assert "?" in listing.summary_spoken  # pending question before correction

    updated_listing, _ = apply_corrections(
        listing, fields, {"material_cost_inr": 500}, confirmed=True
    )

    assert "?" not in updated_listing.summary_spoken
    assert "500" in updated_listing.summary_spoken


def test_semantic_correction_survives_the_llm_failing(monkeypatch):
    """No Groq key, network down: her corrections still save, confirm does not crash."""
    listing, fields = _draft(monkeypatch)

    def down(*args, **kwargs):
        raise RuntimeError("GROQ_API_KEY is not set.")

    monkeypatch.setattr(pipeline_module, "generate_descriptions", down)
    updated_listing, _ = apply_corrections(
        listing, fields, {"material": "clay", "material_cost_inr": 500}, confirmed=True
    )

    assert updated_listing.material == "clay"
    assert updated_listing.material_cost_inr == 500
    assert updated_listing.title_en == listing.title_en  # kept, not blanked
    assert "500" in updated_listing.summary_spoken  # never a stale number read aloud


def test_semantic_correction_regenerates_marketing_copy(monkeypatch):
    calls: list = []
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fake_marketing(calls))
    listing, fields = _draft(monkeypatch)
    calls.clear()

    updated_listing, updated_fields = apply_corrections(
        listing, fields, {"material": "clay"}, confirmed=False
    )

    assert calls == ["en", "hi"]
    assert updated_fields.material == "clay"


def test_correction_with_no_fields_regenerates_nothing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fake_marketing(calls))
    listing, fields = _draft(monkeypatch, transcript=SIMPLE_TRANSCRIPT)
    calls.clear()

    updated_listing, _ = apply_corrections(listing, fields, {}, confirmed=True)

    assert calls == []
    assert updated_listing.summary_spoken == listing.summary_spoken
    assert updated_listing.title_en == listing.title_en


def test_unknown_correction_field_raises(monkeypatch):
    listing, fields = _draft(monkeypatch)

    with pytest.raises(UnknownCorrectionFieldError):
        apply_corrections(listing, fields, {"title_en": "hacked"}, confirmed=False)


def test_confirmed_true_clears_all_remaining_needs_confirmation(monkeypatch):
    listing, fields = _draft(monkeypatch)
    assert listing.needs_confirmation == ["material_cost_inr"]

    updated_listing, updated_fields = apply_corrections(listing, fields, {}, confirmed=True)

    assert updated_listing.needs_confirmation == []
    assert updated_fields.needs_confirmation == []


def test_confirmed_false_leaves_uncorrected_flags_in_place(monkeypatch):
    listing, fields = _draft(monkeypatch)

    updated_listing, _ = apply_corrections(listing, fields, {}, confirmed=False)

    assert updated_listing.needs_confirmation == ["material_cost_inr"]


def test_correcting_flagged_field_removes_it_from_needs_confirmation_even_unconfirmed(monkeypatch):
    listing, fields = _draft(monkeypatch)

    updated_listing, _ = apply_corrections(
        listing, fields, {"material_cost_inr": 500}, confirmed=False
    )

    assert updated_listing.needs_confirmation == []


def test_apply_corrections_does_not_mutate_the_original_listing(monkeypatch):
    listing, fields = _draft(monkeypatch)
    original_cost = listing.material_cost_inr

    apply_corrections(listing, fields, {"material_cost_inr": 500}, confirmed=True)

    assert listing.material_cost_inr == original_cost


def test_a_fractional_cost_becomes_whole_rupees_rounded_up(monkeypatch):
    """ "saadhe baarah rupaye" is 12.5; the listing holds whole rupees, and
    rounding up never understates her floor. It used to crash the request."""
    listing, fields = _draft(monkeypatch, transcript="saadhe baarah rupaye laga, teen ghante")
    assert listing.material_cost_inr == 13
    corrected, _ = apply_corrections(listing, fields, {"material_cost_inr": 20.2}, confirmed=True)
    assert corrected.material_cost_inr == 21
