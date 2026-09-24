"""Integration test for the artisan confirmation flow.

Drafts a listing from a transcript with an ambiguous cost mention (two
different rupee amounts stated for the same item), confirms it lands in
needs_confirmation, submits a correction for it, and confirms the final
listing is complete (no more open confirmations, correct field values).

This exercises the real HTTP endpoints (via FastAPI's TestClient), the
real app/numbers.py parser, the real app/merge.py rule, the real
app/pipeline.py orchestration, and the real deterministic
summary_spoken templates in app/describe.py. Only the network/model-heavy
boundaries are mocked: ASR transcription (app.pipeline.transcribe), the
Groq LLM field-extraction call, the Groq marketing-copy calls, and TTS
synthesis — the same boundary-mocking approach used throughout this
project's test suite (see tests/test_extract.py, tests/test_describe.py).
"""

import io
import json

import pytest
from fastapi.testclient import TestClient

import app.describe as describe_module
import app.main as main_module
import app.pipeline as pipeline_module
from app.extract import ExtractedFields

AMBIGUOUS_TRANSCRIPT = (
    "material ka cost paanch sau rupaye tha, nahi nahi, teen sau rupaye tha shayad, "
    "isme teen ghante lage"
)


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _mock_heavy_dependencies(monkeypatch, tmp_path):
    def fake_transcribe(audio_path, language_hint=None):
        return {
            "text": AMBIGUOUS_TRANSCRIPT,
            "detected_language": "hi",
            "confidence": 0.95,
            "duration_sec": 5.0,
        }

    def fake_extract_fields(text, language):
        return ExtractedFields(
            category="home decor",
            craft_type="hand-molded pottery",
            material="terracotta",
            product_description_facts=["hand-molded"],
            confidence={"category": 0.9, "craft_type": 0.85, "material": 0.9},
        )

    def fake_marketing(lang, facts):
        title = "Handmade Terracotta Diya" if lang == "en" else "मिट्टी का दीया"
        description = " ".join(["word"] * 70)
        return "sys", json.dumps(facts), json.dumps({"title": title, "description": description})

    audio_dir = tmp_path / "audio_files"

    def fake_speak(text, language):
        audio_dir.mkdir(parents=True, exist_ok=True)
        out_path = audio_dir / f"{abs(hash(text))}.mp3"
        out_path.write_bytes(b"fake-audio-bytes")
        return out_path

    monkeypatch.setattr(pipeline_module, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline_module, "extract_fields", fake_extract_fields)
    monkeypatch.setattr(describe_module, "_call_groq_marketing", fake_marketing)
    monkeypatch.setattr(main_module, "speak", fake_speak)
    monkeypatch.setattr(main_module, "AUDIO_DIR", audio_dir)


def _draft(client: TestClient) -> dict:
    response = client.post(
        "/listing/draft",
        data={"artisan_id": "art_flow_test"},
        files={
            "audio": ("clip.wav", io.BytesIO(b"fake audio bytes"), "audio/wav"),
            "image": ("photo.jpg", io.BytesIO(b"fake image bytes"), "image/jpeg"),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_draft_then_confirm_ambiguous_cost_flow(client):
    # --- Draft: the transcript states two different costs for the same
    # item, so app/numbers.py finds 2 money matches and app/merge.py must
    # refuse to guess between them. ---
    draft_body = _draft(client)
    listing = draft_body["listing"]

    assert listing["needs_confirmation"] == ["material_cost_inr"]
    assert listing["material_cost_inr"] is None
    # The single, unambiguous duration mention must NOT be flagged.
    assert listing["hours_worked"] == 3.0
    assert "hours_worked" not in listing["needs_confirmation"]

    # Requirement 3: a flagged field must be spoken back as a question,
    # not stated as fact.
    assert "?" in listing["summary_spoken"]
    assert "सामान की लागत कितनी थी?" in listing["summary_spoken"]
    # And a real number must never be fabricated for it.
    assert "None" not in listing["summary_spoken"]

    # The draft response carries summary_spoken (already asserted above,
    # as part of `listing`) and a servable audio URL for it.
    assert draft_body["summary_audio_url"] is not None
    assert draft_body["summary_audio_url"].startswith("http")

    listing_id = listing["listing_id"]

    # --- Confirm: the artisan corrects the ambiguous cost. ---
    confirm_response = client.post(
        "/listing/confirm",
        json={
            "listing_id": listing_id,
            "confirmed": True,
            "corrections": {"material_cost_inr": 500},
        },
    )
    assert confirm_response.status_code == 200, confirm_response.text
    confirm_body = confirm_response.json()
    final = confirm_body["listing"]

    # --- The final listing must be complete. ---
    assert final["material_cost_inr"] == 500
    assert final["needs_confirmation"] == []
    assert final["hours_worked"] == 3.0
    assert "?" not in final["summary_spoken"]
    assert confirm_body["summary_audio_url"] is not None

    # Semantic fields (never touched by this numbers-only correction)
    # must be exactly what the draft produced.
    assert final["category"] == listing["category"]
    assert final["craft_type"] == listing["craft_type"]
    assert final["material"] == listing["material"]
    assert final["title_en"] == listing["title_en"]
    assert final["description_en"] == listing["description_en"]

    # Every other Listing field required for a "complete" listing is present.
    for field in ("listing_id", "artisan_id", "title_en", "title_hi", "description_en", "description_hi"):
        assert final[field], f"{field} should be populated on a complete listing"


def test_draft_endpoint_rejects_unsupported_audio_format(client, monkeypatch):
    # This one deliberately uses the REAL app.asr.transcribe (undoing the
    # autouse mock) since it's the format check inside ASR itself being
    # tested here, not the pipeline's use of its result.
    from app.asr import transcribe as real_transcribe

    monkeypatch.setattr(pipeline_module, "transcribe", real_transcribe)

    response = client.post(
        "/listing/draft",
        data={"artisan_id": "art_1"},
        files={
            "audio": ("clip.flac", io.BytesIO(b"not real audio"), "audio/flac"),
            "image": ("photo.jpg", io.BytesIO(b"fake image"), "image/jpeg"),
        },
    )
    assert response.status_code == 415


def test_confirm_endpoint_404s_for_unknown_listing_id(client):
    response = client.post(
        "/listing/confirm",
        json={"listing_id": "lst_does_not_exist", "confirmed": True, "corrections": {}},
    )
    assert response.status_code == 404


def test_confirm_endpoint_400s_for_uncorrectable_field(client):
    draft_body = _draft(client)
    listing_id = draft_body["listing"]["listing_id"]

    response = client.post(
        "/listing/confirm",
        json={
            "listing_id": listing_id,
            "confirmed": False,
            "corrections": {"title_en": "not a real correction target"},
        },
    )
    assert response.status_code == 400


def test_confirm_endpoint_400s_for_wrong_typed_correction_value(client):
    draft_body = _draft(client)
    listing_id = draft_body["listing"]["listing_id"]

    response = client.post(
        "/listing/confirm",
        json={
            "listing_id": listing_id,
            "confirmed": False,
            "corrections": {"material_cost_inr": "not a number"},
        },
    )
    assert response.status_code == 400


def test_confirming_without_corrections_clears_needs_confirmation(client):
    draft_body = _draft(client)
    listing_id = draft_body["listing"]["listing_id"]
    assert draft_body["listing"]["needs_confirmation"] == ["material_cost_inr"]

    response = client.post(
        "/listing/confirm",
        json={"listing_id": listing_id, "confirmed": True, "corrections": {}},
    )
    assert response.status_code == 200
    final = response.json()["listing"]

    # confirmed=True signs off on the listing as-is, even without a
    # correction for the field that was flagged.
    assert final["needs_confirmation"] == []
    # material_cost_inr itself is still unknown — confirming doesn't
    # fabricate a value, it just stops asking.
    assert final["material_cost_inr"] is None


def test_semantic_correction_regenerates_description_via_confirm_endpoint(client, monkeypatch):
    marketing_calls: list[str] = []

    def counting_marketing(lang, facts):
        marketing_calls.append(lang)
        title = f"Regenerated {lang}"
        description = " ".join(["word"] * 70)
        return "sys", json.dumps(facts), json.dumps({"title": title, "description": description})

    monkeypatch.setattr(describe_module, "_call_groq_marketing", counting_marketing)

    draft_body = _draft(client)
    listing_id = draft_body["listing"]["listing_id"]
    marketing_calls.clear()  # ignore the draft's own calls

    response = client.post(
        "/listing/confirm",
        json={
            "listing_id": listing_id,
            "confirmed": False,
            "corrections": {"material": "brass"},
        },
    )
    assert response.status_code == 200
    final = response.json()["listing"]

    assert final["material"] == "brass"
    assert marketing_calls == ["en", "hi"]
    assert final["title_en"] == "Regenerated en"
