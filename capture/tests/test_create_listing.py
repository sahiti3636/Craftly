"""Tests for app.pipeline.create_listing — the orchestration behind
POST /listing/create.

All external stage functions (image cleanup, ASR, Groq marketing calls,
TTS) are mocked here so these are fast, deterministic tests of the
orchestration itself: stage ordering, genuine concurrency in the two
parallel stages, and the degrade-not-crash contract for each stage.
The real end-to-end HTTP behavior is covered by test_listing_flow.py's
sibling test file for /listing/create (tests/test_main.py).
"""

import json
import time
from pathlib import Path

import pytest

import app.describe as describe_module
import app.pipeline as pipeline_module
from app.extract import ExtractedFields
from app.pipeline import create_listing


def _fake_clean(clean_path="/tmp/fake_clean.jpg", alpha_path="/tmp/fake_alpha.png", warnings=None):
    def _inner(image_path):
        return {"clean_path": clean_path, "alpha_path": alpha_path, "warnings": warnings or []}

    return _inner


def _fake_transcribe(text="paanch sau rupaye, teen ghante", language="hi"):
    def _inner(audio_path, language_hint=None, **kwargs):
        return {"text": text, "detected_language": language, "confidence": 0.9, "duration_sec": 2.0}

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
        return "sys", json.dumps(facts, ensure_ascii=False), json.dumps(
            {"title": title, "description": " ".join(["word"] * 70)}
        )

    return _inner


def _fake_speak(path: Path = Path("/tmp/fake_summary.mp3")):
    def _inner(text, language):
        path.write_bytes(b"fake-audio")
        return path

    return _inner


@pytest.fixture(autouse=True)
def _mock_all_stages(monkeypatch):
    monkeypatch.setattr(pipeline_module, "clean_product_image", _fake_clean())
    monkeypatch.setattr(pipeline_module, "transcribe", _fake_transcribe())
    monkeypatch.setattr(pipeline_module, "extract_fields", _fake_extract_fields())
    monkeypatch.setattr(pipeline_module, "speak", _fake_speak())
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fake_marketing())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_produces_a_complete_listing():
    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    assert result["errors"] == []
    listing = result["listing"]
    assert listing.artisan_id == "art_1"
    assert listing.listing_id.startswith("lst_")
    assert listing.transcript_raw == "paanch sau rupaye, teen ghante"
    assert listing.source_language == "hi"
    assert listing.material_cost_inr == 500
    assert listing.hours_worked == 3.0
    assert listing.needs_confirmation == []
    assert listing.category == "home decor"
    assert listing.title_en == "Title"
    assert listing.image_original_url == "http://x/img.jpg"
    assert listing.image_clean_url is None  # filled in by app/main.py, not pipeline.py
    assert result["clean_path"] == "/tmp/fake_clean.jpg"
    assert result["summary_audio_path"] == Path("/tmp/fake_summary.mp3")


@pytest.mark.asyncio
async def test_debug_contains_timing_for_every_stage():
    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    debug = result["debug"]
    expected_keys = {
        "image_cleanup_sec",
        "transcription_sec",
        "numbers_parsing_sec",
        "llm_extraction_sec",
        "merge_sec",
        "description_generation_sec",
        "tts_sec",
        "total_sec",
    }
    assert expected_keys.issubset(debug.keys())
    for key in expected_keys:
        assert isinstance(debug[key], (int, float))
        assert debug[key] >= 0


# ---------------------------------------------------------------------------
# Genuine concurrency in the two parallel stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_cleanup_and_transcription_run_concurrently(monkeypatch):
    delay = 0.25

    def slow_clean(image_path):
        time.sleep(delay)
        return {"clean_path": "/tmp/c.jpg", "alpha_path": "/tmp/a.png", "warnings": []}

    def slow_transcribe(audio_path, language_hint=None, **kwargs):
        time.sleep(delay)
        return {"text": "teen ghante", "detected_language": "hi", "confidence": 0.9, "duration_sec": 2.0}

    monkeypatch.setattr(pipeline_module, "clean_product_image", slow_clean)
    monkeypatch.setattr(pipeline_module, "transcribe", slow_transcribe)

    start = time.monotonic()
    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")
    wall_clock = time.monotonic() - start

    # If run sequentially this would take >= 2 * delay; concurrently it
    # should take roughly `delay` (plus the fast downstream stages).
    assert wall_clock < delay * 1.8, f"stage 1 does not appear to run concurrently: {wall_clock}s"
    assert result["debug"]["image_cleanup_sec"] >= delay
    assert result["debug"]["transcription_sec"] >= delay


@pytest.mark.asyncio
async def test_numbers_parsing_and_llm_extraction_run_concurrently(monkeypatch):
    delay = 0.25

    def slow_extract_fields(text, language):
        time.sleep(delay)
        return ExtractedFields(category="home decor")

    monkeypatch.setattr(pipeline_module, "extract_fields", slow_extract_fields)

    start = time.monotonic()
    await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")
    wall_clock = time.monotonic() - start

    assert wall_clock < delay * 1.8


# ---------------------------------------------------------------------------
# Degrade, don't crash — one stage per test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asr_failure_degrades_transcript_but_not_image(monkeypatch):
    def failing_transcribe(audio_path, language_hint=None, **kwargs):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(pipeline_module, "transcribe", failing_transcribe)

    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    assert "transcription_failed" in result["errors"]
    assert "RuntimeError" in result["debug"]["transcription_error"]
    assert result["listing"].transcript_raw is None
    assert result["listing"].source_language == "en"  # fallback when no language_hint given
    # Image cleanup is unaffected by the ASR failure.
    assert result["clean_path"] == "/tmp/fake_clean.jpg"


@pytest.mark.asyncio
async def test_asr_failure_falls_back_to_language_hint_if_given(monkeypatch):
    def failing_transcribe(audio_path, language_hint=None, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline_module, "transcribe", failing_transcribe)

    result = await create_listing(
        "art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg", language_hint="ta"
    )
    assert result["listing"].source_language == "ta"


@pytest.mark.asyncio
async def test_image_cleanup_exception_degrades_but_asr_and_extraction_continue(monkeypatch):
    def failing_clean(image_path):
        raise ValueError("could not read image")

    monkeypatch.setattr(pipeline_module, "clean_product_image", failing_clean)

    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    assert "image_cleanup_failed" in result["errors"]
    assert result["clean_path"] is None
    assert result["alpha_path"] is None
    # Everything downstream of the transcript is unaffected.
    assert result["listing"].material_cost_inr == 500
    assert result["listing"].title_en == "Title"


@pytest.mark.asyncio
async def test_image_cleanup_retake_warning_surfaces_without_being_a_crash(monkeypatch):
    monkeypatch.setattr(
        pipeline_module,
        "clean_product_image",
        _fake_clean(clean_path=None, alpha_path=None, warnings=["retake"]),
    )

    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    assert "retake" in result["errors"]
    assert result["clean_path"] is None
    # Not treated as an exception-style failure — no image_cleanup_failed code.
    assert "image_cleanup_failed" not in result["errors"]
    # The rest of the pipeline still ran.
    assert result["listing"].material_cost_inr == 500


@pytest.mark.asyncio
async def test_llm_extraction_failure_degrades_but_numbers_parsing_still_works(monkeypatch):
    from app.extract import ExtractionError

    def failing_extract(text, language):
        raise ExtractionError("groq is down")

    monkeypatch.setattr(pipeline_module, "extract_fields", failing_extract)

    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    assert "extraction_failed" in result["errors"]
    listing = result["listing"]
    assert listing.category is None
    assert listing.craft_type is None
    # Numbers parsing doesn't depend on the LLM at all — it still ran on
    # the (successfully transcribed) text and populated these.
    assert listing.material_cost_inr == 500
    assert listing.hours_worked == 3.0


@pytest.mark.asyncio
async def test_description_failure_still_reads_back_her_numbers(monkeypatch):
    """The title and description need the LLM; the readback does not. When
    the copy fails she still hears her own cost and hours, in her language."""
    from app.describe import DescriptionError

    def failing_describe(fields, language):
        raise DescriptionError("groq is down")

    monkeypatch.setattr(pipeline_module, "generate_descriptions", failing_describe)
    speak_calls = []
    monkeypatch.setattr(pipeline_module, "speak", lambda text, lang: speak_calls.append((text, lang)))

    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    assert "description_generation_failed" in result["errors"]
    listing = result["listing"]
    assert listing.title_en is None
    assert listing.summary_spoken and "500" in listing.summary_spoken and "3" in listing.summary_spoken
    assert speak_calls == [(listing.summary_spoken, "hi")]
    # Factual fields from merge are untouched by the description failure.
    assert listing.material_cost_inr == 500


@pytest.mark.asyncio
async def test_tts_failure_degrades_but_listing_is_otherwise_complete(monkeypatch):
    def failing_speak(text, language):
        raise RuntimeError("gTTS network error")

    monkeypatch.setattr(pipeline_module, "speak", failing_speak)

    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    assert "tts_failed" in result["errors"]
    assert result["summary_audio_path"] is None
    assert result["listing"].summary_spoken is not None  # text generation still succeeded


@pytest.mark.asyncio
async def test_merge_failure_falls_back_to_llm_fields(monkeypatch):
    def failing_merge(llm_fields, money, duration):
        raise RuntimeError("merge blew up")

    monkeypatch.setattr(pipeline_module, "merge_extraction", failing_merge)

    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    assert "merge_failed" in result["errors"]
    # Best-effort fallback: at least the LLM's own fields survive.
    assert result["listing"].category == "home decor"


@pytest.mark.asyncio
async def test_numbers_parsing_failure_degrades_to_empty_matches(monkeypatch):
    def failing_numbers(transcript, language):
        raise RuntimeError("regex blew up")

    monkeypatch.setattr(pipeline_module, "_parse_numbers", failing_numbers)

    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    assert "numbers_parsing_failed" in result["errors"]
    listing = result["listing"]
    # Zero money/duration matches -> merge.py's own rule flags both for
    # confirmation rather than fabricating a value.
    assert listing.material_cost_inr is None
    assert "material_cost_inr" in listing.needs_confirmation
    assert listing.hours_worked is None
    assert "hours_worked" in listing.needs_confirmation


@pytest.mark.asyncio
async def test_multiple_simultaneous_stage_failures_all_degrade_independently(monkeypatch):
    def failing_transcribe(audio_path, language_hint=None, **kwargs):
        raise RuntimeError("asr down")

    def failing_speak(text, language):
        raise RuntimeError("tts down")

    monkeypatch.setattr(pipeline_module, "transcribe", failing_transcribe)
    monkeypatch.setattr(pipeline_module, "speak", failing_speak)

    result = await create_listing("art_1", Path("/tmp/img.jpg"), Path("/tmp/audio.wav"), "http://x/img.jpg")

    # ASR and TTS fail independently — both show up, neither one crashes
    # the other's contribution to the result.
    #
    # Zero money/duration matches from an empty transcript still means
    # merge.py flags both as needing confirmation (its normal
    # "ambiguous/absent" rule — see app/merge.py), so summary_spoken is
    # still generated (asking those two questions) and TTS is still
    # attempted on it — correctly reported as its own independent failure
    # since it's mocked to fail here too.
    assert set(result["errors"]) == {"transcription_failed", "tts_failed"}
    assert result["listing"].needs_confirmation == ["material_cost_inr", "hours_worked"]
    assert result["listing"].summary_spoken is not None
    assert result["summary_audio_path"] is None
    assert result["clean_path"] == "/tmp/fake_clean.jpg"  # image cleanup unaffected
