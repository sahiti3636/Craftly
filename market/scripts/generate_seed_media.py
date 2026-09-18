"""Generate placeholder product photos and artisan portraits for the seed catalogue.

The seed catalogue describes real crafts, and this repository has no
photographs of them — using someone's Ajrakh dupatta off the internet to
demo a fair-trade marketplace would be a poor joke. So each listing gets a
generated swatch instead: a woven pattern drawn in the colours the artisan
actually named, labelled with the craft, obviously a placeholder and
obviously not a stolen photo.

Run once after cloning:

    python scripts/generate_seed_media.py

Everything it writes lands in `seed/media/` and is gitignored. Replace any
file with a real photo of the same name and every surface picks it up.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "seed"
MEDIA = SEED / "media"

SIZE = (1000, 1250)
PORTRAIT = (400, 400)

#: Named colours in the seed data mapped to something paintable.
PALETTE = {
    "red": (168, 51, 39),
    "terracotta red": (178, 84, 56),
    "gold": (196, 148, 62),
    "indigo": (41, 58, 99),
    "madder red": (150, 58, 51),
    "ivory": (238, 228, 210),
    "ochre": (186, 137, 58),
    "white": (242, 238, 230),
    "lamp black": (38, 35, 32),
    "black": (38, 35, 32),
    "antique brass": (155, 122, 54),
    "mustard": (194, 155, 52),
    "yellow": (216, 174, 56),
    "green": (74, 105, 66),
    "natural": (203, 180, 145),
    "earth brown": (112, 82, 58),
    "cobalt blue": (44, 74, 132),
    "crimson": (151, 43, 56),
}
GROUND = (247, 242, 233)


def colour_for(name: str) -> tuple[int, int, int]:
    if name in PALETTE:
        return PALETTE[name]
    digest = hashlib.sha256(name.encode()).digest()
    return (110 + digest[0] % 110, 90 + digest[1] % 100, 70 + digest[2] % 100)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "C:/Windows/Fonts/seguisb.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def draw_weave(draw: ImageDraw.ImageDraw, colours, rng: random.Random) -> None:
    """Block-print-ish grid: the texture most of these crafts share."""
    step = 84
    for y in range(120, SIZE[1] - 200, step):
        for x in range(90, SIZE[0] - 90, step):
            colour = rng.choice(colours)
            shape = rng.randrange(4)
            box = (x + 8, y + 8, x + step - 16, y + step - 16)
            if shape == 0:
                draw.ellipse(box, fill=colour)
            elif shape == 1:
                draw.rectangle(box, outline=colour, width=7)
            elif shape == 2:
                draw.polygon(
                    [(x + step // 2, y + 8), (x + step - 14, y + step - 14), (x + 12, y + step - 14)],
                    fill=colour,
                )
            else:
                draw.line([(x + 10, y + 10), (x + step - 18, y + step - 18)], fill=colour, width=9)
                draw.line([(x + step - 18, y + 10), (x + 10, y + step - 18)], fill=colour, width=9)


def product_image(listing: dict) -> Image.Image:
    rng = random.Random(listing["listing_id"])
    colours = [colour_for(c) for c in (listing.get("colours") or ["terracotta red"])]

    image = Image.new("RGB", SIZE, GROUND)
    draw = ImageDraw.Draw(image)

    # A soft vignette so the swatch reads as an object, not a texture fill.
    draw.rounded_rectangle((60, 90, SIZE[0] - 60, SIZE[1] - 170), 26, fill=(252, 249, 243))
    draw_weave(draw, colours, rng)

    overlay = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    centre = (SIZE[0] // 2, (SIZE[1] - 80) // 2)
    radius = 250
    odraw.ellipse(
        (centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius),
        fill=(*colours[0], 210),
    )
    if len(colours) > 1:
        inner = int(radius * 0.62)
        odraw.ellipse(
            (centre[0] - inner, centre[1] - inner, centre[0] + inner, centre[1] + inner),
            fill=(*colours[1 % len(colours)], 235),
        )
    ring = int(radius * 0.86)
    odraw.ellipse(
        (centre[0] - ring, centre[1] - ring, centre[0] + ring, centre[1] + ring),
        outline=(*colours[-1], 255),
        width=10,
    )
    image = Image.alpha_composite(image.convert("RGBA"), overlay.filter(ImageFilter.GaussianBlur(1.2)))
    image = image.convert("RGB")

    draw = ImageDraw.Draw(image)
    craft = (listing.get("craft_type") or "handmade").upper()
    label = font(34, bold=True)
    width = draw.textlength(craft, font=label)
    draw.text(((SIZE[0] - width) / 2, SIZE[1] - 132), craft, font=label, fill=(70, 60, 48))

    note = font(22)
    msg = "placeholder — replace with the artisan's photo"
    width = draw.textlength(msg, font=note)
    draw.text(((SIZE[0] - width) / 2, SIZE[1] - 88), msg, font=note, fill=(150, 138, 120))
    return image


def portrait_image(artisan: dict) -> Image.Image:
    rng = random.Random(artisan["artisan_id"])
    base = colour_for(artisan.get("craft") or artisan["name"])
    image = Image.new("RGB", PORTRAIT, tuple(min(255, c + 60) for c in base))
    draw = ImageDraw.Draw(image)

    for _ in range(26):
        x, y = rng.randrange(PORTRAIT[0]), rng.randrange(PORTRAIT[1])
        r = rng.randrange(18, 64)
        draw.ellipse((x - r, y - r, x + r, y + r), outline=base, width=4)

    initials = "".join(part[0] for part in artisan["name"].split()[:2]).upper()
    glyph = font(150, bold=True)
    width = draw.textlength(initials, font=glyph)
    draw.text(
        ((PORTRAIT[0] - width) / 2, PORTRAIT[1] / 2 - 100), initials, font=glyph, fill=(252, 249, 243)
    )
    return image


def main() -> int:
    MEDIA.mkdir(parents=True, exist_ok=True)
    listings = json.loads((SEED / "listings.json").read_text(encoding="utf-8"))
    artisans = json.loads((SEED / "artisans.json").read_text(encoding="utf-8"))

    for listing in listings:
        image = product_image(listing)
        image.save(MEDIA / f"{listing['listing_id']}.jpg", "JPEG", quality=86)
        # The "original" is the same photo, dulled and slightly blurred, so
        # the before/after of A2's photo cleanup has something to show.
        original = image.filter(ImageFilter.GaussianBlur(1.6)).point(lambda v: int(v * 0.86))
        original.save(MEDIA / f"{listing['listing_id']}_original.jpg", "JPEG", quality=78)

    for artisan in artisans:
        portrait_image(artisan).save(MEDIA / f"artisan_{artisan['artisan_id']}.jpg", "JPEG", quality=86)

    print(f"Wrote {len(listings) * 2 + len(artisans)} files to {MEDIA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
