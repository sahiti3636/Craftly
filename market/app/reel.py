"""Fifteen-second vertical reels, built from one photo and a caption script.

The artisan has a photo and possibly a ten-second clip of her hands
working. What she does not have is video editing software, a designer, or
English. This module turns what she does have into the format that
actually moves craft today — 1080x1920, silent-readable, four beats —
without her doing anything at all.

How it is put together, and why in that order:

1. `app/captions.py` produces the argument as data.
2. PIL composes each beat into a full 1080x1920 frame: the product photo
   blurred to make a background, the photo itself placed clean on top,
   the caption set underneath.
3. ffmpeg turns the frames into video, giving each still a slow push so it
   does not read as a slideshow, and splicing in the making-clip for the
   "how it was made" beat when one exists.

**The storyboard fallback is the important part of this file.** Step 3
needs ffmpeg, which is not on every machine, and a demo that dies on a
laptop without it would be a bad trade for a nicer transition. So step 2
is complete on its own: with no ffmpeg at all, `build()` returns the same
frames as PNGs and the surface shows the reel as a storyboard. Degrade,
don't crash — the same contract A2's pipeline holds itself to.

A voiceover reads one short line per beat, in the reel's language
(`app/voice.py`, gTTS). The captions still carry everything, so a reel
watched on mute loses nothing. A beat whose line runs longer than its
caption slot is lengthened rather than cut off, so a voiced reel can run
a few seconds past fifteen. When the voice cannot be made — offline,
say — the reel is built silent with a warning, never not at all.

No music. A reel with an unlicensed track is a takedown waiting to
happen on exactly the marketplaces this project is trying to reach, and
picking a licensed bed is a decision for whoever owns distribution, not
a default buried in a renderer.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

from app import config, voice
from app.captions import CaptionCard, REEL_SECONDS, script
from app.contracts import Passport
from app.view import ProductCard

WIDTH, HEIGHT = 1080, 1920
FPS = 30
MARGIN = 72

INK = (246, 241, 231)
INK_DIM = (246, 241, 231, 205)
ACCENT = (226, 112, 58)
BACKDROP = (27, 26, 23)

#: Fonts that can set Devanagari, in order of preference. A reel captioned
#: in Hindi that renders as tofu boxes is worse than one captioned in
#: English, so `find_font` reports what it found and `build()` falls back
#: to English rather than shipping squares.
_DEVANAGARI_FONTS = (
    "C:/Windows/Fonts/Nirmala.ttc",
    "C:/Windows/Fonts/mangal.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
    "/System/Library/Fonts/Supplemental/Kohinoor.ttc",
)
_DEVANAGARI_BOLD = (
    "C:/Windows/Fonts/NirmalaB.ttf",
    "C:/Windows/Fonts/Nirmala.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Kohinoor.ttc",
)
_LATIN_FONTS = (
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
_LATIN_BOLD = (
    "C:/Windows/Fonts/seguisb.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)


class ReelError(RuntimeError):
    """Rendering failed in a way the caller should show, not swallow."""


@dataclass
class ReelResult:
    listing_id: str
    lang: str
    video_path: Path | None = None
    frame_paths: list[Path] = field(default_factory=list)
    captions: list[CaptionCard] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_video(self) -> bool:
        return self.video_path is not None

    @property
    def video_url(self) -> str | None:
        return f"/reels/{self.video_path.name}" if self.video_path else None

    @property
    def frame_urls(self) -> list[str]:
        return [f"/reels/{p.name}" for p in self.frame_paths]


# ---------------------------------------------------------------------------
# fonts and text
# ---------------------------------------------------------------------------


def _first_existing(paths: tuple[str, ...]) -> Path | None:
    for candidate in paths:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def find_font(lang: str, *, bold: bool = False) -> Path | None:
    if lang == "hi":
        return _first_existing(_DEVANAGARI_BOLD if bold else _DEVANAGARI_FONTS)
    return _first_existing(_LATIN_BOLD if bold else _LATIN_FONTS)


@lru_cache(maxsize=64)
def _load(path: Path | None, size: int) -> ImageFont.FreeTypeFont:
    if path is None:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default()


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# scripts that need shaping
# ---------------------------------------------------------------------------
#
# Devanagari is not drawn one character after another: the ि matra is typed
# after its consonant but drawn before it, and consonant clusters join into
# conjuncts. Pillow only does that with libraqm, which its Windows wheels
# cannot load out of the box — without it "हस्तनिर्मित" comes out as
# "हस्तनरि्मति", which any Hindi reader sees at once. FFmpeg's drawtext
# shapes with HarfBuzz, so when Pillow cannot, the Indic lines of a frame
# are drawn by ffmpeg and pasted onto the frame Pillow composed.


def _needs_shaping(text: str) -> bool:
    """True for text in an Indic script (Devanagari through Sinhala)."""
    return any("ऀ" <= ch <= "෿" for ch in text)


@lru_cache(maxsize=1)
def pillow_shapes_text() -> bool:
    return bool(features.check("raqm"))


@lru_cache(maxsize=8)
def _ffmpeg_shapes_text(binary: str) -> bool:
    """drawtext shapes with HarfBuzz from FFmpeg 6.1, the release that also
    added its `y_align` option. Older drawtext only reorders right-to-left
    text and would draw Devanagari as badly as Pillow does."""
    if shutil.which(binary) is None:
        return False
    try:
        completed = subprocess.run(
            [binary, "-hide_banner", "-h", "filter=drawtext"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "y_align" in completed.stdout


def can_render_hindi() -> bool:
    """Whether Hindi captions come out correctly shaped on this machine."""
    if find_font("hi") is None:
        return False
    return pillow_shapes_text() or _ffmpeg_shapes_text(config.FFMPEG)


def _filter_path(path: Path) -> str:
    """A file path as a quoted drawtext option. A Windows drive colon is an
    option separator to ffmpeg, so it is escaped."""
    text = path.resolve().as_posix().replace("'", r"'\''").replace(":", r"\:")
    return f"'{text}'"


class _Text:
    """Draws the text of one frame.

    Pillow draws what it can shape. Indic lines it cannot shape are queued
    and drawn by ffmpeg in one call at `flush()`, as white-on-black masks —
    one per colour — that fill the colour in over the composed frame.
    """

    def __init__(self, frame: Image.Image, draw: ImageDraw.ImageDraw) -> None:
        self.frame = frame
        self.draw = draw
        self._queued: list[tuple[tuple[int, int], str, Path, int, tuple]] = []

    def __call__(self, xy: tuple[int, int], text: str, font_path: Path | None, size: int, fill: tuple) -> None:
        if font_path is not None and _needs_shaping(text) and not pillow_shapes_text():
            self._queued.append((xy, text, font_path, size, fill))
        else:
            self.draw.text(xy, text, font=_load(font_path, size), fill=fill)

    def flush(self) -> None:
        if not self._queued:
            return
        by_colour: dict[tuple, list[tuple[tuple[int, int], str, Path, int]]] = {}
        for xy, text, font_path, size, fill in self._queued:
            by_colour.setdefault(fill, []).append((xy, text, font_path, size))

        with tempfile.TemporaryDirectory(prefix="craftly_text_") as tmp:
            workdir = Path(tmp)
            chains: list[str] = []
            outputs: list[str] = []
            for c, runs in enumerate(by_colour.values()):
                steps = []
                for r, ((x, y), text, font_path, size) in enumerate(runs):
                    # Text goes through a file so no caption can break the
                    # filter syntax; expansion=none keeps a literal "%".
                    name = f"t{c}_{r}.txt"
                    (workdir / name).write_text(text, encoding="utf-8")
                    steps.append(
                        f"drawtext=fontfile={_filter_path(font_path)}:textfile={name}:"
                        f"fontsize={size}:fontcolor=white:x={x}:y={y}:y_align=font:expansion=none"
                    )
                chains.append(f"[s{c}]" + ",".join(steps) + f"[m{c}]")
                outputs += ["-map", f"[m{c}]", "-frames:v", "1", f"m{c}.png"]
            split = "".join(f"[s{c}]" for c in range(len(by_colour)))
            graph = f"[0:v]split={len(by_colour)}{split};" + ";".join(chains)
            command = [
                config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color=black:s={WIDTH}x{HEIGHT}:d=1,format=rgb24",
                "-filter_complex", graph, *outputs,
            ]
            try:
                completed = subprocess.run(
                    command, cwd=workdir, capture_output=True, text=True, timeout=60, check=False
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ReelError(f"ffmpeg could not draw the Hindi captions ({exc}).") from exc
            if completed.returncode != 0:
                tail = " / ".join((completed.stderr or "").strip().splitlines()[-3:])
                raise ReelError(f"ffmpeg could not draw the Hindi captions: {tail}")

            for c, fill in enumerate(by_colour):
                with Image.open(workdir / f"m{c}.png") as rendered:
                    mask = rendered.convert("L")
                if len(fill) == 4 and fill[3] < 255:
                    mask = mask.point(lambda v, a=fill[3]: v * a // 255)
                self.frame.paste(Image.new("RGB", self.frame.size, fill[:3]), (0, 0), mask)
        self._queued.clear()


# ---------------------------------------------------------------------------
# frame composition (Pillow; ffmpeg only for text Pillow cannot shape)
# ---------------------------------------------------------------------------


def local_media_path(url: str | None) -> Path | None:
    """Resolve a media URL to a file on disk, if it is one of ours.

    Handles the `/media/...` URLs the seed catalogue uses. A remote URL
    from B2's real storage returns None and the caller falls back to a
    plain background — downloading someone's product photo on the request
    path to render a reel is a decision for whoever runs the media store,
    not something to sneak in here.
    """
    if not url:
        return None
    if url.startswith("/media/"):
        candidate = Path(config.MEDIA_DIR) / Path(url).name
        return candidate if candidate.exists() else None
    path = Path(url)
    return path if path.exists() else None


def _background(photo: Image.Image | None) -> Image.Image:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), BACKDROP)
    if photo is None:
        return canvas

    filled = _cover(photo, WIDTH, HEIGHT).filter(ImageFilter.GaussianBlur(48))
    canvas.paste(filled, (0, 0))
    shade = Image.new("RGBA", (WIDTH, HEIGHT), (17, 16, 14, 150))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), shade).convert("RGB")
    return canvas


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Scale to fill, crop the overflow, centred."""
    ratio = max(width / image.width, height / image.height)
    resized = image.resize(
        (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
        Image.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _contain(image: Image.Image, width: int, height: int) -> Image.Image:
    ratio = min(width / image.width, height / image.height)
    return image.resize(
        (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
        Image.LANCZOS,
    )


def compose_frame(
    caption: CaptionCard,
    *,
    photo: Image.Image | None,
    lang: str,
    qr: Image.Image | None = None,
) -> Image.Image:
    """One finished 1080x1920 frame: background, product, caption."""
    frame = _background(photo)
    draw = ImageDraw.Draw(frame, "RGBA")

    is_close = caption.style == "close"
    art_box = (WIDTH - 2 * MARGIN, 980)

    if is_close and qr is not None:
        card_w = 560
        card = Image.new("RGB", (card_w, card_w), "white")
        card.paste(_contain(qr, card_w - 60, card_w - 60), (30, 30))
        frame.paste(card, ((WIDTH - card_w) // 2, 430))
    elif photo is not None:
        art = _contain(photo, *art_box)
        x = (WIDTH - art.width) // 2
        y = 300 + (art_box[1] - art.height) // 2
        shadow = Image.new("RGBA", (art.width + 40, art.height + 40), (0, 0, 0, 90))
        shadow = shadow.filter(ImageFilter.GaussianBlur(20))
        frame.paste(shadow, (x - 20, y - 10), shadow)
        frame.paste(art, (x, y))

    text = _Text(frame, draw)
    _draw_wordmark(text, lang)
    _draw_caption(frame, draw, text, caption, lang)
    text.flush()
    return frame


def _scrim(frame: Image.Image, top: int) -> None:
    """Fade the bottom of the frame to near-black behind the caption.

    A flat slab would also make the text readable, but it cuts the picture
    in half with a hard line. The gradient keeps the photo and the words
    in one image, which is what makes a reel look made rather than
    generated.
    """
    top = max(0, min(top, HEIGHT - 1))
    height = HEIGHT - top
    column = Image.new("L", (1, height))
    for y in range(height):
        # Ease in, so the fade starts gently and is solid under the text.
        column.putpixel((0, y), int(225 * (y / height) ** 0.65))
    mask = column.resize((WIDTH, height))
    frame.paste(Image.new("RGB", (WIDTH, height), (16, 15, 13)), (0, top), mask)


def _draw_wordmark(text: _Text, lang: str) -> None:
    text((MARGIN, 72), "CRAFTLY", find_font("en", bold=True), 34, INK)
    text(
        (MARGIN, 118),
        "handmade, traceable, fairly priced" if lang == "en" else "हस्तनिर्मित, प्रमाणित, न्यायसंगत",
        find_font(lang),
        26,
        INK_DIM,
    )


def _draw_caption(
    frame: Image.Image, draw: ImageDraw.ImageDraw, text: _Text, caption: CaptionCard, lang: str
) -> None:
    max_width = WIDTH - 2 * MARGIN
    sizes = {"hook": 82, "close": 88, "fact": 72}
    headline_size = sizes.get(caption.style, 72)
    headline_path = find_font(lang, bold=True)
    body_path = find_font(lang)

    # Wrapping measures unshaped widths. Shaping mostly joins Devanagari
    # glyphs into narrower conjuncts, so a shaped line still fits.
    blocks: list[tuple[list[str], Path | None, int, int, tuple]] = []
    if caption.kicker:
        lines = _wrap(draw, caption.kicker, _load(body_path, 32), max_width)
        blocks.append((lines, body_path, 32, 44, ACCENT))
    lines = _wrap(draw, caption.headline, _load(headline_path, headline_size), max_width)
    blocks.append((lines, headline_path, headline_size, int(headline_size * 1.18), INK))
    if caption.sub:
        lines = _wrap(draw, caption.sub, _load(body_path, 40), max_width)
        blocks.append((lines, body_path, 40, 54, INK_DIM))

    height = sum(len(lines) * step for lines, _, _, step, _ in blocks) + 40 * (len(blocks) - 1)
    y = HEIGHT - 200 - height

    # The fade starts well above the text so a pale photo cannot swallow it.
    _scrim(frame, y - 260)

    for lines, font_path, size, step, colour in blocks:
        for line in lines:
            text((MARGIN, y), line, font_path, size, colour)
            y += step
        y += 40


# ---------------------------------------------------------------------------
# video assembly (ffmpeg)
# ---------------------------------------------------------------------------


def ffmpeg_available() -> bool:
    return shutil.which(config.FFMPEG) is not None


def build_ffmpeg_command(
    frames: list[tuple[Path, float]],
    output: Path,
    *,
    making_clip: Path | None = None,
    making_index: int | None = None,
    narration: list[Path | None] | None = None,
    ffmpeg: str | None = None,
) -> list[str]:
    """Assemble the ffmpeg invocation. Pure — returns args, runs nothing.

    Each still becomes a segment with a slow push (`zoompan`), so a
    four-photo reel reads as motion rather than as a slide deck. When a
    making-clip exists it replaces the background of one beat and the
    caption is overlaid on top of the moving footage.

    `narration`, one clip (or None) per frame, becomes the soundtrack:
    each clip starts just after its beat does and is padded to the beat's
    length, so the voice stays on the caption it is reading.
    """
    binary = ffmpeg or config.FFMPEG
    args: list[str] = [binary, "-y"]
    filters: list[str] = []
    labels: list[str] = []
    input_index = 0
    clip_input: int | None = None

    for i, (path, duration) in enumerate(frames):
        use_clip = making_clip is not None and i == making_index
        if use_clip:
            if clip_input is None:
                args += ["-i", str(making_clip)]
                clip_input = input_index
                input_index += 1
            args += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(path)]
            overlay_input = input_index
            input_index += 1
            filters.append(
                f"[{clip_input}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={WIDTH}:{HEIGHT},fps={FPS},trim=duration={duration:.3f},"
                f"setpts=PTS-STARTPTS[clip{i}]"
            )
            filters.append(
                f"[clip{i}][{overlay_input}:v]overlay=0:0:format=auto,"
                f"format=yuv420p,setsar=1[v{i}]"
            )
        else:
            args += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(path)]
            input_index += 1
            frames_count = max(2, int(duration * FPS))
            # zoompan emits `d` frames per *input* frame, and -loop feeds it a
            # frame every tick — untrimmed, a 4s still became ~100x too many frames.
            # Feed it exactly one.
            direction = "min(zoom+0.0007,1.12)" if i % 2 == 0 else "if(lte(zoom,1.0),1.12,max(1.001,zoom-0.0007))"
            filters.append(
                f"[{input_index - 1}:v]trim=end_frame=1,scale={(WIDTH * 6 // 5) // 2 * 2}:{(HEIGHT * 6 // 5) // 2 * 2},"
                f"zoompan=z='{direction}':d={frames_count}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={WIDTH}x{HEIGHT}:fps={FPS},"
                f"format=yuv420p,setsar=1[v{i}]"
            )
        labels.append(f"[v{i}]")

    filters.append(f"{''.join(labels)}concat=n={len(frames)}:v=1:a=0[out]")

    audio_map: list[str] = []
    if narration and any(narration):
        audio_labels: list[str] = []
        pcm = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
        for i, ((_, duration), clip) in enumerate(zip(frames, narration)):
            if clip is None:
                filters.append(f"anullsrc=r=44100:cl=stereo,atrim=end={duration:.3f},{pcm}[a{i}]")
            else:
                args += ["-i", str(clip)]
                filters.append(
                    f"[{input_index}:a]{pcm},adelay=delays={int(VOICE_LEAD * 1000)}:all=1,"
                    f"apad=whole_dur={duration:.3f},atrim=end={duration:.3f},"
                    f"asetpts=PTS-STARTPTS[a{i}]"
                )
                input_index += 1
            audio_labels.append(f"[a{i}]")
        filters.append(f"{''.join(audio_labels)}concat=n={len(frames)}:v=0:a=1[aout]")
        audio_map = ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]

    args += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        *audio_map,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-r",
        str(FPS),
        str(output),
    ]
    return args


# ---------------------------------------------------------------------------
# voiceover
# ---------------------------------------------------------------------------

#: Seconds of quiet before each beat's line starts, and after it ends.
VOICE_LEAD = 0.3
VOICE_TAIL = 0.4
#: gTTS speaks slowly for a reel; 1.15x still sounds natural.
VOICE_TEMPO = 1.15


def _audio_seconds(path: Path) -> float | None:
    completed = subprocess.run(
        [config.FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    found = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr or "")
    if not found:
        return None
    hours, minutes, seconds = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _narrate(captions: list[CaptionCard], lang: str, directory: Path, stem: str) -> list[Path | None]:
    """One clean clip per beat that has a spoken line: synthesised, the
    silence gTTS leaves at either end trimmed, sped up a touch. Raises
    VoiceError when any line cannot be made — a reel with a voice on
    three beats of four sounds broken, so it is all or nothing."""
    trim = "silenceremove=start_periods=1:start_threshold=-50dB"
    clips: list[Path | None] = []
    for i, caption in enumerate(captions):
        if not caption.spoken:
            clips.append(None)
            continue
        raw = directory / f"{stem}_say{i}.mp3"
        clip = directory / f"{stem}_say{i}.wav"
        voice.synthesize(caption.spoken, lang, raw)
        try:
            completed = subprocess.run(
                [
                    config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                    "-af", f"{trim},areverse,{trim},areverse,atempo={VOICE_TEMPO}",
                    "-ar", "44100", "-ac", "2", str(clip),
                ],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise voice.VoiceError(f"The voiceover could not be processed ({exc}).") from exc
        finally:
            raw.unlink(missing_ok=True)
        if completed.returncode != 0 or not clip.exists():
            raise voice.VoiceError("The voiceover could not be processed by ffmpeg.")
        clips.append(clip)
    return clips


def _saved_narration(captions: list[CaptionCard], directory: Path, stem: str) -> list[Path | None]:
    return [
        path if caption.spoken and path.exists() else None
        for caption, path in ((c, directory / f"{stem}_say{i}.wav") for i, c in enumerate(captions))
    ]


def _fit_to_voice(captions: list[CaptionCard], clips: list[Path | None]) -> list[CaptionCard]:
    """Lengthen any beat its line does not fit in, and re-lay the starts.

    The caption script is fifteen seconds; spoken, a long product title
    can need more. Cutting the voice off mid-word is worse than a reel a
    few seconds longer, so the beat grows to fit the line.
    """
    timed: list[CaptionCard] = []
    at = 0.0
    for caption, clip in zip(captions, clips):
        spoken = _audio_seconds(clip) if clip is not None else None
        needed = VOICE_LEAD + spoken + VOICE_TAIL if spoken else 0.0
        duration = round(max(caption.duration, needed), 3)
        timed.append(replace(caption, start=round(at, 3), duration=duration))
        at += duration
    return timed


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------


def build(
    card: ProductCard,
    *,
    lang: str = "en",
    passport: Passport | None = None,
    out_dir: Path | None = None,
    force: bool = False,
) -> ReelResult:
    """Render a reel for one listing. Never raises on a missing toolchain.

    Returns a `ReelResult` that is either a video or a storyboard. The
    caller renders whichever it got and shows `warnings` — which is where
    "ffmpeg is not installed" and "no Devanagari font on this machine"
    end up, rather than in a stack trace nobody sees.
    """
    directory = Path(out_dir or config.REELS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    if lang == "hi" and find_font("hi") is None:
        warnings.append(
            "No Devanagari font on this machine, so the reel is captioned in "
            "English. Install Noto Sans Devanagari (or Nirmala UI on Windows) "
            "for Hindi captions."
        )
        lang = "en"
    elif lang == "hi" and not can_render_hindi():
        warnings.append(
            "Neither Pillow (no libraqm) nor ffmpeg (6.1 or newer needed) can "
            "shape Devanagari on this machine, so the reel is captioned in "
            "English rather than with misdrawn Hindi. Install ffmpeg 6.1+ for "
            "Hindi captions."
        )
        lang = "en"

    def captions_for(lang: str) -> list[CaptionCard]:
        return script(
            card,
            lang=lang,
            verification_code=passport.verification_code if passport else None,
            years_experience=None,
        )

    captions = captions_for(lang)

    photo_path = local_media_path(card.image_url)
    photo = None
    if photo_path is not None:
        try:
            photo = Image.open(photo_path).convert("RGB")
        except OSError:
            warnings.append(f"Could not read the product photo at {photo_path.name}.")
    else:
        warnings.append(
            "No local product photo, so the reel is text-only. "
            "Reels are worth far more with the picture."
        )

    qr_image = None
    if passport is not None:
        from app.adapters.stub_passport import render_qr_png

        try:
            qr_image = Image.open(render_qr_png(passport.verification_code))
        except (OSError, ImportError):
            warnings.append("Could not render the passport QR into the reel.")

    # A voiced reel and a silent one are different files, so a reel built
    # silent while offline is never served in place of a voiced one: the
    # next request tries the voice again.
    voiced = voice.enabled()

    def video_path_for(lang: str, *, with_voice: bool = voiced) -> Path:
        return directory / f"{card.listing_id}_{lang}{'_voice' if with_voice else ''}.mp4"

    video_path = video_path_for(lang)
    if video_path.exists() and not force:
        if voiced:
            stem = f"{card.listing_id}_{lang}"
            captions = _fit_to_voice(captions, _saved_narration(captions, directory, stem))
        return ReelResult(
            listing_id=card.listing_id,
            lang=lang,
            video_path=video_path,
            captions=captions,
            warnings=warnings,
        )

    try:
        frames = [compose_frame(c, photo=photo, lang=lang, qr=qr_image) for c in captions]
    except ReelError as exc:
        # Only shaped (Hindi) text goes through ffmpeg here, so English
        # always composes.
        warnings.append(f"{exc} The reel is captioned in English instead.")
        lang = "en"
        captions = captions_for(lang)
        video_path = video_path_for(lang)
        frames = [compose_frame(c, photo=photo, lang=lang, qr=qr_image) for c in captions]

    frame_paths: list[Path] = []
    for i, frame in enumerate(frames):
        frame_path = directory / f"{card.listing_id}_{lang}_{i}.png"
        frame.save(frame_path, "PNG")
        frame_paths.append(frame_path)

    if not ffmpeg_available():
        warnings.append(
            f"ffmpeg was not found (looked for {config.FFMPEG!r}), so this is a "
            "storyboard rather than a video. Install ffmpeg and rebuild for the reel."
        )
        return ReelResult(
            listing_id=card.listing_id,
            lang=lang,
            frame_paths=frame_paths,
            captions=captions,
            warnings=warnings,
        )

    making_clip = local_media_path(
        passport.making_clip_url if passport else None
    ) or local_media_path(f"/media/{card.listing_id}_making.mp4")

    narration: list[Path | None] | None = None
    if voiced:
        try:
            narration = _narrate(captions, lang, directory, f"{card.listing_id}_{lang}")
            captions = _fit_to_voice(captions, narration)
        except voice.VoiceError as exc:
            warnings.append(
                f"{exc} This reel is silent; opening it again will retry the voiceover."
            )
            narration = None
            video_path = video_path_for(lang, with_voice=False)

    command = build_ffmpeg_command(
        [(p, c.duration) for p, c in zip(frame_paths, captions)],
        video_path,
        making_clip=making_clip,
        making_index=1 if making_clip else None,
        narration=narration,
    )

    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=180, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"ffmpeg did not finish ({exc}). Showing the storyboard instead.")
        return ReelResult(
            listing_id=card.listing_id,
            lang=lang,
            frame_paths=frame_paths,
            captions=captions,
            warnings=warnings,
        )

    if completed.returncode != 0 or not video_path.exists():
        tail = (completed.stderr or "").strip().splitlines()[-4:]
        warnings.append(
            "ffmpeg failed, so this is a storyboard rather than a video: "
            + " / ".join(tail)
        )
        return ReelResult(
            listing_id=card.listing_id,
            lang=lang,
            frame_paths=frame_paths,
            captions=captions,
            warnings=warnings,
        )

    return ReelResult(
        listing_id=card.listing_id,
        lang=lang,
        video_path=video_path,
        frame_paths=frame_paths,
        captions=captions,
        warnings=warnings,
    )


def poster(card: ProductCard, *, lang: str = "en", out_dir: Path | None = None) -> Path | None:
    """The first frame on its own — a share image for WhatsApp and OG tags."""
    if lang == "hi" and not can_render_hindi():
        lang = "en"
    captions = script(card, lang=lang)
    if not captions:
        return None
    photo_path = local_media_path(card.image_url)
    photo = None
    if photo_path:
        try:
            photo = Image.open(photo_path).convert("RGB")
        except OSError:
            photo = None
    directory = Path(out_dir or config.REELS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        frame = compose_frame(captions[0], photo=photo, lang=lang)
    except ReelError:
        lang = "en"
        frame = compose_frame(script(card, lang=lang)[0], photo=photo, lang=lang)
    path = directory / f"{card.listing_id}_{lang}_poster.jpg"
    frame.save(path, "JPEG", quality=88)
    return path


__all__ = [
    "REEL_SECONDS",
    "ReelError",
    "ReelResult",
    "build",
    "build_ffmpeg_command",
    "can_render_hindi",
    "compose_frame",
    "ffmpeg_available",
    "find_font",
    "local_media_path",
    "poster",
]
