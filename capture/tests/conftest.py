"""Shared test fixtures.

Sets CRAFTLY_ASR_MODEL_SIZE=tiny before app.asr is ever imported, so the
test suite exercises the real faster-whisper pipeline without paying for
the "small" model's download/load time. This relies on app/asr.py reading
its model config from the environment (the same knob a developer would use
in production), not on any test-only code path.
"""

import os
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("CRAFTLY_ASR_MODEL_SIZE", "tiny")
# The test suite never calls Groq: transcription runs on the local model.
os.environ.setdefault("CRAFTLY_ASR_BACKEND", "local")
os.environ.setdefault("CRAFTLY_ASR_DEVICE", "cpu")
os.environ.setdefault("CRAFTLY_ASR_COMPUTE_TYPE", "int8")

# Same reasoning as the ASR "tiny" override above: u2netp is a lightweight
# variant of the same model family as the production default (u2net, see
# app/image.py's licensing note), so tests exercise the real rembg
# pipeline without paying for the full-size model's slower inference.
os.environ.setdefault("CRAFTLY_REMBG_MODEL", "u2netp")

# Directories app.main/app.tts/app.describe create relative to the repo
# root for real runtime artifacts (audio, uploaded images, debug logs).
# They're gitignored, but tests that hit the real /listing/draft endpoint
# (e.g. tests/test_listing_flow.py) write real files into them — clean up
# once per test session so a `pytest` run doesn't leave clutter behind.
_RUNTIME_DIRS = [Path("audio_files"), Path("images"), Path("logs"), Path("processed_images")]


@pytest.fixture(scope="session", autouse=True)
def _cleanup_runtime_dirs():
    yield
    for d in _RUNTIME_DIRS:
        shutil.rmtree(d, ignore_errors=True)


def make_tone_wav(
    path: Path, duration_sec: float, freq_hz: float = 440.0, sample_rate: int = 16000
) -> Path:
    """Write a synthetic mono 16-bit PCM WAV file containing a pure tone."""
    n_samples = int(duration_sec * sample_rate)
    t = np.arange(n_samples) / sample_rate
    audio = (3000 * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio.tobytes())
    return path


@pytest.fixture
def tone_wav_factory(tmp_path):
    def _make(duration_sec: float, name: str = "clip.wav") -> Path:
        return make_tone_wav(tmp_path / name, duration_sec)

    return _make


@pytest.fixture(autouse=True)
def _reset_listing_store():
    """app.store keeps drafts in a module-level dict — clear it before and
    after every test so listing_ids from one test can never leak into
    another (e.g. a stale draft satisfying a lookup it shouldn't).
    """
    import app.store as store

    store._records.clear()
    yield
    store._records.clear()
