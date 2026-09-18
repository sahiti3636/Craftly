#!/usr/bin/env python3
"""Generate the 5 deliberately-bad synthetic product photos used to test
app/image.py: tests/fixtures/bad_photos/{dark,cluttered,blurry,
off_white,tight_crop}.jpg.

These are synthetic (drawn, not real photographs) so the fixtures are
small, deterministic (fixed seed), and don't depend on any external
image source — but each is built to trigger a specific real-world
failure mode:
  - dark.jpg: underexposed (should still process, but a real pipeline
    downstream might want to flag low overall brightness too).
  - cluttered.jpg: busy, multi-object background (stresses background
    removal's segmentation).
  - blurry.jpg: strong blur (must trigger the "retake" warning and stop
    the pipeline before background removal even runs).
  - off_white.jpg: cream/beige background instead of true white (tests
    that output is forced to pure white regardless of input background
    colour).
  - tight_crop.jpg: product fills nearly the whole frame (tests the 8%
    padding logic when there's little/no room within the original image
    bounds, forcing the canvas-extension behaviour in
    app.image._paste_centered).

Re-run this script to regenerate the fixtures (e.g. after changing the
product drawing) — output is fully deterministic.
"""

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bad_photos"

SIZE = 800
BASE_COLOR = (150, 70, 40)  # terracotta-ish
ACCENT_COLOR = (210, 160, 90)  # a woven-textile accent tone


def _draw_product(canvas: Image.Image, cx: int, cy: int, radius: float) -> None:
    """A roughly bowl/pot-shaped product with concentric woven-looking
    rings, so there's texture for CLAHE/white-balance to act on (a flat
    single colour doesn't stress-test either step meaningfully).
    """
    draw = ImageDraw.Draw(canvas)
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=BASE_COLOR)
    ring_count = 6
    for i in range(ring_count):
        ring_r = radius * (0.85 - i * 0.12)
        if ring_r <= radius * 0.1:
            break
        color = ACCENT_COLOR if i % 2 == 0 else BASE_COLOR
        width = max(2, int(radius * 0.04))
        draw.ellipse(
            [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r], outline=color, width=width
        )


def make_dark() -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), (250, 250, 248))
    _draw_product(img, SIZE // 2, SIZE // 2, SIZE * 0.32)
    # Underexpose: multiply brightness down hard.
    dark = Image.eval(img, lambda p: int(p * 0.22))
    return dark


def make_cluttered() -> Image.Image:
    rng = random.Random(42)
    img = Image.new("RGB", (SIZE, SIZE), (235, 232, 225))
    draw = ImageDraw.Draw(img)
    # A busy background: scattered shapes in assorted colours/sizes,
    # behind and partly overlapping the product's bounding area.
    for _ in range(40):
        x = rng.randint(0, SIZE)
        y = rng.randint(0, SIZE)
        w = rng.randint(15, 90)
        h = rng.randint(15, 90)
        color = tuple(rng.randint(60, 220) for _ in range(3))
        shape = rng.choice(["rect", "ellipse"])
        box = [x, y, x + w, y + h]
        if shape == "rect":
            draw.rectangle(box, fill=color)
        else:
            draw.ellipse(box, fill=color)
    _draw_product(img, SIZE // 2, SIZE // 2, SIZE * 0.3)
    return img


def make_blurry() -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), (250, 250, 248))
    _draw_product(img, SIZE // 2, SIZE // 2, SIZE * 0.32)
    return img.filter(ImageFilter.GaussianBlur(radius=18))


def make_off_white() -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), (238, 222, 180))  # cream/beige, not white
    _draw_product(img, SIZE // 2, SIZE // 2, SIZE * 0.32)
    return img


def make_tight_crop() -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), (250, 250, 248))
    # Radius close to half the frame — almost no margin around the
    # product, unlike the other fixtures' ~0.3 radius.
    _draw_product(img, SIZE // 2, SIZE // 2, SIZE * 0.49)
    return img


GENERATORS = {
    "dark": make_dark,
    "cluttered": make_cluttered,
    "blurry": make_blurry,
    "off_white": make_off_white,
    "tight_crop": make_tight_crop,
}


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for name, generator in GENERATORS.items():
        img = generator()
        out_path = FIXTURES_DIR / f"{name}.jpg"
        img.save(out_path, quality=92)
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
