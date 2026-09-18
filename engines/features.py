"""
Craftly B1 — Step 2: feature extraction  (and the shared feature definitions used at inference)

    python features.py              # download images, embed, build structured features, save
    python features.py --force      # recompute everything, ignore caches
    python features.py --no-images  # skip image download (image branch becomes zero vectors)

What it produces (all from data/processed/products.pkl written by data.py):
    data/images/<pid>.jpg                     downloaded product photos (resized, cached)
    data/cache/image_emb.npy   (N, 512)       CLIP ViT-B/32, L2-normalised; zeros when no image
    data/cache/text_emb.npy    (N, 384)       all-MiniLM-L6-v2 on "title | description", L2-normalised
    data/cache/struct.npy      (N, 8)         structured features (see STRUCT_FEATURES)
    data/cache/meta.pkl                       ids, prices, split, category ... aligned with the arrays
    data/cache/features_manifest.json         dims, coverage, encoder versions
    models/price_model/encoders.pkl           vocabularies (fit on TRAIN split only)
    models/price_model/category_stats.pkl     category median / std price (TRAIN split only)

The functions in the "shared feature definitions" section are imported by data.py
(text cleaning, material parsing) and service.py (encoders + structured row builder),
so a new artisan product is featurised by exactly the same code as the training data.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# huggingface_hub's Xet transfer backend can stall on some networks (the download appears to hang
# with no progress); the plain HTTPS path is slower but reliable. Override by exporting HF_HUB_DISABLE_XET=0.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
# macOS: LightGBM/XGBoost and PyTorch each ship their own OpenMP runtime. When both end up in one
# process (the service loads the booster with joblib, then torch), the first threaded op can deadlock
# — typically inside torch module construction, with no error at all. These two make that safe.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import joblib
import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Paths and constants
# ─────────────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PROCESSED = DATA / "processed" / "products.pkl"
IMAGES = DATA / "images"
CACHE = DATA / "cache"
MODEL_DIR = ROOT / "models" / "price_model"

# Encoders. Env overrides exist so a local checkpoint path can be used offline.
CLIP_ARCH = os.environ.get("B1_CLIP_ARCH", "ViT-B-32")
CLIP_PRETRAINED = os.environ.get("B1_CLIP_PRETRAINED", "laion2b_s34b_b79k")
TEXT_MODEL = os.environ.get("B1_TEXT_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
IMG_DIM, TXT_DIM = 512, 384

STRUCT_FEATURES = [
    "category_enc", "subcategory_enc", "material_enc",
    "title_word_count", "desc_word_count", "has_image",
    "category_median_price", "category_price_std",
    # finer price anchors than the top-level category (train-split statistics only)
    "subcategory_median_price", "material_median_price",
    # physical scale: a 6 cm idol and a 30 cm idol are not the same product
    "size_cm", "has_size", "pack_count",
]
CATEGORICAL_STRUCT = ["category_enc", "subcategory_enc", "material_enc"]
# Retrieval features: what the k nearest real listings actually sell for. Computed leakage-safely at
# training time (a row never sees its own name-group) and from the training index at inference.
EXTRA_FEATURES = ["comparable_median_price", "comparable_mean_similarity",
                  "comparable_top1_price", "comparable_iqr_ratio"]
FEATURE_NAMES = ([f"img_{i}" for i in range(IMG_DIM)]
                 + [f"txt_{i}" for i in range(TXT_DIM)]
                 + STRUCT_FEATURES + EXTRA_FEATURES)      # 512 + 384 + 8 + 2 = 906

# Canonical craft taxonomy — the vocabulary the artisan app (A1) sends to the API.
CATEGORIES = [
    "jewellery", "apparel", "footwear", "home_decor", "home_furnishing",
    "kitchen_dining", "bags_accessories", "toys_games", "stationery", "furniture",
]
MIN_SUBCATEGORY_COUNT = 15          # subcategories rarer than this in TRAIN collapse to "other"
UNKNOWN = "other"

# Comparable search weights (plan §5)
SIM_W_IMAGE, SIM_W_TEXT = 0.4, 0.6


def _hf_hub_dir() -> Path:
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"


def _repo_cached(pattern: str, min_bytes: int = 10_000_000) -> bool:
    """True if a HuggingFace repo matching `pattern` has real weights (not just pointers) on disk."""
    hub = _hf_hub_dir()
    if not hub.exists():
        return False
    for repo in hub.glob(pattern):
        for f in repo.glob("snapshots/*/*"):
            try:
                if f.stat().st_size > min_bytes:
                    return True
            except OSError:
                pass
    return False


def clip_weights_cached() -> bool:
    """True if the CLIP checkpoint is already on disk (local file, or in the HuggingFace cache)."""
    return Path(CLIP_PRETRAINED).exists() or _repo_cached("models--*CLIP-ViT-B-32*", 100_000_000)


def text_weights_cached() -> bool:
    return Path(TEXT_MODEL).exists() or _repo_cached(f"models--*{Path(TEXT_MODEL).name}*", 10_000_000)


# Once both encoders are cached, talk to the Hub no more. huggingface_hub still makes a metadata
# request per file on every load, and on a slow / rate-limited connection (unauthenticated requests
# are throttled) that request can stall for minutes even though the weights are already local — which
# looks exactly like a hang. HF_HUB_OFFLINE is read when the libraries are imported, so it has to be
# set here, before the lazy `import torch / open_clip / sentence_transformers` further down.
# Override with B1_HF_OFFLINE=0 (always online) or 1 (always offline).
_B1_HF_OFFLINE = os.environ.get("B1_HF_OFFLINE", "auto")
if _B1_HF_OFFLINE == "1" or (_B1_HF_OFFLINE == "auto" and clip_weights_cached() and text_weights_cached()):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

IMAGE_MAX_SIDE = 320
HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


# ═════════════════════════════════════════════════════════════════════════════
# SHARED FEATURE DEFINITIONS  (used by data.py and service.py too)
# ═════════════════════════════════════════════════════════════════════════════

# ── Material vocabulary: ordered (first match wins, so specific before generic) ──
MATERIAL_RULES: list[tuple[str, str]] = [
    ("faux_leather",      r"\b(pu|p\.u\.|faux leather|synthetic leather|leatherette|vegan leather|rexine)\b"),
    ("leather",           r"\bleather\b"),
    ("alloy_metal",       r"\b(german silver|oxidi[sz]ed|white metal|imitation)\b"),
    ("silver",            r"\b(sterling|silver|925)\b"),
    ("brass",             r"\b(brass|bell metal|dhokra|dokra)\b"),
    ("copper",            r"\bcopper\b"),
    ("gold",              r"\b(\d{2}\s?k(?:t|arat)?|solid|yellow|rose|white)?\s?gold\b(?![- ]?(?:plated|plating|tone|toned|finish|polish|coated))"),
    ("silk",              r"\b(silk|banarasi|kanjeevaram|kanchipuram|tussar|chanderi)\b"),
    ("wool",              r"\b(wool|woollen|woolen|pashmina|cashmere|merino)\b"),
    ("khadi",             r"\bkhadi\b"),
    ("jute",              r"\b(jute|hemp|sisal|coir)\b"),
    ("linen",             r"\blinen\b"),
    ("cotton",            r"\b(cotton|cambric|muslin|mulmul|handloom)\b"),
    ("rayon_viscose",     r"\b(rayon|viscose|modal)\b"),
    ("synthetic_fabric",  r"\b(polyester|georgette|chiffon|crepe|nylon|lycra|spandex|net fabric|satin|velvet|microfiber|microfibre|polycotton|synthetic)\b"),
    ("bamboo_cane",       r"\b(bamboo|cane|rattan|wicker|seagrass|sabai)\b"),
    ("wood",              r"\b(wood|wooden|sheesham|teak|rosewood|mdf|plywood|sandalwood)\b"),
    ("terracotta_ceramic", r"\b(terracotta|clay|ceramic|porcelain|stoneware|bone china|pottery|earthen)\b"),
    ("glass",             r"\bglass\b"),
    ("stone_marble",      r"\b(marble|stone|soapstone|granite|onyx|jade)\b"),
    ("beads_crystal",     r"\b(bead|beads|crystal|pearl|pearls|zircon|zirconia|cz|kundan|polki|rhinestone)\b"),
    ("paper",             r"\b(paper|papier|cardboard)\b"),
    ("resin",             r"\b(resin|polyresin|polystone)\b"),
    ("alloy_metal",       r"\b(alloy|metal|metallic|zinc|iron|steel|stainless|aluminium|aluminum)\b"),
    ("plastic",           r"\b(plastic|acrylic|pvc|melamine|abs|polypropylene|silicone)\b"),
    ("fabric_other",      r"\b(fabric|cloth|textile|canvas)\b"),
]
MATERIALS = list(dict.fromkeys(m for m, _ in MATERIAL_RULES))   # unique, ordered
_MATERIAL_RX = [(m, re.compile(rx, re.I)) for m, rx in MATERIAL_RULES]

# Spec keys (Flipkart product_specifications) in priority order
MATERIAL_SPEC_KEYS = [
    "material", "base material", "fabric", "primary material", "outer material",
    "body material", "top fabric", "frame material", "box material", "plating",
]


def material_from_text(*texts: str) -> str:
    """Map free text to the canonical material vocabulary ('other' if nothing matches)."""
    blob = " ".join(t for t in texts if isinstance(t, str))
    for name, rx in _MATERIAL_RX:
        if rx.search(blob):
            return name
    return UNKNOWN


def normalise_material(value: str | None) -> str:
    """Accept either a canonical material id or free text ('Brass', 'mango wood') from the app."""
    if not value:
        return UNKNOWN
    v = str(value).strip().lower().replace(" ", "_")
    if v in MATERIALS:
        return v
    return material_from_text(str(value))


# ── Text cleaning: remove target leakage & marketplace boilerplate ──
# ~45% of Flipkart descriptions contain the selling price ("Buy X for Rs.379 online").
# A new artisan product never has that, so it must never be learned from.
_PRICE_PATTERNS = [
    r"(?:rs\.?|inr|mrp|₹|rupees?)\s*[:\-]?\s*\d[\d,]*(?:\.\d+)?(?:\s*/-)?",
    r"\d[\d,]*(?:\.\d+)?\s*(?:/-\s*)?(?:rs\.?|inr|rupees?|₹)(?![a-z])",
    r"\b(?:price|mrp|cost)\s*[:\-]\s*",
    r"\b\d{1,2}(?:\.\d+)?\s*%\s*(?:off|discount)\b",
    r"\bdiscount\s*[:\-]?\s*\d{1,2}(?:\.\d+)?\s*%",
]
_BOILERPLATE = [
    r"(?:at )?best prices? with free shipping & cash on delivery!?\.?",
    r"only genuine products\.?", r"30 day replacement guarantee\.?", r"free shipping\.?",
    r"cash on delivery!?", r"(?:online )?(?:at|on|from) (?:flipkart|amazon)(?:\.com|\.in)?",
    r"\bonly for\b(?=\s*[.,!]|\s*$)", r"\bflipkart(?:\.com)?\b", r"\bamazon(?:\.in|\.com)?\b", r"\bbest prices?\b",
]
_PRICE_RX = re.compile("|".join(_PRICE_PATTERNS), re.I)
_BOILER_RX = re.compile("|".join(_BOILERPLATE), re.I)
_WS_RX = re.compile(r"\s+")


def scrub_text(text: str | None) -> str:
    if not isinstance(text, str):
        return ""
    t = _PRICE_RX.sub(" ", text)
    t = _BOILER_RX.sub(" ", _BOILER_RX.sub(" ", t))   # twice: removals can expose new matches
    t = re.sub(r"\bfor\s+online\b", " online", t, flags=re.I)
    t = re.sub(r"\s+([.,;:!])", r"\1", _WS_RX.sub(" ", t))
    t = re.sub(r"([.,;:!&])(?:\s*[.,;:!&])+", r"\1", t)
    return t.strip()


def compose_text(title: str, description: str, max_chars: int = 1500) -> str:
    """Exactly the string the text encoder sees, at train and inference time."""
    title = scrub_text(title)
    desc = scrub_text(description)[:max_chars]
    return f"{title} | {desc}" if desc else title


# ── physical size and pack count, parsed from the listing text ───────────────
_SIZE_RX = re.compile(r"(\d+(?:\.\d+)?)\s*(cm\b|centimet|mm\b|inch|inches|\bin\b|\"|feet|foot|ft\b)", re.I)
_PACK_RX = re.compile(r"\b(?:pack|set|combo|lot)\s+of\s+(\d+)|\b(\d+)\s*(?:pcs|pieces|pc)\b", re.I)
_TO_CM = {"mm": 0.1, "in": 2.54, "inch": 2.54, "inches": 2.54, '"': 2.54, "feet": 30.48, "foot": 30.48, "ft": 30.48}


def parse_size_cm(*texts: str) -> float:
    """Largest stated dimension in centimetres, 0.0 when the text states none."""
    best = 0.0
    for t in texts:
        if not isinstance(t, str):
            continue
        for value, unit in _SIZE_RX.findall(t):
            u = unit.lower().strip()
            factor = next((v for k, v in _TO_CM.items() if u.startswith(k)), 1.0 if u.startswith(("cm", "centimet")) else None)
            if factor is None:
                continue
            cm = float(value) * factor
            if 0.5 <= cm <= 500:                      # ignore nonsense like "100 cm² of fabric care"
                best = max(best, cm)
    return round(best, 2)


def parse_pack_count(*texts: str) -> int:
    for t in texts:
        if not isinstance(t, str):
            continue
        m = _PACK_RX.search(t)
        if m:
            n = int(m.group(1) or m.group(2))
            if 1 <= n <= 100:
                return n
    return 1


def size_affinity(q_size: float, db_sizes: np.ndarray) -> np.ndarray:
    """
    Multiplier in (0, 1] that pulls comparables of a very different physical size down the ranking.
    Unknown size on either side → 1.0 (no opinion). A 2× size gap costs ~25 % of the similarity.
    """
    out = np.ones(len(db_sizes), dtype=np.float32)
    if not q_size:
        return out
    known = db_sizes > 0
    if not known.any():
        return out
    ratio = np.abs(np.log(np.maximum(db_sizes[known], 1e-3) / q_size))
    out[known] = (1.0 / (1.0 + 0.5 * ratio)).astype(np.float32)
    return out


def word_count(text: str | None) -> int:
    return len(text.split()) if isinstance(text, str) and text.strip() else 0


def normalise_subcategory(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return UNKNOWN
    return _WS_RX.sub(" ", value.strip().lower())


# ── Structured feature row (identical at train and inference) ──
def encode(vocab: dict[str, int], value: str) -> int:
    return vocab.get(value, 0)            # index 0 is reserved for unknown / "other"


def build_extra_row(comparable_median: float, mean_similarity: float,
                    top1_price: float = 0.0, iqr_ratio: float = 1.0) -> np.ndarray:
    return np.asarray([comparable_median, mean_similarity, top1_price, iqr_ratio], dtype=np.float32)


def lookup_category_stats(price_stats: dict, category: str) -> tuple[float, float]:
    s = price_stats["per_category"].get(category)
    if s is None:
        return price_stats["global_median"], price_stats["global_std"]
    return s["median_price"], s["price_std"]


def lookup_group_median(price_stats: dict, group: str, key: str) -> float:
    """Median price of a subcategory / material, from TRAIN rows only; global median if unseen."""
    return price_stats.get(group, {}).get(key, {}).get("median_price", price_stats["global_median"])


def build_structured_row(*, title: str, description: str, category: str, subcategory: str,
                         material: str, has_image: bool, encoders: dict,
                         category_stats: dict) -> np.ndarray:
    med, std = lookup_category_stats(category_stats, category)
    sub_key = f"{category}::{normalise_subcategory(subcategory)}"
    material_n = normalise_material(material)
    sub_med = lookup_group_median(category_stats, "per_subcategory", sub_key)
    mat_med = lookup_group_median(category_stats, "per_material", material_n)
    size_cm = parse_size_cm(title, description)
    row = [
        encode(encoders["category"], category),
        encode(encoders["subcategory"], sub_key),
        encode(encoders["material"], material_n),
        word_count(scrub_text(title)),
        word_count(scrub_text(description)),
        1.0 if has_image else 0.0,
        med,
        std,
        sub_med,
        mat_med,
        size_cm,
        1.0 if size_cm else 0.0,
        float(parse_pack_count(title, description)),
    ]
    return np.asarray(row, dtype=np.float32)


# ── Device + encoders ──
DEVICE_OVERRIDE = os.environ.get("B1_DEVICE", "auto").lower()   # auto | cpu | mps | cuda


def pick_device() -> str:
    """B1_DEVICE (or --device) wins; otherwise CUDA, then Apple MPS, then CPU."""
    import torch
    if DEVICE_OVERRIDE in ("cpu", "mps", "cuda"):
        if DEVICE_OVERRIDE == "cuda" and not torch.cuda.is_available():
            print("[device] B1_DEVICE=cuda but no CUDA GPU is visible to torch — falling back to CPU", flush=True)
            return "cpu"
        if DEVICE_OVERRIDE == "mps" and not torch.backends.mps.is_available():
            print("[device] B1_DEVICE=mps but MPS is unavailable — falling back to CPU", flush=True)
            return "cpu"
        return DEVICE_OVERRIDE
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def describe_device() -> str:
    import torch
    d = pick_device()
    if d == "cuda":
        return f"cuda ({torch.cuda.get_device_name(0)}, torch {torch.__version__})"
    return f"{d} (torch {torch.__version__})"


def resolve_clip_pretrained() -> str:
    """
    Turn the pretrained TAG into the cached file path when we can find it: that skips every
    HuggingFace Hub code path (no metadata request, no resolver, no download logic). We return the
    *snapshot* path rather than the blob it links to, because open_clip decides safetensors-vs-
    torch.load from the file extension and cache blobs have no name. Falls back to the tag.
    """
    if Path(CLIP_PRETRAINED).exists():
        return CLIP_PRETRAINED
    names = ("open_clip_model.safetensors", "open_clip_pytorch_model.bin",
             "model.safetensors", "pytorch_model.bin")
    hub = _hf_hub_dir()
    if hub.exists():
        for repo in sorted(hub.glob("models--*CLIP-ViT-B-32*")):
            for snap in sorted(repo.glob("snapshots/*")):
                for n in names:
                    f = snap / n                    # keep the symlink: its name carries the format
                    try:
                        if f.exists() and f.stat().st_size > 100_000_000:
                            return str(f)
                    except OSError:
                        pass
    return CLIP_PRETRAINED


def _slow_load_warning(what: str, cached: bool, seconds: float = 45.0):
    """
    If a load takes suspiciously long, print a hint AND dump the stack of every thread, so the exact
    line it is stuck on is visible instead of a blank terminal. Tune with B1_STACK_DUMP_SECONDS.
    """
    import faulthandler
    import threading
    seconds = float(os.environ.get("B1_STACK_DUMP_SECONDS", seconds))
    hint = (f"[encoder] {what} still loading after {seconds:.0f}s — weights are on disk, so this is not a "
            f"download. Stack dump of every thread follows; the deepest frame is where it is stuck. "
            f"Workarounds: B1_DEVICE=cpu (rules out Metal/MPS), B1_DISABLE_IMAGE_ENCODER=1 (text-only)."
            if cached else
            f"[encoder] {what} still downloading after {seconds:.0f}s — large download. Stack dump follows.")

    def fire():
        print(hint, flush=True)
        faulthandler.dump_traceback()          # all threads, to stderr

    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    return t


class ImageEncoder:
    """Frozen OpenCLIP ViT-B/32 → 512-d L2-normalised embedding."""

    def __init__(self, device: str | None = None):
        if os.environ.get("B1_DISABLE_IMAGE_ENCODER"):
            raise RuntimeError("image encoder disabled by B1_DISABLE_IMAGE_ENCODER")
        cached = clip_weights_cached()
        if cached:
            print(f"[encoder] loading CLIP ({CLIP_ARCH}) from cache"
                  f"{' (offline)' if os.environ.get('HF_HUB_OFFLINE') == '1' else ''} …", flush=True)
        else:
            print(f"[encoder] downloading CLIP weights ({CLIP_ARCH} / {CLIP_PRETRAINED}, ~600 MB) — "
                  f"first run only; this can take several minutes", flush=True)
        watchdog = _slow_load_warning("CLIP", cached)
        t0 = time.time()

        def step(msg):
            print(f"[encoder]   {msg}  (+{time.time() - t0:.1f}s)", flush=True)

        step("importing torch / open_clip")
        import open_clip
        import torch
        self.torch = torch
        self.device = device or pick_device()
        weights = resolve_clip_pretrained()
        step(f"device = {describe_device()}")
        step(f"weights = {weights}")
        step("building model + loading weights (open_clip.create_model_and_transforms)")
        try:
            self.model, _, self.preprocess = open_clip.create_model_and_transforms(
                CLIP_ARCH, pretrained=weights)
        except Exception as ex:
            if weights == CLIP_PRETRAINED:
                raise
            step(f"cached-file load failed ({type(ex).__name__}: {str(ex)[:120]}); "
                 f"retrying with the pretrained tag '{CLIP_PRETRAINED}'")
            os.environ.pop("HF_HUB_OFFLINE", None)
            self.model, _, self.preprocess = open_clip.create_model_and_transforms(
                CLIP_ARCH, pretrained=CLIP_PRETRAINED)
        step("moving model to device")
        self.model = self.model.to(self.device).eval()
        step("warm-up forward pass")
        with torch.no_grad():
            self.model.encode_image(torch.zeros(1, 3, 224, 224, device=self.device))
        self.version = f"open_clip:{CLIP_ARCH}:{Path(CLIP_PRETRAINED).name}"
        watchdog.cancel()
        print(f"[encoder] CLIP ready on {self.device}", flush=True)

    def encode(self, images: list, batch_size: int = 64) -> np.ndarray:
        """images: list of PIL.Image (RGB). Returns (n, 512) float32."""
        out = []
        with self.torch.no_grad():
            for i in range(0, len(images), batch_size):
                batch = self.torch.stack([self.preprocess(im) for im in images[i:i + batch_size]])
                emb = self.model.encode_image(batch.to(self.device)).float()
                emb = emb / emb.norm(dim=-1, keepdim=True).clamp_min(1e-12)
                out.append(emb.cpu().numpy())
        return np.concatenate(out).astype(np.float32) if out else np.zeros((0, IMG_DIM), np.float32)


class TextEncoder:
    """Frozen sentence-transformers all-MiniLM-L6-v2 → 384-d L2-normalised embedding."""

    def __init__(self, device: str | None = None):
        print(f"[encoder] loading text encoder ({Path(TEXT_MODEL).name}) …", flush=True)
        watchdog = _slow_load_warning("text encoder", text_weights_cached())
        t0 = time.time()

        def step(msg):
            print(f"[encoder]   {msg}  (+{time.time() - t0:.1f}s)", flush=True)

        step("importing sentence_transformers")
        from sentence_transformers import SentenceTransformer
        self.device = device or pick_device()
        step(f"device = {describe_device()}  model = {TEXT_MODEL}")
        self.model = SentenceTransformer(TEXT_MODEL, device=self.device)
        self.version = f"sentence-transformers:{Path(TEXT_MODEL).name}"
        watchdog.cancel()
        print(f"[encoder] text encoder ready on {self.device}", flush=True)

    def encode(self, texts: list[str], batch_size: int = 128) -> np.ndarray:
        emb = self.model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                convert_to_numpy=True, show_progress_bar=len(texts) > 1000)
        return emb.astype(np.float32)


def load_image(source):
    """Path / bytes / PIL → RGB PIL image (alpha flattened on white), or None if unreadable."""
    from PIL import Image, ImageOps
    try:
        if isinstance(source, Image.Image):
            im = source
        elif isinstance(source, (bytes, bytearray)):
            im = Image.open(io.BytesIO(source))
        else:
            im = Image.open(source)
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            return bg
        return im.convert("RGB")
    except Exception:
        return None


# ── Comparable similarity (used by train.py for evaluation and service.py at runtime) ──
def combined_similarity(q_img: np.ndarray, q_txt: np.ndarray, q_has_img: np.ndarray,
                        db_img: np.ndarray, db_txt: np.ndarray, db_has_img: np.ndarray,
                        w_img: float = SIM_W_IMAGE, w_txt: float = SIM_W_TEXT) -> np.ndarray:
    """
    Returns (n_query, n_db) similarity in [0, 1].
    score = w_img·cos(img) + w_txt·cos(txt) when BOTH sides have a photo;
    otherwise text-only (a missing photo is 'unknown', not 'dissimilar').
    """
    txt = np.clip(q_txt @ db_txt.T, 0.0, 1.0)
    img = np.clip(q_img @ db_img.T, 0.0, 1.0)
    both = np.outer(q_has_img.astype(bool), db_has_img.astype(bool))
    return np.where(both, (w_img * img + w_txt * txt) / (w_img + w_txt), txt).astype(np.float32)


def top_k_indices(sims_row: np.ndarray, k: int, db_categories: np.ndarray | None = None,
                  category: str | None = None, db_sizes: np.ndarray | None = None,
                  q_size: float = 0.0) -> np.ndarray:
    """
    Indices of the k most similar DB items, best first. If the query's category has at
    least k items in the DB, search is restricted to that category (a necklace should be
    priced against necklaces, not against a similar-looking cushion cover).
    """
    if db_sizes is not None and q_size:
        sims_row = sims_row * size_affinity(q_size, db_sizes)
    cand = np.arange(len(sims_row))
    if db_categories is not None and category is not None:
        same = np.flatnonzero(db_categories == category)
        if len(same) >= k:
            cand = same
    k = min(k, len(cand))
    part = cand[np.argpartition(-sims_row[cand], kth=k - 1)[:k]]
    return part[np.argsort(-sims_row[part], kind="stable")]


# ── Both encoders in their own process ───────────────────────────────────────
# On macOS, LightGBM/XGBoost and PyTorch each load their own OpenMP runtime. With both in one process
# the first torch op can deadlock or segfault (features.py alone never hits this, because it loads
# no booster). The cure is to keep torch out of the parent entirely: the child imports torch,
# open_clip and sentence-transformers and nothing else — exactly like the feature-extraction run.
def _encoder_worker(conn, device):
    try:
        conn.send(("ready", {"device": describe_device()}))
    except Exception as ex:
        conn.send(("error", f"{type(ex).__name__}: {ex}"))
        return
    img_enc = txt_enc = None
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        kind, payload = msg
        try:
            if kind == "image":
                img_enc = img_enc or ImageEncoder(device)
                conn.send(("ok", img_enc.encode(payload)))
            elif kind == "text":
                txt_enc = txt_enc or TextEncoder(device)
                conn.send(("ok", txt_enc.encode(payload)))
            else:
                conn.send(("error", f"unknown request {kind!r}"))
        except Exception as ex:
            conn.send(("error", f"{type(ex).__name__}: {ex}"))


class EncoderClient:
    """CLIP + MiniLM running in a child process; same encode() semantics, nothing torch in the parent."""

    def __init__(self, device: str | None = None, start_timeout: float = 120.0):
        import multiprocessing as mp
        self.device = device or os.environ.get("B1_DEVICE", "auto")
        ctx = mp.get_context("spawn")
        self._conn, child = ctx.Pipe()
        self._proc = ctx.Process(target=_encoder_worker, args=(child, None if self.device == "auto" else self.device),
                                 name="b1-encoder-worker", daemon=True)
        print("[encoder] starting encoder worker process (torch stays out of the service process) …", flush=True)
        self._proc.start()
        child.close()
        if not self._conn.poll(start_timeout):
            self._proc.terminate()
            raise TimeoutError(f"encoder worker did not start within {start_timeout:.0f}s")
        status, payload = self._conn.recv()
        if status == "error":
            self._proc.terminate()
            raise RuntimeError(payload)
        self.device_description = payload["device"]
        import atexit
        atexit.register(self.close)
        print(f"[encoder] worker ready (pid {self._proc.pid}, device {self.device_description})", flush=True)

    def _call(self, kind: str, payload, timeout: float) -> np.ndarray:
        if not self._proc.is_alive():
            raise RuntimeError("encoder worker died")
        self._conn.send((kind, payload))
        if not self._conn.poll(timeout):
            raise TimeoutError(f"encoder worker did not answer the {kind} request within {timeout:.0f}s")
        status, out = self._conn.recv()
        if status == "error":
            raise RuntimeError(out)
        return out

    def encode_image(self, images: list, timeout: float = 300.0) -> np.ndarray:
        return self._call("image", images, timeout)

    def encode_text(self, texts: list, timeout: float = 300.0) -> np.ndarray:
        return self._call("text", texts, timeout)

    def close(self):
        try:
            if self._proc.is_alive():
                self._conn.send(None)
                self._proc.join(5)
        except Exception:
            pass
        if self._proc.is_alive():
            self._proc.terminate()


class InProcessEncoders:
    """Same interface, models loaded in this process (fine everywhere except the macOS case above)."""

    def __init__(self, device: str | None = None):
        self.device = device
        self._img = self._txt = None
        self.device_description = describe_device()

    def encode_image(self, images: list, timeout: float = 0) -> np.ndarray:
        self._img = self._img or ImageEncoder(self.device)
        return self._img.encode(images)

    def encode_text(self, texts: list, timeout: float = 0) -> np.ndarray:
        self._txt = self._txt or TextEncoder(self.device)
        return self._txt.encode(texts)

    def close(self):
        pass


def use_encoder_worker() -> bool:
    """B1_ENCODER_WORKER: auto (separate process on macOS), 1 (always), 0 (never)."""
    mode = os.environ.get("B1_ENCODER_WORKER", os.environ.get("B1_IMAGE_WORKER", "auto")).lower()
    return mode == "1" or (mode == "auto" and sys.platform == "darwin")


def make_encoders(device: str | None = None):
    return EncoderClient(device) if use_encoder_worker() else InProcessEncoders(device)


# ═════════════════════════════════════════════════════════════════════════════
# EXTRACTION PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

def _candidate_urls(url: str) -> list[str]:
    """Original URL first; for 2016 Flipkart CDN paths also try the current CDN host."""
    if not isinstance(url, str) or not url.startswith("http"):
        return []
    cands = [url]
    if url.startswith("http://"):
        cands.append("https://" + url[len("http://"):])
    m = re.match(r"https?://img\d*[a-z]?\.flixcart\.com/image/(.+)$", url)
    if m:
        for host in ("rukminim1", "rukminim2"):
            cands.append(f"https://{host}.flixcart.com/image/416/416/{m.group(1)}")
    return cands


def _download_one(session, pid: str, url: str, timeout: float = 12.0) -> tuple[str, bool, str]:
    dest = IMAGES / f"{pid}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return pid, True, "cached"
    for cand in _candidate_urls(url):
        try:
            r = session.get(cand, timeout=timeout, headers=HTTP_HEADERS)
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
                continue
            im = load_image(r.content)
            if im is None or min(im.size) < 32:
                continue
            im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            tmp = dest.with_suffix(".tmp")
            im.save(tmp, "JPEG", quality=90)
            tmp.replace(dest)
            return pid, True, cand.split("/")[2]
        except Exception:
            continue
    return pid, False, "failed"


def download_images(df: pd.DataFrame, workers: int = 32) -> pd.Series:
    import requests
    from requests.adapters import HTTPAdapter
    IMAGES.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.mount("http://", HTTPAdapter(pool_maxsize=workers, max_retries=1))
    session.mount("https://", HTTPAdapter(pool_maxsize=workers, max_retries=1))
    ok: dict[str, bool] = {}
    hosts: dict[str, int] = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_download_one, session, pid, url)
                for pid, url in zip(df["pid"], df["image_url"])]
        for i, f in enumerate(as_completed(futs), 1):
            pid, success, how = f.result()
            ok[pid] = success
            hosts[how] = hosts.get(how, 0) + 1
            if i % 1000 == 0 or i == len(futs):
                print(f"  images {i}/{len(futs)}  ok={sum(ok.values())}  "
                      f"({time.time() - t0:.0f}s)  sources={hosts}", flush=True)
    return df["pid"].map(ok).fillna(False).astype(bool)


def fit_encoders(train: pd.DataFrame) -> dict:
    sub_key = train["category"] + "::" + train["subcategory"].map(normalise_subcategory)
    counts = sub_key.value_counts()
    kept = sorted(k for k, c in counts.items() if c >= MIN_SUBCATEGORY_COUNT
                  and not k.endswith("::" + UNKNOWN))
    return {
        "category": {c: i + 1 for i, c in enumerate(CATEGORIES)},
        "subcategory": {k: i + 1 for i, k in enumerate(kept)},
        "material": {m: i + 1 for i, m in enumerate(MATERIALS)},
        "unknown_index": 0,
        "subcategory_min_count": MIN_SUBCATEGORY_COUNT,
    }


MIN_GROUP_ROWS = 20          # below this a group's median is too noisy to use as an anchor


def fit_category_stats(train: pd.DataFrame) -> dict:
    """
    Price anchors by category, subcategory and material — computed on the TRAIN split only and
    saved, so validation, test and live inference all read the same numbers. Groups with fewer
    than MIN_GROUP_ROWS rows are left out and fall back to the global median.
    """
    g = train.groupby("category")["price"].agg(median_price="median", price_std="std")
    g["price_std"] = g["price_std"].fillna(0.0)
    stats = {
        "per_category": {c: {"median_price": float(r.median_price), "price_std": float(r.price_std),
                             "n": int((train["category"] == c).sum())}
                         for c, r in g.iterrows()},
        "global_median": float(train["price"].median()),
        "global_std": float(train["price"].std()),
        "fit_on": "train split only",
        "min_group_rows": MIN_GROUP_ROWS,
        "n_train_rows": int(len(train)),
    }
    sub_key = train["category"] + "::" + train["subcategory"].map(normalise_subcategory)
    for name, keys in (("per_subcategory", sub_key), ("per_material", train["material"])):
        grouped = train.groupby(keys)["price"].agg(median_price="median", n="count")
        stats[name] = {str(k): {"median_price": float(r.median_price), "n": int(r.n)}
                       for k, r in grouped.iterrows() if r.n >= MIN_GROUP_ROWS}
    return stats


def _ids_hash(ids) -> str:
    return hashlib.sha256("\n".join(map(str, ids)).encode()).hexdigest()[:16]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"],
                    help="override the compute device (same as B1_DEVICE)")
    ap.add_argument("--download-encoders", action="store_true",
                    help="only fetch the CLIP + text encoder weights (visible progress), then exit")
    ap.add_argument("--force", action="store_true", help="recompute embeddings even if cached")
    ap.add_argument("--no-images", action="store_true", help="skip image download")
    ap.add_argument("--workers", type=int, default=32, help="parallel image downloads")
    ap.add_argument("--image-batch", type=int, default=64)
    ap.add_argument("--text-batch", type=int, default=128)
    args = ap.parse_args()
    if args.device:
        globals()["DEVICE_OVERRIDE"] = args.device

    if args.download_encoders:
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
        t0 = time.time()
        ImageEncoder("cpu")
        TextEncoder("cpu")
        print(f"[features] encoder weights ready ({time.time() - t0:.0f}s) — they are cached for later runs")
        return

    if not PROCESSED.exists():
        sys.exit(f"{PROCESSED} not found — run `python data.py` first.")
    df = pd.read_pickle(PROCESSED).reset_index(drop=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    ids_hash = _ids_hash(df["pid"])
    manifest_path = CACHE / "features_manifest.json"
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    reuse = (not args.force) and old.get("ids_hash") == ids_hash
    print(f"[features] {len(df)} products  (ids hash {ids_hash}, reuse cache={reuse})")

    # 1) images ---------------------------------------------------------------
    if args.no_images:
        df["has_image"] = df["pid"].map(lambda p: (IMAGES / f"{p}.jpg").exists())
    else:
        print("[features] downloading product images …")
        df["has_image"] = download_images(df, workers=args.workers)
    print(f"[features] image coverage: {df['has_image'].mean():.1%} "
          f"({int(df['has_image'].sum())}/{len(df)})")

    device = pick_device()
    print(f"[features] device: {device}")

    # 2) image embeddings ------------------------------------------------------
    img_path = CACHE / "image_emb.npy"
    has_img_hash = _ids_hash(df["has_image"].astype(int))
    if reuse and img_path.exists() and old.get("has_image_hash") == has_img_hash \
            and old.get("image_encoder") == f"open_clip:{CLIP_ARCH}:{Path(CLIP_PRETRAINED).name}":
        image_emb = np.load(img_path)
        img_version = old["image_encoder"]
        print("[features] image embeddings: cached")
    else:
        image_emb = np.zeros((len(df), IMG_DIM), dtype=np.float32)
        idx = np.flatnonzero(df["has_image"].values)
        if len(idx) == 0:
            print("[features] WARNING: no product images available — image block is all zeros")
            enc, img_version = None, f"open_clip:{CLIP_ARCH}:{Path(CLIP_PRETRAINED).name}"
        else:
            enc = ImageEncoder(device)
            img_version = enc.version
        t0 = time.time()
        chunk = args.image_batch * 8
        for s in range(0, len(idx), chunk):
            part = idx[s:s + chunk]
            ims, keep = [], []
            for j in part:
                im = load_image(IMAGES / f"{df.at[j, 'pid']}.jpg")
                if im is not None:
                    ims.append(im)
                    keep.append(j)
                else:
                    df.at[j, "has_image"] = False
            if ims:
                image_emb[keep] = enc.encode(ims, batch_size=args.image_batch)
            print(f"  CLIP {min(s + chunk, len(idx))}/{len(idx)}  ({time.time() - t0:.0f}s)", flush=True)
        has_img_hash = _ids_hash(df["has_image"].astype(int))
        np.save(img_path, image_emb)
        del enc

    # 3) text embeddings -------------------------------------------------------
    txt_path = CACHE / "text_emb.npy"
    texts = [compose_text(t, d) for t, d in zip(df["title"], df["description"])]
    if reuse and txt_path.exists() and old.get("text_encoder") == f"sentence-transformers:{Path(TEXT_MODEL).name}":
        text_emb = np.load(txt_path)
        txt_version = old["text_encoder"]
        print("[features] text embeddings: cached")
    else:
        t0 = time.time()
        enc = TextEncoder(device)
        txt_version = enc.version
        text_emb = enc.encode(texts, batch_size=args.text_batch)
        np.save(txt_path, text_emb)
        print(f"[features] text embeddings done ({time.time() - t0:.0f}s)")
        del enc

    # 4) structured features (vocab + category stats fit on TRAIN only) --------
    train = df[df["split"] == "train"]
    encoders = fit_encoders(train)
    category_stats = fit_category_stats(train)
    struct = np.stack([
        build_structured_row(title=r.title, description=r.description, category=r.category,
                             subcategory=r.subcategory, material=r.material,
                             has_image=bool(r.has_image), encoders=encoders,
                             category_stats=category_stats)
        for r in df.itertuples(index=False)
    ])
    np.save(CACHE / "struct.npy", struct)
    joblib.dump(encoders, MODEL_DIR / "encoders.pkl")
    joblib.dump(category_stats, MODEL_DIR / "category_stats.pkl")

    meta_cols = ["pid", "source", "title", "category", "subcategory", "material",
                 "price", "split", "image_url", "has_image", "group"]
    df[meta_cols].to_pickle(CACHE / "meta.pkl")

    assert image_emb.shape == (len(df), IMG_DIM), image_emb.shape
    assert text_emb.shape == (len(df), TXT_DIM), text_emb.shape
    assert struct.shape == (len(df), len(STRUCT_FEATURES)), struct.shape
    assert np.isfinite(struct).all()

    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "n_rows": int(len(df)),
        "ids_hash": ids_hash,
        "has_image_hash": has_img_hash,
        "dims": {"image": IMG_DIM, "text": TXT_DIM, "structured": len(STRUCT_FEATURES),
                 "total": IMG_DIM + TXT_DIM + len(STRUCT_FEATURES)},
        "image_encoder": img_version,
        "text_encoder": txt_version,
        "device": device,
        "image_coverage": float(df["has_image"].mean()),
        "image_coverage_by_source": df.groupby("source")["has_image"].mean().round(4).to_dict(),
        "split_sizes": df["split"].value_counts().to_dict(),
        "n_subcategories_kept": len(encoders["subcategory"]),
        "structured_features": STRUCT_FEATURES,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    print("[features] done → data/cache/, models/price_model/{encoders,category_stats}.pkl")


if __name__ == "__main__":
    main()
