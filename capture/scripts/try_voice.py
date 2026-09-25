#!/usr/bin/env python3
"""Speak into the mic, see what the capture pipeline makes of it, hear the readback.

Usage (from capture/):
    uv run python scripts/try_voice.py                   # record 10 s from the default mic
    uv run python scripts/try_voice.py --seconds 15 --lang hi
    uv run python scripts/try_voice.py --file my_note.m4a
    uv run python scripts/try_voice.py --mic "Microphone Array (AMD Audio Device)"

Runs the same create_listing() that POST /listing/create runs (speech-to-text,
number parsing, AI extraction and descriptions, spoken summary) on your voice
and the sample vase photo, prints what came out, and plays the readback.
--lang is what Studio sends as the artisan's chosen language: used only when
nothing is heard or detection is unsure. Recording needs ffmpeg on PATH.
"""

import argparse
import asyncio
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # .env (GROQ_API_KEY) and the runtime folders live here

from app.pipeline import create_listing  # noqa: E402

PHOTO = ROOT / "static" / "studio" / "assets" / "vase.png"


def default_mic() -> str:
    """The first audio input DirectShow lists (Windows)."""
    listing = subprocess.run(
        ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True,
    ).stderr
    found = re.findall(r'"([^"]+)" \(audio\)', listing)
    if not found:
        sys.exit("No microphone found. Pass one with --mic, or use --file.")
    return found[0]


def record(seconds: int, mic: str) -> Path:
    out = Path(tempfile.mkdtemp()) / "voice.wav"
    input(f"Mic: {mic}\nPress Enter, then speak for {seconds} seconds... ")
    print("Recording...", flush=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "dshow", "-i", f"audio={mic}",
         "-t", str(seconds), "-ac", "1", "-ar", "16000", str(out)],
        check=True,
    )
    print("Done.\n")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, help="use this recording instead of the mic")
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--mic", help="DirectShow microphone name (default: the first one)")
    parser.add_argument("--lang", default=None, help="her chosen language, e.g. hi or en")
    parser.add_argument("--no-play", action="store_true", help="do not play the readback")
    args = parser.parse_args()

    audio = args.file or record(args.seconds, args.mic or default_mic())
    print("Running the capture pipeline (the first run downloads models)...\n", flush=True)
    result = asyncio.run(create_listing(
        artisan_id="try_voice", image_path=PHOTO, audio_path=audio,
        image_original_url="", language_preference=args.lang,
    ))
    listing = result["listing"]
    rows = [
        ("Heard", listing.transcript_raw),
        ("Language", listing.source_language),
        ("Material cost (Rs)", listing.material_cost_inr),
        ("Hours worked", listing.hours_worked),
        ("Needs confirming", ", ".join(listing.needs_confirmation) or "-"),
        ("Title (en)", listing.title_en),
        ("Title (hi)", listing.title_hi),
        ("Readback", listing.summary_spoken),
        ("Problems", ", ".join(result["errors"]) or "none"),
    ]
    for label, value in rows:
        print(f"{label:>20}: {value if value not in (None, '') else '-'}")

    spoken = result.get("summary_audio_path")
    if spoken and not args.no_play:
        print(f"\nPlaying the readback: {spoken}")
        os.startfile(spoken) if hasattr(os, "startfile") else print("(open it with any audio player)")
    elif not spoken:
        print("\nNo readback audio was made (see Problems above).")


if __name__ == "__main__":
    main()
