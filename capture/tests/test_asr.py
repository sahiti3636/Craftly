import subprocess

import pytest

from app.asr import (
    AudioDurationError,
    UnsupportedAudioFormatError,
    transcribe,
)


def test_rejects_unsupported_format(tmp_path):
    bad_file = tmp_path / "clip.flac"
    bad_file.write_bytes(b"not real audio")
    with pytest.raises(UnsupportedAudioFormatError):
        transcribe(bad_file)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        transcribe(tmp_path / "nope.wav")


def test_rejects_clip_under_minimum_duration(tone_wav_factory):
    clip = tone_wav_factory(0.5)
    with pytest.raises(AudioDurationError):
        transcribe(clip)


def test_rejects_clip_over_maximum_duration(tone_wav_factory):
    clip = tone_wav_factory(91.0)
    with pytest.raises(AudioDurationError):
        transcribe(clip)


def test_accepts_clip_just_inside_bounds(tone_wav_factory):
    # 1.5s is the floor and should pass duration validation, even though
    # a pure tone has no real speech content to transcribe.
    clip = tone_wav_factory(1.6)
    result = transcribe(clip, language_hint="en")
    assert result["duration_sec"] >= 1.5


def test_transcribe_returns_expected_shape(tone_wav_factory):
    clip = tone_wav_factory(2.0)
    result = transcribe(clip, language_hint="en")
    assert set(result.keys()) == {"text", "detected_language", "confidence", "duration_sec"}
    assert isinstance(result["text"], str)
    assert result["detected_language"] == "en"
    assert result["confidence"] == 1.0
    assert 1.5 <= result["duration_sec"] <= 90.0


def test_language_hint_overrides_detection(tone_wav_factory):
    clip = tone_wav_factory(2.0)
    result = transcribe(clip, language_hint="hi")
    assert result["detected_language"] == "hi"
    # Confidence is 1.0 whenever detection was skipped in favor of the hint.
    assert result["confidence"] == 1.0


def test_accepts_mp3(tone_wav_factory, tmp_path):
    wav_clip = tone_wav_factory(2.0)
    mp3_clip = tmp_path / "clip.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_clip), str(mp3_clip)],
        check=True,
        capture_output=True,
    )
    result = transcribe(mp3_clip, language_hint="en")
    assert result["duration_sec"] >= 1.5


def test_accepts_ogg(tone_wav_factory, tmp_path):
    wav_clip = tone_wav_factory(2.0)
    ogg_clip = tmp_path / "clip.ogg"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_clip), str(ogg_clip)],
        check=True,
        capture_output=True,
    )
    result = transcribe(ogg_clip, language_hint="en")
    assert result["duration_sec"] >= 1.5


def test_accepts_m4a(tone_wav_factory, tmp_path):
    wav_clip = tone_wav_factory(2.0)
    m4a_clip = tmp_path / "clip.m4a"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_clip), str(m4a_clip)],
        check=True,
        capture_output=True,
    )
    result = transcribe(m4a_clip, language_hint="en")
    assert result["duration_sec"] >= 1.5


def test_speech_detected_as_urdu_is_transcribed_as_hindi(tmp_path, monkeypatch):
    """Whisper often labels spoken Hindi "ur" and writes Urdu script, which
    nothing downstream reads. With no hint, it is transcribed again as Hindi."""
    from types import SimpleNamespace

    import app.asr as asr_module
    from tests.conftest import make_tone_wav

    calls = []

    class FakeModel:
        def transcribe(self, path, language=None, task=None):
            calls.append(language)
            text = "दो सौ रुपये" if language == "hi" else "دو سو روپے"
            return [SimpleNamespace(text=text)], SimpleNamespace(
                language="ur", language_probability=0.6, all_language_probs=[("ur", 0.6), ("hi", 0.3), ("en", 0.1)]
            )

    monkeypatch.setattr(asr_module, "_get_model", lambda: FakeModel())
    clip = make_tone_wav(tmp_path / "clip.wav", duration_sec=2.0)
    result = asr_module.transcribe(clip)
    assert calls == [None, "hi"]
    assert result["detected_language"] == "hi"
    assert result["text"] == "दो सौ रुपये"


def test_an_explicit_hint_is_never_second_guessed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import app.asr as asr_module
    from tests.conftest import make_tone_wav

    calls = []

    class FakeModel:
        def transcribe(self, path, language=None, task=None):
            calls.append(language)
            return [SimpleNamespace(text="x")], SimpleNamespace(language=language, language_probability=1.0)

    monkeypatch.setattr(asr_module, "_get_model", lambda: FakeModel())
    asr_module.transcribe(make_tone_wav(tmp_path / "clip.wav", duration_sec=2.0), language_hint="ur")
    assert calls == ["ur"]


def test_detection_only_picks_a_language_artisans_speak(tmp_path, monkeypatch):
    """A muffled English clip once came back as Norwegian ("nn")."""
    from types import SimpleNamespace

    import app.asr as asr_module
    from tests.conftest import make_tone_wav

    calls = []

    class FakeModel:
        def transcribe(self, path, language=None, task=None):
            calls.append(language)
            info = SimpleNamespace(language="nn", language_probability=0.4,
                                   all_language_probs=[("nn", 0.4), ("en", 0.35), ("no", 0.2), ("hi", 0.05)])
            return [SimpleNamespace(text="material cost two hundred rupees")], info

    monkeypatch.setattr(asr_module, "_get_model", lambda: FakeModel())
    result = asr_module.transcribe(make_tone_wav(tmp_path / "clip.wav", duration_sec=2.0))
    assert calls == [None, "en"]
    assert result["detected_language"] == "en"
