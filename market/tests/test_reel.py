"""Reel captions, frame composition, and the ffmpeg fallback.

These run with or without ffmpeg installed, which is the point: the
storyboard path is not a consolation prize, it is the contract.
"""

from __future__ import annotations

import subprocess

import pytest
from PIL import Image, ImageDraw

from app import deps, reel
from app.adapters import registry
from app.captions import REEL_SECONDS, script
from app.contracts import Channel

GOOD = "lst_51ea90cb7d24"
NO_HOURS = "lst_77b0c4d8e913"


def test_script_is_exactly_fifteen_seconds():
    card = deps.card(GOOD, Channel.OWN_STORE)
    beats = script(card)
    assert abs(sum(b.duration for b in beats) - REEL_SECONDS) < 0.01
    assert beats[0].start == 0.0
    for previous, following in zip(beats, beats[1:]):
        assert abs(previous.end - following.start) < 0.01


def test_script_never_invents_hours_it_does_not_have():
    """The whole pitch is that the provenance is real."""
    card = deps.card(NO_HOURS, Channel.OWN_STORE)
    beats = script(card)
    text = " ".join(filter(None, [b.headline for b in beats] + [b.sub for b in beats]))
    assert "hours" not in text.lower()
    assert "by hand" in text.lower()


def test_script_carries_the_price_and_the_share():
    card = deps.card(GOOD, Channel.OWN_STORE)
    beats = script(card)
    text = " ".join(filter(None, [b.headline for b in beats] + [b.sub for b in beats]))
    assert str(card.quote.price_inr)[:2] in text.replace(",", "")
    assert card.artisan_name.split()[0] in text


def test_script_includes_the_passport_code():
    card = deps.card(GOOD, Channel.OWN_STORE)
    beats = script(card, verification_code="CR-TEST-CODE")
    assert any("CR-TEST-CODE" in (b.sub or "") for b in beats)


def test_hindi_script_is_in_devanagari():
    card = deps.card(GOOD, Channel.OWN_STORE)
    beats = script(card, lang="hi")
    joined = " ".join(filter(None, [b.sub for b in beats] + [b.kicker for b in beats]))
    assert any("ऀ" <= ch <= "ॿ" for ch in joined)


def test_frames_are_vertical_and_full_size(tmp_path):
    card = deps.card(GOOD, Channel.OWN_STORE)
    beats = script(card)
    frame = reel.compose_frame(beats[0], photo=None, lang="en")
    assert frame.size == (reel.WIDTH, reel.HEIGHT)
    assert reel.HEIGHT > reel.WIDTH


def test_build_produces_a_reel_or_a_storyboard(tmp_path):
    card = deps.card(GOOD, Channel.OWN_STORE)
    passport = registry.passports().by_listing(GOOD)
    result = reel.build(card, passport=passport, out_dir=tmp_path)

    assert result.captions
    if reel.ffmpeg_available():
        assert result.is_video
        assert result.video_path.exists()
        assert result.video_path.stat().st_size > 10_000
    else:
        assert not result.is_video
        assert len(result.frame_paths) == len(result.captions)
        assert all(p.exists() for p in result.frame_paths)
        assert any("ffmpeg" in w for w in result.warnings)


def test_ffmpeg_command_has_one_input_per_frame_and_concatenates(tmp_path):
    frames = [(tmp_path / f"f{i}.png", 3.75) for i in range(4)]
    command = reel.build_ffmpeg_command(frames, tmp_path / "out.mp4")
    assert command[0].endswith("ffmpeg") or command[0] == "ffmpeg"
    assert command.count("-loop") == 4
    filters = command[command.index("-filter_complex") + 1]
    assert "concat=n=4:v=1:a=0" in filters
    assert "zoompan" in filters
    assert str(tmp_path / "out.mp4") == command[-1]


def test_ffmpeg_command_splices_in_a_making_clip(tmp_path):
    frames = [(tmp_path / f"f{i}.png", 3.75) for i in range(4)]
    clip = tmp_path / "making.mp4"
    command = reel.build_ffmpeg_command(
        frames, tmp_path / "out.mp4", making_clip=clip, making_index=1
    )
    filters = command[command.index("-filter_complex") + 1]
    assert str(clip) in command
    assert "overlay" in filters
    assert "concat=n=4:v=1:a=0" in filters


def test_hindi_text_is_drawn_shaped_when_the_machine_can(tmp_path):
    """The ि matra is drawn before its consonant, so shaped "कि" and the
    unshaped glyph-by-glyph "कि" put their ink in different places."""
    if not reel.can_render_hindi() or reel.pillow_shapes_text():
        pytest.skip("needs ffmpeg 6.1+ doing the shaping for Pillow")
    font = reel.find_font("hi")

    def ink(shaped: bool):
        frame = Image.new("RGB", (reel.WIDTH, reel.HEIGHT))
        draw = ImageDraw.Draw(frame)
        if shaped:
            text = reel._Text(frame, draw)
            text((100, 100), "कि", font, 120, (255, 255, 255))
            text.flush()
        else:
            draw.text((100, 100), "कि", font=reel._load(font, 120), fill=(255, 255, 255))
        return frame.getbbox()

    assert ink(shaped=True) is not None
    assert ink(shaped=True) != ink(shaped=False)


def test_hindi_reel_falls_back_to_english_rather_than_misdrawn(tmp_path, monkeypatch):
    monkeypatch.setattr(reel, "can_render_hindi", lambda: False)
    card = deps.card(GOOD, Channel.OWN_STORE)
    result = reel.build(card, lang="hi", out_dir=tmp_path)
    assert result.lang == "en"
    assert any("shape Devanagari" in w for w in result.warnings)


def test_hindi_reel_falls_back_to_english_if_ffmpeg_cannot_draw_it(tmp_path, monkeypatch):
    def fail(self):
        if self._queued:
            raise reel.ReelError("ffmpeg could not draw the Hindi captions.")

    monkeypatch.setattr(reel, "can_render_hindi", lambda: True)
    monkeypatch.setattr(reel, "pillow_shapes_text", lambda: False)
    monkeypatch.setattr(reel._Text, "flush", fail)
    card = deps.card(GOOD, Channel.OWN_STORE)
    result = reel.build(card, lang="hi", out_dir=tmp_path)
    assert result.lang == "en"
    assert all("_en_" in p.name for p in result.frame_paths)
    assert any("English instead" in w for w in result.warnings)


def test_every_beat_has_a_line_to_speak_and_it_reads_well_aloud():
    card = deps.card(GOOD, Channel.OWN_STORE)
    for lang in ("en", "hi"):
        beats = script(card, lang=lang, verification_code="CR-TEST-CODE")
        assert all(b.spoken for b in beats)
        spoken = " ".join(b.spoken for b in beats)
        assert "CR-TEST-CODE" not in spoken  # nobody wants a code read out
        assert "₹" not in spoken  # said as rupees, not left to the voice


def test_voiceover_never_invents_hours_either():
    card = deps.card(NO_HOURS, Channel.OWN_STORE)
    spoken = " ".join(b.spoken for b in script(card))
    assert "hours" not in spoken.lower()
    assert "by hand" in spoken.lower()


def test_ffmpeg_command_lays_the_narration_under_each_beat(tmp_path):
    frames = [(tmp_path / f"f{i}.png", 3.75) for i in range(4)]
    clips = [tmp_path / "a0.wav", None, tmp_path / "a2.wav", tmp_path / "a3.wav"]
    command = reel.build_ffmpeg_command(frames, tmp_path / "out.mp4", narration=clips)
    filters = command[command.index("-filter_complex") + 1]
    assert command.count("-i") == 4 + 3
    assert "anullsrc" in filters  # the beat with no line is silence, not a gap
    assert "concat=n=4:v=0:a=1[aout]" in filters
    assert "[aout]" in command


def test_ffmpeg_command_is_silent_without_narration(tmp_path):
    frames = [(tmp_path / f"f{i}.png", 3.75) for i in range(4)]
    command = reel.build_ffmpeg_command(frames, tmp_path / "out.mp4")
    assert "[aout]" not in command
    assert "-c:a" not in command


def _fake_voice(seconds: float):
    """A tone instead of Google's voice, so the test needs no network."""

    def synthesize(text, lang, out_path):
        subprocess.run(
            [reel.config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={seconds}", str(out_path)],
            check=True,
        )

    return synthesize


def _has_audio(path) -> bool:
    probe = subprocess.run(
        [reel.config.FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True
    )
    return "Audio:" in probe.stderr


def test_voiced_reel_has_a_soundtrack_and_stretches_to_fit_it(tmp_path, monkeypatch):
    if not reel.ffmpeg_available():
        pytest.skip("needs ffmpeg")
    monkeypatch.setattr(reel.config, "REEL_VOICE", "gtts")
    monkeypatch.setattr(reel.voice, "synthesize", _fake_voice(6.0))
    card = deps.card(GOOD, Channel.OWN_STORE)
    result = reel.build(card, out_dir=tmp_path)

    assert result.is_video, result.warnings
    assert result.video_path.name.endswith("_voice.mp4")
    assert _has_audio(result.video_path)
    # 6s of speech at 1.15x does not fit a 3.5s beat, so every beat grows.
    assert all(c.duration > 5 for c in result.captions)
    assert sum(c.duration for c in result.captions) > REEL_SECONDS

    again = reel.build(card, out_dir=tmp_path)  # served from disk, same timing
    assert again.video_path == result.video_path
    assert [c.duration for c in again.captions] == [c.duration for c in result.captions]


def test_reel_is_built_silent_when_the_voice_fails(tmp_path, monkeypatch):
    if not reel.ffmpeg_available():
        pytest.skip("needs ffmpeg")

    def offline(text, lang, out_path):
        raise reel.voice.VoiceError("The voiceover could not be generated (offline).")

    monkeypatch.setattr(reel.config, "REEL_VOICE", "gtts")
    monkeypatch.setattr(reel.voice, "synthesize", offline)
    card = deps.card(GOOD, Channel.OWN_STORE)
    result = reel.build(card, out_dir=tmp_path)

    assert result.is_video
    assert not result.video_path.name.endswith("_voice.mp4")
    assert not _has_audio(result.video_path)
    assert any("retry the voiceover" in w for w in result.warnings)
    assert abs(sum(c.duration for c in result.captions) - REEL_SECONDS) < 0.01


def test_poster_is_a_single_shareable_frame(tmp_path):
    card = deps.card(GOOD, Channel.OWN_STORE)
    path = reel.poster(card, out_dir=tmp_path)
    assert path is not None and path.exists()


def test_reel_page_renders_something_either_way(client):
    response = client.get(f"/reel/{GOOD}")
    assert response.status_code == 200
    assert "The script" in response.text
    assert ("<video" in response.text) or ("Storyboard" in response.text)
