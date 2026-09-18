"""Product photo cleanup pipeline. Runs entirely offline — background
removal (step 2) is the only ML step; everything else is plain
OpenCV/Pillow.

Pipeline, in this exact order:
  1. Blur check (variance of Laplacian, on a histogram-equalized copy so
     a dark-but-sharp photo isn't confused for a blurry one — see
     _blur_score) — if below CRAFTLY_BLUR_THRESHOLD, return warning
     "retake" and stop. No further processing happens.
  2. Background removal via rembg.
  3. Composite the cutout onto a pure white background (alpha-blended,
     so soft/antialiased edges are preserved rather than hard-cut).
  4. Crop to the alpha bounding box + 8% padding, square output.
  5. Gray-world white balance, then subtle CLAHE on the L channel (LAB).
  6. Output a 2000x2000 JPEG at quality 90, plus a 600x600 thumbnail.

--- Model licensing (read this before changing CRAFTLY_REMBG_MODEL) ---

rembg's OWN default model, as of rembg 2.0.84, is "bria-rmbg" (RMBG-2.0)
— confirmed by inspecting rembg.new_session's actual signature, not just
its docs. RMBG-2.0 is released under a BRIA license that requires a paid
commercial agreement. Relying on rembg's default here would silently
introduce a paid-license dependency into a government deployment.

This module never uses that default. CRAFTLY_REMBG_MODEL defaults to
"u2net" instead: its source repo (github.com/xuebinqin/U-2-Net) is
licensed Apache License 2.0 — permissive, no payment or separate
commercial agreement required. See README.md's "Image cleanup" section
for the full citation and one residual caveat worth a legal read before
an actual government deployment (the Apache 2.0 license's "Object form"
language plausibly covers the distributed weight files, but there's an
open, unanswered upstream GitHub issue asking the maintainer to confirm
that explicitly for the trained weights, not just the code).

If you change CRAFTLY_REMBG_MODEL to anything else (especially back to
rembg's own default, or to "sam", "birefnet-*", etc.), check that
model's license before deploying — see the rembg README's per-model
notes, which flag at least one other model (bria-rmbg) as non-permissive.
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

REMBG_MODEL = os.environ.get("CRAFTLY_REMBG_MODEL", "u2net")
# Threshold is on the equalized-image score (see _blur_score) — a
# genuinely blurred test image scored ~280 on this scale, a dark-but-sharp
# one scored ~2700, so 500 sits well clear of both with real headroom.
# Real photos vary a lot; treat this as a starting point to tune against
# actual camera output, not a calibrated final value.
BLUR_THRESHOLD = float(os.environ.get("CRAFTLY_BLUR_THRESHOLD", "500.0"))
OUTPUT_DIR = Path(os.environ.get("CRAFTLY_PROCESSED_IMAGES_DIR", "processed_images"))
CLAHE_CLIP_LIMIT = float(os.environ.get("CRAFTLY_CLAHE_CLIP_LIMIT", "1.5"))
CLAHE_TILE_GRID_SIZE = 8
# Blend factor toward the full gray-world correction (1.0 = full
# correction, 0.0 = none). Damped well below 1.0 by default: a product
# that's naturally one strong colour (common for dyed textiles) would
# otherwise get partially desaturated trying to drag its average toward
# neutral gray — see _gray_world_white_balance's docstring.
WHITE_BALANCE_STRENGTH = float(os.environ.get("CRAFTLY_WHITE_BALANCE_STRENGTH", "0.6"))

FINAL_SIZE = 2000
THUMB_SIZE = 600
JPEG_QUALITY = 90
PADDING_FRACTION = 0.08

_session = None


def thumbnail_path_for(clean_path: str | Path) -> Path:
    """The 600x600 thumbnail is always written next to clean_path, named
    by inserting "_thumb" before its extension. Exposed as a function
    (rather than a second key in clean_product_image's return dict) so
    the return shape stays exactly {clean_path, alpha_path, warnings) as
    specified, while the thumbnail is still easy to find.
    """
    clean_path = Path(clean_path)
    return clean_path.with_name(f"{clean_path.stem}_thumb{clean_path.suffix}")


def _get_rembg_session():
    global _session
    if _session is None:
        from rembg import new_session

        _session = new_session(REMBG_MODEL)
    return _session


def _blur_score(gray: np.ndarray) -> float:
    """Variance of Laplacian, on a histogram-equalized copy of `gray`.

    The Laplacian is a linear filter, so variance-of-Laplacian scales
    with pixel amplitude: a dark-but-perfectly-sharp photo scores far
    lower than the same shot at normal exposure, purely from reduced
    contrast — not from any actual blur. Equalizing first (cheap, only
    used for this measurement, never affects the actual output pixels)
    makes the score reflect edge sharpness rather than exposure, so a
    dark sharp photo doesn't get wrongly sent back for a "retake" it
    doesn't need. Confirmed empirically: a synthetic sharp-but-dark test
    image scored 20 raw (below any sane threshold, a false "retake") vs.
    2713 equalized; a genuinely blurred image scored 282 equalized —
    still clearly separated from every sharp image tested. See
    tests/fixtures/bad_photos/ and scripts/try_image.py.
    """
    equalized = cv2.equalizeHist(gray)
    return float(cv2.Laplacian(equalized, cv2.CV_64F).var())


def _remove_background(pil_rgb: Image.Image) -> Image.Image:
    from rembg import remove

    result = remove(pil_rgb, session=_get_rembg_session())
    if result.mode != "RGBA":
        result = result.convert("RGBA")
    return result


def _composite_on_white(rgba: Image.Image) -> Image.Image:
    white_bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(white_bg, rgba).convert("RGB")


def _compute_square_canvas_params(
    bbox: tuple[int, int, int, int], padding_fraction: float
) -> tuple[int, int, int]:
    """Given an alpha bounding box, compute the size of a square canvas
    padded by `padding_fraction` of the larger content dimension on each
    side, and the (x, y) offset at which the source image must be pasted
    so the bbox's center lands at the canvas's center.
    """
    left, top, right, bottom = bbox
    content_size = max(right - left, bottom - top)
    padded_size = max(1, int(round(content_size * (1 + 2 * padding_fraction))))
    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0
    paste_x = int(round(padded_size / 2 - cx))
    paste_y = int(round(padded_size / 2 - cy))
    return padded_size, paste_x, paste_y


def _paste_centered(
    img: Image.Image, mode: str, fill, size: int, paste_x: int, paste_y: int
) -> Image.Image:
    """Paste `img` onto a new size x size canvas at (paste_x, paste_y).
    Any canvas area not covered by `img` (e.g. a tightly-cropped source
    photo with little room for padding) stays `fill` — for RGB that's
    white, matching the composited background; for RGBA that's fully
    transparent.
    """
    canvas = Image.new(mode, (size, size), fill)
    mask = img if mode == "RGBA" else None
    canvas.paste(img, (paste_x, paste_y), mask)
    return canvas


def _gray_world_white_balance(bgr: np.ndarray, fg_mask: np.ndarray | None = None) -> np.ndarray:
    """Gray-world assumes the average colour of what it's measuring is
    neutral gray. That's a reasonable assumption for a whole natural
    scene, but badly wrong for a product crop dominated by one saturated
    colour (e.g. a red pot filling most of the frame) — measuring the
    whole cropped/padded image (background included) lets a strongly
    coloured product drag the correction so far off that it visibly
    tints the padding, which is manufactured pure white and should never
    need correcting at all.

    fg_mask (boolean array, same H×W as bgr — pass the alpha>0 mask):
    restricts the *measurement* to foreground pixels, so a saturated
    product doesn't get diluted/distorted by the neutral padding, and the
    padding's statistics don't get corrupted by the product either. The
    correction itself is still computed with WHITE_BALANCE_STRENGTH < 1
    (see module constant) and applied to the whole array — callers are
    expected to re-composite onto pure white afterwards (see
    clean_product_image) so the background can never end up tinted
    regardless of what this function computes.
    """
    img = bgr.astype(np.float32)
    b, g, r = cv2.split(img)
    if fg_mask is not None and fg_mask.any():
        b_mean, g_mean, r_mean = b[fg_mask].mean(), g[fg_mask].mean(), r[fg_mask].mean()
    else:
        b_mean, g_mean, r_mean = b.mean(), g.mean(), r.mean()
    gray_mean = (b_mean + g_mean + r_mean) / 3.0
    eps = 1e-6
    strength = WHITE_BALANCE_STRENGTH
    b_scale = 1.0 + strength * (gray_mean / (b_mean + eps) - 1.0)
    g_scale = 1.0 + strength * (gray_mean / (g_mean + eps) - 1.0)
    r_scale = 1.0 + strength * (gray_mean / (r_mean + eps) - 1.0)
    balanced = cv2.merge([b * b_scale, g * g_scale, r * r_scale])
    return np.clip(balanced, 0, 255).astype(np.uint8)


def _subtle_clahe(bgr: np.ndarray) -> np.ndarray:
    # Only the L (lightness) channel is touched — a and b (colour) are
    # passed through untouched, so this can't shift hue/saturation, only
    # local contrast. clipLimit is deliberately conservative (see
    # CRAFTLY_CLAHE_CLIP_LIMIT default): these are textiles and
    # handicrafts, where colour accuracy matters more than punch.
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(
        clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=(CLAHE_TILE_GRID_SIZE, CLAHE_TILE_GRID_SIZE)
    )
    l_eq = clahe.apply(l_channel)
    lab_eq = cv2.merge([l_eq, a_channel, b_channel])
    return cv2.cvtColor(lab_eq, cv2.COLOR_LAB2BGR)


def _recomposite_on_white(bgr: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Alpha-blend `bgr` back onto pure white using `alpha` (uint8, same
    H×W, 0=transparent..255=opaque). Guarantees padding is exactly white
    and foreground edges stay soft, regardless of any per-pixel drift a
    prior step introduced.
    """
    alpha_f = (alpha.astype(np.float32) / 255.0)[:, :, None]
    white = np.full_like(bgr, 255, dtype=np.float32)
    blended = bgr.astype(np.float32) * alpha_f + white * (1.0 - alpha_f)
    return np.clip(blended, 0, 255).astype(np.uint8)


def _resize_square(bgr: np.ndarray, size: int) -> np.ndarray:
    h, w = bgr.shape[:2]
    interpolation = cv2.INTER_AREA if size < w else cv2.INTER_LANCZOS4
    return cv2.resize(bgr, (size, size), interpolation=interpolation)


def clean_product_image(input_path: str | Path, output_dir: str | Path | None = None) -> dict:
    """Run the full cleanup pipeline on `input_path`.

    Returns {"clean_path": str | None, "alpha_path": str | None,
    "warnings": list[str]}.

    If the blur check fails, clean_path/alpha_path are None, warnings is
    ["retake"], and nothing else in the pipeline runs (no background
    removal, no files written) — a blurry photo isn't worth the cost of
    processing, it just needs to be retaken.

    The 600x600 thumbnail isn't a separate return key (the contract is
    exactly {clean_path, alpha_path, warnings}); find it via
    thumbnail_path_for(result["clean_path"]).
    """
    input_path = Path(input_path)
    out_dir = Path(output_dir) if output_dir is not None else OUTPUT_DIR
    warnings: list[str] = []

    bgr = cv2.imread(str(input_path))
    if bgr is None:
        raise ValueError(f"Could not read image: {input_path}")

    # --- Step 1: blur check ---
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if _blur_score(gray) < BLUR_THRESHOLD:
        warnings.append("retake")
        return {"clean_path": None, "alpha_path": None, "warnings": warnings}

    # --- Step 2: background removal ---
    pil_rgb = Image.open(input_path).convert("RGB")
    rgba = _remove_background(pil_rgb)

    alpha = rgba.split()[-1]
    bbox = alpha.getbbox()
    if bbox is None:
        # rembg found nothing distinct from the background at all — fall
        # back to the full frame rather than crashing, but flag it since
        # the result is unlikely to be useful as-is.
        warnings.append("no_subject_detected")
        bbox = (0, 0, rgba.width, rgba.height)

    # --- Step 3: composite onto pure white ---
    composited = _composite_on_white(rgba)

    # --- Step 4: crop to alpha bbox + 8% padding, square ---
    padded_size, paste_x, paste_y = _compute_square_canvas_params(bbox, PADDING_FRACTION)
    cropped_rgb = _paste_centered(composited, "RGB", (255, 255, 255), padded_size, paste_x, paste_y)
    cropped_rgba = _paste_centered(rgba, "RGBA", (0, 0, 0, 0), padded_size, paste_x, paste_y)

    # --- Step 5: white balance + subtle CLAHE ---
    bgr_cropped = cv2.cvtColor(np.array(cropped_rgb), cv2.COLOR_RGB2BGR)
    alpha_arr = np.array(cropped_rgba.split()[-1])
    fg_mask = alpha_arr > 10  # excludes fully/near-fully transparent padding

    balanced = _gray_world_white_balance(bgr_cropped, fg_mask=fg_mask)
    enhanced = _subtle_clahe(balanced)
    # Re-composite onto pure white using the (unmodified) alpha mask: this
    # guarantees the padding is exactly white regardless of any drift
    # white-balance/CLAHE introduced, the same guarantee step 3 made.
    enhanced = _recomposite_on_white(enhanced, alpha_arr)

    # --- Step 6: output sizes ---
    final_bgr = _resize_square(enhanced, FINAL_SIZE)
    thumb_bgr = _resize_square(enhanced, THUMB_SIZE)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem
    clean_path = out_dir / f"{stem}_clean.jpg"
    thumb_path = thumbnail_path_for(clean_path)
    alpha_path = out_dir / f"{stem}_alpha.png"

    cv2.imwrite(str(clean_path), final_bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    cv2.imwrite(str(thumb_path), thumb_bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    cropped_rgba.save(alpha_path)

    return {"clean_path": str(clean_path), "alpha_path": str(alpha_path), "warnings": warnings}
