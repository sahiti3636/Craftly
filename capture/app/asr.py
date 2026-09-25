"""Speech-to-text transcription for artisan audio uploads.

Transcript only. No field extraction (title, category, material, etc.)
happens here — that is a separate downstream step that will read
Listing.transcript_raw once this module has produced it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import wave
from pathlib import Path

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from faster_whisper.vad import VadOptions, get_speech_timestamps

log = logging.getLogger(__name__)

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
#: Below this language probability, detection defers to the language the
#: artisan chose in Studio (when it sent one).
UNSURE_BELOW = 0.5

#: "groq": Whisper large-v3 hosted by Groq (needs GROQ_API_KEY and the
#: network) — far better at Hindi than the local model: on the same clip the
#: local "small" model wrote "दूसो रुप्ये … पाज्खंटे" where large-v3 wrote
#: "200 रुपये … 5 घंटे", and it takes under a second. "local": faster-whisper
#: on this machine. Default: groq when a key is set, else local; a failed
#: Groq call falls back to local.
def _backend() -> str:
    # Read per call, not at import: GROQ_API_KEY arrives from .env when
    # app.groq_client is first imported, which may be after this module.
    import app.groq_client  # noqa: F401  (loads .env)

    return os.environ.get("CRAFTLY_ASR_BACKEND") or ("groq" if os.environ.get("GROQ_API_KEY") else "local")


GROQ_ASR_MODEL = os.environ.get("CRAFTLY_GROQ_ASR_MODEL", "whisper-large-v3")
#: Groq names the language ("Hindi"); the pipeline speaks codes.
_LANGUAGE_CODES = {
    "hindi": "hi", "english": "en", "telugu": "te", "tamil": "ta", "kannada": "kn", "urdu": "ur",
    "marathi": "mr", "bengali": "bn", "gujarati": "gu", "punjabi": "pa", "nepali": "ne",
}

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


def _scores(info) -> dict[str, float]:
    """Whisper's language probabilities, with Urdu counted as Hindi."""
    scores: dict[str, float] = {}
    for code, prob in info.all_language_probs or [(info.language, info.language_probability)]:
        code = SAME_AS.get(code, code)
        scores[code] = scores.get(code, 0.0) + prob
    return scores


def transcribe(
    audio_path: str | Path,
    language_hint: str | None = None,
    language_preference: str | None = None,
) -> dict:
    """Transcribe one audio file.

    Args:
        audio_path: path to a .m4a, .mp3, .wav, or .ogg file.
        language_hint: optional ISO language code (e.g. "hi"). When given,
            it overrides auto-detection entirely — faster-whisper skips
            detection and transcribes directly in that language.
        language_preference: the artisan's chosen language. Unlike a hint
            it does not override clear speech in another language; it is
            used when there is no speech at all, or detection is unsure.

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

        preferred = SAME_AS.get(language_preference or "", language_preference)
        if preferred not in SPOKEN_LANGUAGES:
            preferred = None

        text = language = confidence = None
        if _backend() == "groq":
            try:
                text, language, confidence = _transcribe_groq(wav_path, language_hint, preferred)
            except Exception as exc:  # noqa: BLE001 — no network, no key, rate limit: use the local model
                log.warning("Groq transcription failed, using the local model: %s", exc)
                text = None
        if text is None:
            text, language, confidence = _transcribe_local(wav_path, language_hint, preferred)
        return {
            "text": text,
            "detected_language": language,
            "confidence": confidence,
            "duration_sec": round(duration_sec, 3),
        }


def _has_speech(wav_path: Path) -> bool:
    """Silero VAD, as bundled with faster-whisper: no download, well under a second."""
    audio = decode_audio(str(wav_path), sampling_rate=16000)
    return bool(get_speech_timestamps(audio, VadOptions()))


def _transcribe_groq(
    wav_path: Path, language_hint: str | None, preferred: str | None
) -> tuple[str, str, float | None]:
    # Whisper invents words on silence ("Thank you.") hosted or not, so
    # silence is caught here, before the call, like vad_filter does locally.
    if not _has_speech(wav_path):
        return "", language_hint or preferred or "en", None

    from app.groq_client import get_client

    client = get_client()
    audio = wav_path.read_bytes()

    def ask(language: str | None) -> tuple[str, str | None]:
        extra = {"language": language} if language else {}
        reply = client.audio.transcriptions.create(
            file=("voice.wav", audio), model=GROQ_ASR_MODEL, response_format="verbose_json", **extra
        )
        name = str(getattr(reply, "language", "") or "").lower()
        return (reply.text or "").strip(), _LANGUAGE_CODES.get(name, language)

    text, detected = ask(language_hint)
    if language_hint:
        return text, language_hint, None
    language = SAME_AS.get(detected, detected)
    if language != detected or language not in SPOKEN_LANGUAGES:
        # Urdu heard means Urdu script; anything else we do not support
        # means her own language. Either way, ask again in that language.
        if language not in SPOKEN_LANGUAGES:
            language = preferred or "en"
        text, _ = ask(language)
    return text, language, None  # Groq reports no language probability


def _transcribe_local(
    wav_path: Path, language_hint: str | None, preferred: str | None
) -> tuple[str, str, float]:
    model = _get_model()
    # vad_filter drops silence before decoding. Without it Whisper
    # "hears" words in a silent or noisy clip ("You", "Thank you") and
    # labels them English, and the artisan's readback comes out in
    # English.
    segments, info = model.transcribe(
        str(wav_path),
        language=language_hint,
        task="transcribe",
        vad_filter=True,
    )
    text = "".join(segment.text for segment in segments).strip()

    if not text:
        # Nothing was said: there is no language to detect.
        language = language_hint or preferred or "en"
    elif language_hint is None:
        language = SAME_AS.get(info.language, info.language)
        unsure = info.language_probability < UNSURE_BELOW
        renamed = language != info.language  # "ur" heard: the text is in Urdu script
        if renamed or language not in SPOKEN_LANGUAGES or (unsure and preferred and preferred != language):
            # Her own language when detection is unsure, else the
            # likeliest language we support from Whisper's scores
            # (Urdu counting as Hindi). Then transcribe again in it.
            if unsure and preferred:
                language = preferred
            elif language not in SPOKEN_LANGUAGES:
                scores = _scores(info)
                language = max(SPOKEN_LANGUAGES, key=lambda code: scores.get(code, 0.0))
            segments, _ = model.transcribe(str(wav_path), language=language, task="transcribe", vad_filter=True)
            text = "".join(segment.text for segment in segments).strip()
    else:
        language = info.language

    # How sure Whisper was of the language actually used, not of its first guess.
    confidence = info.language_probability if language == info.language else _scores(info).get(language, 0.0)
    return text, language, round(confidence, 4)
