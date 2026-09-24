"""The voiceover for a reel: one spoken line per beat.

Same backend as A2's readback to the artisan (`capture/app/tts.py`):
gTTS, Google Translate's voice. It is duplicated rather than imported
because both packages are called `app`. gTTS needs the internet while a
reel is built; `app/reel.py` treats any failure here as "no voiceover"
and still ships the silent reel, with a warning saying why.

`CRAFTLY_REEL_VOICE=off` turns narration off entirely — the tests do
this so they never touch the network.
"""

from __future__ import annotations

from pathlib import Path

from app import config

#: Our language codes as gTTS names them. Explicit, so a future mismatch
#: is a one-line data fix.
_GTTS_LANGUAGES = {"en": "en", "hi": "hi"}


class VoiceError(RuntimeError):
    """No voiceover could be made; the message says why."""


def enabled() -> bool:
    return config.REEL_VOICE != "off"


def synthesize(text: str, lang: str, out_path: Path) -> None:
    """Write `text`, spoken in `lang`, to `out_path` as an MP3."""
    if config.REEL_VOICE != "gtts":
        raise VoiceError(f"Unknown reel voice {config.REEL_VOICE!r}; use 'gtts' or 'off'.")
    try:
        from gtts import gTTS
    except ImportError as exc:
        raise VoiceError("gTTS is not installed, so the reel has no voiceover.") from exc
    try:
        # co.in: the Indian English voice, which also reads Indian names
        # in a Hindi line far better than the US one.
        gTTS(text=text, lang=_GTTS_LANGUAGES.get(lang, "en"), tld="co.in", timeout=15).save(str(out_path))
    except Exception as exc:  # gTTS raises its own errors and requests' alike
        raise VoiceError(f"The voiceover could not be generated ({exc}).") from exc
