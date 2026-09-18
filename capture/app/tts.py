"""Text-to-speech for reading a listing's summary_spoken back to the
artisan.

Backend is pluggable: speak() always calls whatever TTSBackend is
configured (via CRAFTLY_TTS_BACKEND), so swapping gTTS for an on-device
Android TTS engine or an AI4Bharat Indic-TTS model later is a matter of
implementing one class and flipping an env var — no caller of speak()
needs to change. GTTSBackend is the only working implementation today;
AndroidTTSBackend and AI4BharatTTSBackend are placeholders that make the
intended shape of a real implementation visible (same pattern as the
empty app/lang/te.py-style stubs elsewhere in this codebase).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from uuid import uuid4

AUDIO_DIR = Path(os.environ.get("CRAFTLY_AUDIO_DIR", "audio_files"))
_TTS_BACKEND_NAME = os.environ.get("CRAFTLY_TTS_BACKEND", "gtts")


class TTSBackend(ABC):
    @abstractmethod
    def synthesize(self, text: str, language: str, out_path: Path) -> None:
        """Write synthesized speech for `text` in `language` to `out_path`."""


class GTTSBackend(TTSBackend):
    """Development backend: Google Translate's TTS via the gTTS library.
    Requires network access. Not meant to survive into production — it's
    here so the pipeline is runnable and testable before a real backend
    (on-device or AI4Bharat) is wired in.
    """

    # Our language codes map 1:1 onto gTTS's for hi/en/te/kn/ta today;
    # kept as an explicit table (not an identity passthrough) so a future
    # mismatch is a one-line data fix here, not a code change.
    LANGUAGE_MAP: dict[str, str] = {
        "en": "en",
        "hi": "hi",
        "te": "te",
        "kn": "kn",
        "ta": "ta",
    }

    def synthesize(self, text: str, language: str, out_path: Path) -> None:
        from gtts import gTTS

        lang_code = self.LANGUAGE_MAP.get(language, language)
        tts = gTTS(text=text, lang=lang_code, tld="co.in")
        tts.save(str(out_path))


class AndroidTTSBackend(TTSBackend):
    """Placeholder for the on-device Android TTS engine.

    This backend only makes sense running inside the Android app process
    itself (it would call the platform TextToSpeech API directly), not
    from this server — it's included so the registry/interface shape for
    a future integration is visible here, not so it can be selected today.
    """

    def synthesize(self, text: str, language: str, out_path: Path) -> None:
        raise NotImplementedError(
            "AndroidTTSBackend is a placeholder for the on-device app; "
            "it cannot run from this server."
        )


class AI4BharatTTSBackend(TTSBackend):
    """Placeholder for an AI4Bharat Indic-TTS model backend."""

    def synthesize(self, text: str, language: str, out_path: Path) -> None:
        raise NotImplementedError("AI4Bharat TTS backend is not implemented yet.")


_BACKENDS: dict[str, type[TTSBackend]] = {
    "gtts": GTTSBackend,
    "android": AndroidTTSBackend,
    "ai4bharat": AI4BharatTTSBackend,
}

_backend_instance: TTSBackend | None = None


def _get_backend() -> TTSBackend:
    global _backend_instance
    if _backend_instance is None:
        backend_cls = _BACKENDS.get(_TTS_BACKEND_NAME)
        if backend_cls is None:
            raise ValueError(
                f"Unknown TTS backend {_TTS_BACKEND_NAME!r}; choices: {sorted(_BACKENDS)}"
            )
        _backend_instance = backend_cls()
    return _backend_instance


def speak(text: str, language: str) -> Path:
    """Synthesize `text` (in `language`) to an audio file and return its
    local path. The configured backend (CRAFTLY_TTS_BACKEND, default
    "gtts") decides how; callers never need to know which one is active.
    """
    if not text or not text.strip():
        raise ValueError("speak() requires non-empty text")

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AUDIO_DIR / f"{uuid4().hex}.mp3"
    _get_backend().synthesize(text, language, out_path)
    return out_path
