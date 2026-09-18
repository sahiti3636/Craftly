#!/usr/bin/env python3
"""Manual ASR try-out CLI.

Usage:
    uv run python scripts/try_asr.py path/to/recording.m4a
    uv run python scripts/try_asr.py path/to/recording.m4a --language hi

Prints the transcribe() result as JSON. Useful for testing with real phone
recordings, since the automated test suite only uses synthetic tones.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.asr import (  # noqa: E402
    AudioConversionError,
    AudioDurationError,
    UnsupportedAudioFormatError,
    transcribe,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_path", type=Path, help="Path to a .m4a/.mp3/.wav/.ogg file")
    parser.add_argument(
        "--language",
        default=None,
        help="Optional ISO language hint (e.g. 'hi'), overrides auto-detection",
    )
    args = parser.parse_args()

    try:
        result = transcribe(args.audio_path, language_hint=args.language)
    except (
        FileNotFoundError,
        UnsupportedAudioFormatError,
        AudioDurationError,
        AudioConversionError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
