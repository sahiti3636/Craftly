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
