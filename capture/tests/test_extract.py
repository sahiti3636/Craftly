"""Tests for app/extract.py.

All tests here monkeypatch app.extract._call_groq so the suite is fast,
deterministic, and never makes a real network call or spends API credits.
The 15-transcript fixture (tests/fixtures/transcripts.json) is reused
here to check extract_fields() correctly parses a variety of realistic
mock LLM responses and never touches material_cost_inr/hours_worked.
"""

import json
from pathlib import Path

import pytest

import app.extract as extract_module
from app.extract import ExtractedFields, ExtractionError, extract_fields

FIXTURES_PATH = Path(__file__).resolve().parent / "fixtures" / "transcripts.json"
FIXTURES = json.loads(FIXTURES_PATH.read_text())


@pytest.fixture(autouse=True)
def _isolate_log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_module, "_LOG_PATH", tmp_path / "extract.jsonl")
    return tmp_path / "extract.jsonl"


def _mock_call_groq(system_prompt: str, raw_response: str):
    def _fake(transcript, detected_language):
        return system_prompt, raw_response

    return _fake


# ---------------------------------------------------------------------------
# Happy path, driven by the 15-transcript fixture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", FIXTURES, ids=[c["id"] for c in FIXTURES])
def test_extract_fields_parses_fixture_transcripts(monkeypatch, case):
    monkeypatch.setattr(
        extract_module,
        "_call_groq",
        _mock_call_groq("fake system prompt", json.dumps(case["mock_llm_extraction"])),
    )

    result = extract_fields(case["transcript"], case["language"])

    expected = case["mock_llm_extraction"]
    assert result.category == expected["category"]
    assert result.craft_type == expected["craft_type"]
    assert result.material == expected["material"]
    assert result.colours == expected["colours"]
    assert result.dimensions == expected["dimensions"]
    assert result.product_description_facts == expected["product_description_facts"]
    assert result.confidence == expected["confidence"]

    # extract_fields must never populate money/hours or needs_confirmation —
    # that's app/merge.py's job.
    assert result.material_cost_inr is None
    assert result.hours_worked is None
    assert result.needs_confirmation == []


# ---------------------------------------------------------------------------
# Empty transcript short-circuit
# ---------------------------------------------------------------------------


def test_extract_fields_empty_transcript_returns_default_without_calling_groq(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Groq should not be called for an empty transcript")

    monkeypatch.setattr(extract_module, "_call_groq", _fail_if_called)

    result = extract_fields("", "hi")
    assert result == ExtractedFields()


def test_extract_fields_whitespace_only_transcript_returns_default(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Groq should not be called for a whitespace-only transcript")

    monkeypatch.setattr(extract_module, "_call_groq", _fail_if_called)

    result = extract_fields("   \n  ", "hi")
    assert result == ExtractedFields()


# ---------------------------------------------------------------------------
# Retry-once-then-fail-clean behavior
# ---------------------------------------------------------------------------


def test_extract_fields_retries_once_then_succeeds(monkeypatch):
    responses = iter(["not json at all", json.dumps({"category": "pottery"})])
    call_count = {"n": 0}

    def _fake(transcript, detected_language):
        call_count["n"] += 1
        return "system prompt", next(responses)

    monkeypatch.setattr(extract_module, "_call_groq", _fake)

    result = extract_fields("teen ghante lage", "hi")
    assert result.category == "pottery"
    assert call_count["n"] == 2


def test_extract_fields_fails_cleanly_after_two_malformed_responses(monkeypatch):
    call_count = {"n": 0}

    def _fake(transcript, detected_language):
        call_count["n"] += 1
        return "system prompt", "this is not valid json {{{"

    monkeypatch.setattr(extract_module, "_call_groq", _fake)

    with pytest.raises(ExtractionError):
        extract_fields("teen ghante lage", "hi")
    assert call_count["n"] == 2


def test_extract_fields_rejects_hallucinated_money_field_and_retries(monkeypatch):
    # The LLM is told never to extract money, but if it does anyway, the
    # extra="forbid" schema must reject it rather than silently accept it.
    bad = json.dumps({"category": "pottery", "material_cost_inr": 500})
    good = json.dumps({"category": "pottery"})
    responses = iter([bad, good])
    call_count = {"n": 0}

    def _fake(transcript, detected_language):
        call_count["n"] += 1
        return "system prompt", next(responses)

    monkeypatch.setattr(extract_module, "_call_groq", _fake)

    result = extract_fields("teen ghante lage", "hi")
    assert result.category == "pottery"
    assert result.material_cost_inr is None
    assert call_count["n"] == 2


def test_extract_fields_rejects_out_of_range_confidence(monkeypatch):
    bad = json.dumps({"category": "pottery", "confidence": {"category": 1.5}})

    def _fake(transcript, detected_language):
        return "system prompt", bad

    monkeypatch.setattr(extract_module, "_call_groq", _fake)

    with pytest.raises(ExtractionError):
        extract_fields("teen ghante lage", "hi")


def test_extract_fields_raises_on_request_failure_without_retrying(monkeypatch):
    call_count = {"n": 0}

    def _fake(transcript, detected_language):
        call_count["n"] += 1
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(extract_module, "_call_groq", _fake)

    with pytest.raises(ExtractionError):
        extract_fields("teen ghante lage", "hi")
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_extract_fields_logs_every_attempt(monkeypatch, _isolate_log_file):
    responses = iter(["not json", json.dumps({"category": "pottery"})])

    def _fake(transcript, detected_language):
        return "the system prompt used", next(responses)

    monkeypatch.setattr(extract_module, "_call_groq", _fake)

    extract_fields("teen ghante lage", "hi")

    lines = _isolate_log_file.read_text().strip().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["attempt"] == 1
    assert first["transcript"] == "teen ghante lage"
    assert first["detected_language"] == "hi"
    assert first["raw_response"] == "not json"
    assert first["error"] is not None

    second = json.loads(lines[1])
    assert second["attempt"] == 2
    assert second["error"] is None
    assert second["system_prompt"] == "the system prompt used"


def test_extract_fields_logs_request_failure(monkeypatch, _isolate_log_file):
    def _fake(transcript, detected_language):
        raise RuntimeError("boom")

    monkeypatch.setattr(extract_module, "_call_groq", _fake)

    with pytest.raises(ExtractionError):
        extract_fields("teen ghante lage", "hi")

    lines = _isolate_log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert "boom" in entry["error"]
