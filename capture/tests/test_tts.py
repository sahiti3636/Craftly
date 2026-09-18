"""Tests for app/tts.py.

Uses a fake TTSBackend (no network) to test speak()'s own behavior —
file path/naming, directory creation, backend selection, error handling
— without depending on gTTS or network access. The pluggability itself
(swapping backends without touching callers) is exercised directly by
injecting a different backend implementation.
"""

from pathlib import Path

import pytest

import app.tts as tts_module
from app.tts import TTSBackend, speak


class _FakeBackend(TTSBackend):
    def __init__(self):
        self.calls: list[tuple[str, str, Path]] = []

    def synthesize(self, text: str, language: str, out_path: Path) -> None:
        self.calls.append((text, language, out_path))
        out_path.write_bytes(b"fake-audio-bytes")


@pytest.fixture(autouse=True)
def _isolate_audio_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_module, "AUDIO_DIR", tmp_path / "audio_files")
    monkeypatch.setattr(tts_module, "_backend_instance", None)
    yield


def test_speak_writes_a_file_and_returns_its_path(monkeypatch):
    fake = _FakeBackend()
    monkeypatch.setattr(tts_module, "_backend_instance", fake)

    out_path = speak("hello world", "en")

    assert out_path.exists()
    assert out_path.read_bytes() == b"fake-audio-bytes"
    assert out_path.suffix == ".mp3"


def test_speak_creates_audio_dir_if_missing(monkeypatch, tmp_path):
    fresh_dir = tmp_path / "does" / "not" / "exist" / "yet"
    monkeypatch.setattr(tts_module, "AUDIO_DIR", fresh_dir)
    monkeypatch.setattr(tts_module, "_backend_instance", _FakeBackend())

    assert not fresh_dir.exists()
    speak("hello", "en")
    assert fresh_dir.exists()


def test_speak_passes_text_and_language_through_to_backend(monkeypatch):
    fake = _FakeBackend()
    monkeypatch.setattr(tts_module, "_backend_instance", fake)

    speak("yeh aapka diya hai", "hi")

    assert len(fake.calls) == 1
    text, language, out_path = fake.calls[0]
    assert text == "yeh aapka diya hai"
    assert language == "hi"


def test_speak_generates_a_unique_filename_per_call(monkeypatch):
    monkeypatch.setattr(tts_module, "_backend_instance", _FakeBackend())

    path1 = speak("first", "en")
    path2 = speak("second", "en")

    assert path1 != path2


def test_speak_rejects_empty_text():
    with pytest.raises(ValueError):
        speak("", "en")


def test_speak_rejects_whitespace_only_text():
    with pytest.raises(ValueError):
        speak("   ", "en")


def test_unknown_backend_name_raises_clear_error(monkeypatch):
    monkeypatch.setattr(tts_module, "_TTS_BACKEND_NAME", "not-a-real-backend")
    monkeypatch.setattr(tts_module, "_backend_instance", None)

    with pytest.raises(ValueError, match="Unknown TTS backend"):
        speak("hello", "en")


# ---------------------------------------------------------------------------
# Pluggability: swapping the backend doesn't require touching speak()'s
# callers — that's the entire point of the interface.
# ---------------------------------------------------------------------------


def test_backend_can_be_swapped_without_changing_speak_callers(monkeypatch):
    class _AlternateBackend(TTSBackend):
        def synthesize(self, text, language, out_path):
            out_path.write_text(f"ALT::{language}::{text}")

    monkeypatch.setattr(tts_module, "_backend_instance", _AlternateBackend())

    out_path = speak("namaste", "hi")
    assert out_path.read_text() == "ALT::hi::namaste"


def test_placeholder_backends_raise_not_implemented(tmp_path):
    from app.tts import AI4BharatTTSBackend, AndroidTTSBackend

    with pytest.raises(NotImplementedError):
        AndroidTTSBackend().synthesize("hi", "en", tmp_path / "out.mp3")
    with pytest.raises(NotImplementedError):
        AI4BharatTTSBackend().synthesize("hi", "en", tmp_path / "out.mp3")


def test_gtts_backend_maps_language_codes():
    from app.tts import GTTSBackend

    backend = GTTSBackend()
    for code in ("en", "hi", "te", "kn", "ta"):
        assert backend.LANGUAGE_MAP[code] == code
