#!/usr/bin/env python3
"""Batch-run app.image.clean_product_image over a folder of photos and
write a before/after contact sheet, so image quality can be eyeballed
quickly without opening every file individually.

Usage:
    uv run python scripts/try_image.py tests/fixtures/bad_photos
    uv run python scripts/try_image.py path/to/photos --out contact_sheet.png
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.image import clean_product_image  # noqa: E402

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

THUMB_SIZE = 260
PADDING = 16
LABEL_HEIGHT = 40
ROW_HEIGHT = THUMB_SIZE + LABEL_HEIGHT + PADDING
COL_WIDTH = THUMB_SIZE + PADDING
HEADER_HEIGHT = 40
RETAKE_COLOR = (200, 60, 60)
WARN_COLOR = (190, 130, 20)
OK_COLOR = (60, 140, 70)


def _fit_thumbnail(img: Image.Image, size: int) -> Image.Image:
    """Resize to fit within size x size, then paste centered onto a
    light-gray size x size canvas — keeps every thumbnail the same
    dimensions in the contact sheet regardless of aspect ratio.
    """
    canvas = Image.new("RGB", (size, size), (235, 235, 235))
    fitted = img.copy()
    fitted.thumbnail((size, size))
    x = (size - fitted.width) // 2
    y = (size - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return canvas


def _placeholder(size: int, text: str, color: tuple[int, int, int]) -> Image.Image:
    canvas = Image.new("RGB", (size, size), (245, 245, 245))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([4, 4, size - 4, size - 4], outline=color, width=4)
    _draw_wrapped_text(draw, text, (size // 2, size // 2), color, size - 20, anchor="mm")
    return canvas


def _draw_wrapped_text(draw, text, xy, color, max_width, anchor="la"):
    font = ImageFont.load_default()
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) > max_width and current:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)

    x, y = xy
    line_height = 14
    total_h = line_height * len(lines)
    start_y = y - total_h // 2 if "m" in anchor else y
    for i, line in enumerate(lines):
        draw.text((x, start_y + i * line_height), line, fill=color, font=font, anchor=anchor)


def process_folder(input_dir: Path, output_dir: Path) -> list[dict]:
    results = []
    for path in sorted(input_dir.iterdir()):
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        start = time.time()
        try:
            result = clean_product_image(path, output_dir=output_dir)
            error = None
        except Exception as exc:  # keep going even if one file blows up
            result = {"clean_path": None, "alpha_path": None, "warnings": []}
            error = str(exc)
        elapsed = time.time() - start
        results.append(
            {
                "input_path": path,
                "clean_path": result["clean_path"],
                "warnings": result["warnings"],
                "error": error,
                "elapsed_sec": elapsed,
            }
        )
        status = "ERROR" if error else (result["warnings"] or "ok")
        print(f"{path.name}: {status} ({elapsed:.2f}s)")
    return results


def build_contact_sheet(results: list[dict], out_path: Path) -> None:
    if not results:
        raise ValueError("No images to build a contact sheet from.")

    n = len(results)
    # before | after
    sheet_width = COL_WIDTH * 2 + PADDING
    sheet_height = HEADER_HEIGHT + ROW_HEIGHT * n + PADDING
    sheet = Image.new("RGB", (sheet_width, sheet_height), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()

    draw.text((PADDING, 10), "BEFORE", fill=(30, 30, 30), font=font)
    draw.text((PADDING + COL_WIDTH, 10), "AFTER", fill=(30, 30, 30), font=font)

    y = HEADER_HEIGHT
    for row in results:
        before_img = Image.open(row["input_path"]).convert("RGB")
        before_thumb = _fit_thumbnail(before_img, THUMB_SIZE)
        sheet.paste(before_thumb, (PADDING, y))

        if row["error"]:
            after_thumb = _placeholder(THUMB_SIZE, f"ERROR: {row['error'][:80]}", RETAKE_COLOR)
        elif row["clean_path"] is None:
            after_thumb = _placeholder(
                THUMB_SIZE, f"STOPPED: {', '.join(row['warnings'])}", RETAKE_COLOR
            )
        else:
            after_img = Image.open(row["clean_path"]).convert("RGB")
            after_thumb = _fit_thumbnail(after_img, THUMB_SIZE)
        sheet.paste(after_thumb, (PADDING + COL_WIDTH, y))

        label_y = y + THUMB_SIZE + 4
        label_color = RETAKE_COLOR if row["error"] or row["clean_path"] is None else (
            WARN_COLOR if row["warnings"] else OK_COLOR
        )
        label = f"{row['input_path'].name}  ({row['elapsed_sec']:.2f}s)"
        if row["warnings"]:
            label += f"  warnings: {', '.join(row['warnings'])}"
        draw.text((PADDING, label_y), label, fill=label_color, font=font)

        y += ROW_HEIGHT

    sheet.save(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path, help="Folder of photos to process")
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=None,
        help="Where cleaned images are written (default: <input_dir>/_processed)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Contact sheet output path (default: <input_dir>/_contact_sheet.png)",
    )
    args = parser.parse_args()

    if not args.input_dir.is_dir():
        parser.error(f"{args.input_dir} is not a directory")

    processed_dir = args.processed_dir or (args.input_dir / "_processed")
    out_path = args.out or (args.input_dir / "_contact_sheet.png")

    results = process_folder(args.input_dir, processed_dir)
    if not results:
        print(f"No images found in {args.input_dir} (looked for {sorted(IMAGE_EXTENSIONS)})")
        return

    build_contact_sheet(results, out_path)
    print(f"\nContact sheet written to {out_path}")


if __name__ == "__main__":
    main()
