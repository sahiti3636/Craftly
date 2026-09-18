from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import make_tone_wav

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_transcribe_endpoint_returns_expected_shape(tmp_path):
    clip = make_tone_wav(tmp_path / "clip.wav", duration_sec=2.0)
    with open(clip, "rb") as f:
        response = client.post(
            "/transcribe",
            files={"audio": ("clip.wav", f, "audio/wav")},
            data={"language_hint": "en"},
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"text", "detected_language", "confidence", "duration_sec"}
    assert body["detected_language"] == "en"


def test_transcribe_endpoint_rejects_unsupported_format(tmp_path):
    bad_file = tmp_path / "clip.flac"
    bad_file.write_bytes(b"not real audio")
    with open(bad_file, "rb") as f:
        response = client.post(
            "/transcribe",
            files={"audio": ("clip.flac", f, "application/octet-stream")},
        )
    assert response.status_code == 415


def test_transcribe_endpoint_rejects_short_clip(tmp_path):
    clip = make_tone_wav(tmp_path / "clip.wav", duration_sec=0.5)
    with open(clip, "rb") as f:
        response = client.post(
            "/transcribe",
            files={"audio": ("clip.wav", f, "audio/wav")},
        )
    assert response.status_code == 422
