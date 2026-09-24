"""Speech-to-text transcription for artisan audio uploads.

Transcript only. No field extraction (title, category, material, etc.)
happens here — that is a separate downstream step that will read
Listing.transcript_raw once this module has produced it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import wave
from pathlib import Path

from faster_whisper import WhisperModel

SUPPORTED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg"}

MIN_DURATION_SEC = 1.5
MAX_DURATION_SEC = 90.0

#: The languages artisans speak to this pipeline (app/lang/ has tables for
#: each). With no hint, detection only ever picks one of these — left free,
#: Whisper has heard a slightly muffled English clip as Norwegian.
SPOKEN_LANGUAGES = ("hi", "en", "te", "ta", "kn")
#: Detected as a close relative, but meant as the key language: spoken Hindi
#: is often labelled "ur" and written in Urdu script, which nothing
#: downstream reads.
SAME_AS = {"ur": "hi"}

MODEL_SIZE = os.environ.get("CRAFTLY_ASR_MODEL_SIZE", "small")
DEVICE = os.environ.get("CRAFTLY_ASR_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("CRAFTLY_ASR_COMPUTE_TYPE", "int8")


class UnsupportedAudioFormatError(ValueError):
    """Raised when the input file extension isn't one craftly accepts."""


class AudioDurationError(ValueError):
    """Raised when a clip is shorter than MIN_DURATION_SEC or longer than MAX_DURATION_SEC."""


class AudioConversionError(RuntimeError):
    """Raised when ffmpeg is missing or fails to decode/convert the input file."""


_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    # Loaded lazily and cached: constructing a WhisperModel loads weights
    # from disk/network, which we don't want to pay on every call or at
    # import time (e.g. when tests import this module without needing it).
    global _model
    if _model is None:
        _model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
    return _model


def _convert_to_wav_16k_mono(audio_path: Path, out_path: Path) -> None:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(audio_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "wav",
                str(out_path),
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise AudioConversionError(
            "ffmpeg is not installed or not on PATH — it is required to decode audio uploads."
        ) from exc

    if result.returncode != 0:
        stderr_tail = result.stderr.strip()[-500:]
        raise AudioConversionError(f"ffmpeg failed to convert '{audio_path.name}': {stderr_tail}")


def _wav_duration_sec(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def transcribe(audio_path: str | Path, language_hint: str | None = None) -> dict:
    """Transcribe one audio file.

    Args:
        audio_path: path to a .m4a, .mp3, .wav, or .ogg file.
        language_hint: optional ISO language code (e.g. "hi"). When given,
            it overrides auto-detection entirely — faster-whisper skips
            detection and transcribes directly in that language.

    Returns:
        {"text": str, "detected_language": str, "confidence": float,
         "duration_sec": float}
        confidence is faster-whisper's language-probability for the
        returned language (1.0 when language_hint was supplied, since
        detection was skipped in favor of the caller's certainty).

    Raises:
        FileNotFoundError: audio_path does not exist.
        UnsupportedAudioFormatError: extension isn't one of
            SUPPORTED_EXTENSIONS.
        AudioConversionError: ffmpeg is missing or failed to decode the file.
        AudioDurationError: clip is under MIN_DURATION_SEC or over
            MAX_DURATION_SEC.
    """
    audio_path = Path(audio_path)

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    ext = audio_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedAudioFormatError(
            f"Unsupported audio format '{ext}'. Accepted formats: "
            f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}."
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        wav_path = Path(tmp_dir) / "converted.wav"
        _convert_to_wav_16k_mono(audio_path, wav_path)

        duration_sec = _wav_duration_sec(wav_path)
        if duration_sec < MIN_DURATION_SEC:
            raise AudioDurationError(
                f"Clip is {duration_sec:.2f}s, shorter than the {MIN_DURATION_SEC}s minimum."
            )
        if duration_sec > MAX_DURATION_SEC:
            raise AudioDurationError(
                f"Clip is {duration_sec:.2f}s, longer than the {MAX_DURATION_SEC}s maximum."
            )

        model = _get_model()
        segments, info = model.transcribe(
            str(wav_path),
            language=language_hint,
            task="transcribe",
        )
        text = "".join(segment.text for segment in segments).strip()
        language = info.language

        if language_hint is None and language not in SPOKEN_LANGUAGES:
            # Pick the likeliest language we support from Whisper's own
            # scores (Urdu counting as Hindi) and transcribe again in it.
            scores: dict[str, float] = {}
            for code, prob in info.all_language_probs or [(language, 1.0)]:
                code = SAME_AS.get(code, code)
                scores[code] = scores.get(code, 0.0) + prob
            language = max(SPOKEN_LANGUAGES, key=lambda code: scores.get(code, 0.0))
            segments, _ = model.transcribe(str(wav_path), language=language, task="transcribe")
            text = "".join(segment.text for segment in segments).strip()

        return {
            "text": text,
            "detected_language": language,
            "confidence": info.language_probability,
            "duration_sec": round(duration_sec, 3),
        }
