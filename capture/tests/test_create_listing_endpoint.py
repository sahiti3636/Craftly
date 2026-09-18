"""HTTP-level tests for POST /listing/create and GET /demo.

Mocks the same network/model boundaries as tests/test_create_listing.py
(image cleanup, ASR, Groq, TTS) so this stays fast/deterministic while
exercising the real FastAPI endpoint: multipart parsing, URL building for
image_clean_url/clean_thumb_url/summary_audio_url, and the response shape
(errors/debug surfaced even on full success).
"""

import io
import json

import pytest
from fastapi.testclient import TestClient

import app.describe as describe_module
import app.main as main_module
import app.pipeline as pipeline_module
from app.extract import ExtractedFields


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _mock_heavy_dependencies(monkeypatch, tmp_path):
    def fake_transcribe(audio_path, language_hint=None):
        return {
            "text": "paanch sau rupaye, teen ghante",
            "detected_language": "hi",
            "confidence": 0.9,
            "duration_sec": 2.0,
        }

    def fake_extract_fields(text, language):
        return ExtractedFields(category="home decor", craft_type="pottery", material="terracotta")

    def fake_marketing(lang, facts):
        title = "Handmade Diya" if lang == "en" else "मिट्टी का दीया"
        return "sys", json.dumps(facts), json.dumps(
            {"title": title, "description": " ".join(["word"] * 70)}
        )

    processed_dir = tmp_path / "processed_images"
    processed_dir.mkdir()

    def fake_clean(image_path):
        clean_path = processed_dir / "test_clean.jpg"
        thumb_path = processed_dir / "test_clean_thumb.jpg"
        clean_path.write_bytes(b"fake-jpeg-bytes")
        thumb_path.write_bytes(b"fake-thumb-bytes")
        return {"clean_path": str(clean_path), "alpha_path": None, "warnings": []}

    audio_dir = tmp_path / "audio_files"

    def fake_speak(text, language):
        audio_dir.mkdir(parents=True, exist_ok=True)
        out_path = audio_dir / "summary.mp3"
        out_path.write_bytes(b"fake-audio-bytes")
        return out_path

    monkeypatch.setattr(pipeline_module, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline_module, "extract_fields", fake_extract_fields)
    monkeypatch.setattr(pipeline_module, "clean_product_image", fake_clean)
    monkeypatch.setattr(pipeline_module, "speak", fake_speak)
    monkeypatch.setattr(describe_module, "_call_groq_marketing", fake_marketing)


def _post_create(client: TestClient, **overrides) -> object:
    data = {"artisan_id": "art_1"}
    data.update(overrides)
    files = {
        "image": ("photo.jpg", io.BytesIO(b"fake image bytes"), "image/jpeg"),
        "audio": ("clip.wav", io.BytesIO(b"fake audio bytes"), "audio/wav"),
    }
    return client.post("/listing/create", data=data, files=files)


def test_create_listing_happy_path(client):
    response = _post_create(client)
    assert response.status_code == 200, response.text
    body = response.json()

    listing = body["listing"]
    assert listing["material_cost_inr"] == 500
    assert listing["hours_worked"] == 3.0
    assert listing["needs_confirmation"] == []
    assert listing["category"] == "home decor"
    assert listing["title_en"] == "Handmade Diya"

    assert body["errors"] == []
    assert set(body["debug"].keys()) >= {
        "image_cleanup_sec",
        "transcription_sec",
        "numbers_parsing_sec",
        "llm_extraction_sec",
        "merge_sec",
        "description_generation_sec",
        "tts_sec",
        "total_sec",
    }


def test_create_listing_returns_well_formed_image_and_audio_urls(client):
    response = _post_create(client)
    body = response.json()

    # StaticFiles mounts are bound to their directory at app-creation
    # time, so a monkeypatched directory (this fixture's tmp_path) can't
    # be retroactively wired to actually serve — that's covered instead
    # by /listing/draft's existing image/audio URL tests against the
    # real mounted dirs. This test just confirms /listing/create builds
    # the right *shape* of URL, from the right route, for each asset.
    assert body["listing"]["image_clean_url"].startswith("http://testserver/processed/")
    assert body["clean_thumb_url"].startswith("http://testserver/processed/")
    assert body["summary_audio_url"].startswith("http://testserver/audio/")


def test_create_listing_degrades_on_asr_failure_instead_of_erroring(client, monkeypatch):
    def failing_transcribe(audio_path, language_hint=None):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(pipeline_module, "transcribe", failing_transcribe)

    response = _post_create(client)

    # Still a 200 — a single stage failing must degrade, not crash the request.
    assert response.status_code == 200
    body = response.json()
    assert "transcription_failed" in body["errors"]
    assert body["listing"]["transcript_raw"] is None
    # Image cleanup still succeeded and is reflected in the response.
    assert body["listing"]["image_clean_url"] is not None


def test_create_listing_degrades_on_image_cleanup_failure(client, monkeypatch):
    def failing_clean(image_path):
        raise ValueError("could not read image")

    monkeypatch.setattr(pipeline_module, "clean_product_image", failing_clean)

    response = _post_create(client)
    assert response.status_code == 200
    body = response.json()
    assert "image_cleanup_failed" in body["errors"]
    assert body["listing"]["image_clean_url"] is None
    assert body["clean_thumb_url"] is None
    # The raw uploaded image is still there regardless.
    assert body["listing"]["image_original_url"] is not None
    # Unaffected stages still worked.
    assert body["listing"]["material_cost_inr"] == 500


def test_create_listing_with_language_hint(client):
    response = _post_create(client, language_hint="ta")
    assert response.status_code == 200


def test_demo_page_loads_and_references_the_create_endpoint(client):
    response = client.get("/demo")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "/listing/create" in response.text
    assert "<form" in response.text
    assert 'type="file"' in response.text
