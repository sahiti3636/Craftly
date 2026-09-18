"""Reel captions, frame composition, and the ffmpeg fallback.

These run with or without ffmpeg installed, which is the point: the
storyboard path is not a consolation prize, it is the contract.
"""

from __future__ import annotations

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


def test_poster_is_a_single_shareable_frame(tmp_path):
    card = deps.card(GOOD, Channel.OWN_STORE)
    path = reel.poster(card, out_dir=tmp_path)
    assert path is not None and path.exists()


def test_reel_page_renders_something_either_way(client):
    response = client.get(f"/reel/{GOOD}")
    assert response.status_code == 200
    assert "The script" in response.text
    assert ("<video" in response.text) or ("Storyboard" in response.text)
