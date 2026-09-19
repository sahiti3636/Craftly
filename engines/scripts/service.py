"""
Craftly B1 — Step 4: runtime intelligence  (inference + business rules + API + demo)

    python service.py demo                      # end-to-end demo on a held-out real product
    python service.py demo --image photo.jpg --title "..." --description "..." \\
                              --category home_decor --material brass --material-cost 400 --labour-hours 10
    python service.py serve --port 8000         # same as: uvicorn service:app --port 8000

Pipeline for one product (ML and business logic strictly separated):
    1  ML market prediction       trained regressor on CLIP + MiniLM + structured features
    2  Comparable evidence        k-NN over the training index (same feature encoders)
    3  Market estimate            0.7 · model + 0.3 · comparable median
    4  Sustainable floor          (material + hours × wage) × (1 + overhead)
    5  Floor enforcement          base = max(market estimate, floor)
    6  Craft-passport premium     recommended = base × (1 + premium)
    7  Channel pricing            D2C / marketplace / B2B with a per-channel floor check
Plus: deterministic capacity-priority bulk allocation, craft passport + QR.

Endpoints
    POST /api/predict              multipart: image (optional file) + data (JSON string)
    POST /api/bulk-order           JSON
    POST /api/passport             JSON
    GET  /api/passport/{id}        passport JSON
    GET  /api/passport/{id}/qr     QR PNG
    GET  /api/model/info           version, metrics, taxonomy
    GET  /api/taxonomy             accepted categories / subcategories / materials
    GET  /api/health
"""
import argparse
import json
import math
import os
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import warnings

# Single-threaded OpenMP in the service: one prediction at a time needs no thread pool, and it removes
# the nested-parallelism deadlock between the booster's OpenMP and PyTorch's on macOS. Training is
# unaffected (train.py keeps all cores). Override with B1_OMP_THREADS.
os.environ.setdefault("OMP_NUM_THREADS", os.environ.get("B1_OMP_THREADS", "1"))
# The SERVICE runs the encoders on CPU by default. One prediction is milliseconds either way, and
# mixing Apple MPS with the booster libraries in one process has been seen to segfault. Training and
# feature extraction are untouched and still use MPS/CUDA. Override with --device mps / B1_DEVICE=mps.
os.environ.setdefault("B1_DEVICE", "cpu")

import features                        # noqa: F401 — sets OpenMP/HF env before anything heavy loads

import joblib
import numpy as np
import pandas as pd

from features import (CATEGORIES, CACHE, IMAGES, MATERIALS, MODEL_DIR, UNKNOWN,
                         build_extra_row, build_structured_row, clip_weights_cached,
                         combined_similarity, compose_text, load_image, make_encoders,
                         normalise_category, normalise_material, normalise_subcategory,
                         parse_pack_count, parse_size_cm, top_k_indices, use_encoder_worker)

# NOTE: this module deliberately never imports torch. When B1_ENCODER_WORKER is on (the default on
# macOS) both encoders live in a child process, so the parent holds only the booster's OpenMP runtime.

warnings.filterwarnings("ignore", message=".*does not have valid feature names.*")
ROOT = Path(__file__).resolve().parent.parent  # engines/ (this file lives in engines/scripts/)
PASSPORT_DIR = ROOT / "data" / "passports"
ARTISAN_ROSTER = ROOT / "demo_artisans.json"
PUBLIC_BASE_URL = os.environ.get("B1_PUBLIC_BASE_URL", "http://localhost:8000")

# ─────────────────────────────────────────────────────────────────────────────
# Business parameters — explicit rules, never learned
# ─────────────────────────────────────────────────────────────────────────────
HOURLY_WAGE_INR = 60.0             # default artisan wage per hour
OVERHEAD_PCT = 0.15                # tools, electricity, transport to market, wastage
PASSPORT_PREMIUM_PCT = 0.05        # verified-handmade premium
BLEND_MODEL_WEIGHT = 0.70          # market estimate = 0.7·model + 0.3·comparable median
K_COMPARABLES = 8
DISAGREEMENT_RATIO = 2.0           # flag when model and comparables differ by more than 2×
# Hard ceiling on bringing the encoders up, so a request can never block for ever.
ENCODER_TIMEOUT = float(os.environ.get("B1_ENCODER_TIMEOUT", os.environ.get("B1_IMAGE_ENCODER_TIMEOUT", 180)))

CHANNEL_PARAMS = {
    "d2c_packaging": 50.0, "d2c_payment_fee_pct": 0.025,
    "mkt_commission_pct": 0.15, "mkt_packaging": 80.0, "mkt_payment_fee_pct": 0.03,
    "b2b_wholesale_discount_pct": 0.20, "b2b_logistics_per_unit": 30.0,
}


def r2(x: float) -> float:
    return float(round(float(x), 2))


def retail_round(price: float, floor: float = 0.0) -> float:
    """A price an artisan can actually put on a label: ₹10 steps up to ₹1k, ₹50 to ₹5k, ₹100 above.
    Rounds up whenever rounding down would break the wage floor."""
    step = 10 if price < 1000 else 50 if price < 5000 else 100
    rounded = round(price / step) * step
    if rounded < floor:
        rounded = math.ceil(floor / step) * step
    return float(rounded)


def confidence_label(band: list, comparable_stats: dict, model_comparable_ratio: float,
                     has_image: bool) -> dict:
    """What the artisan app should show next to the number: high / medium / low, with reasons."""
    width_ratio = band[1] / max(band[0], 1.0)
    reasons, score = [], 0
    if width_ratio <= 3:
        score += 1
    else:
        reasons.append("wide price band for this kind of product")
    if comparable_stats["mean_similarity"] >= 0.55:
        score += 1
    else:
        reasons.append("no closely similar listings found")
    if model_comparable_ratio <= 1.5:
        score += 1
    else:
        reasons.append("model and comparable listings disagree")
    if has_image:
        score += 1
    else:
        reasons.append("no photo supplied")
    level = "high" if score >= 4 else "medium" if score >= 2 else "low"
    return {"level": level, "score": score, "max_score": 4, "reasons": reasons,
            "band_width_ratio": r2(width_ratio)}


# ═════════════════════════════════════════════════════════════════════════════
# BUSINESS RULES
# ═════════════════════════════════════════════════════════════════════════════
def sustainable_floor(material_cost: float, labour_hours: float,
                      hourly_wage: float = HOURLY_WAGE_INR, overhead_pct: float = OVERHEAD_PCT) -> dict:
    for name, v in [("material_cost", material_cost), ("labour_hours", labour_hours),
                    ("hourly_wage", hourly_wage), ("overhead_pct", overhead_pct)]:
        if v is None or v < 0 or not np.isfinite(v):
            raise ValueError(f"{name} must be a non-negative number")
    labour_cost = labour_hours * hourly_wage
    subtotal = material_cost + labour_cost
    overhead = overhead_pct * subtotal
    return {"material_cost": r2(material_cost), "labour_hours": r2(labour_hours),
            "hourly_wage": r2(hourly_wage), "labour_cost": r2(labour_cost),
            "subtotal": r2(subtotal), "overhead_pct": overhead_pct, "overhead": r2(overhead),
            "sustainable_floor": r2(subtotal + overhead)}


def enforce_floor(market_estimate: float, floor: float) -> dict:
    base = max(market_estimate, floor)
    return {"base_price": r2(base), "floor_binding": bool(floor > market_estimate),
            "gap_above_floor": r2(market_estimate - floor)}


def apply_passport_premium(base_price: float, pct: float = PASSPORT_PREMIUM_PCT,
                           has_passport: bool = True) -> dict:
    premium = pct * base_price if has_passport else 0.0
    return {"passport_premium_pct": pct if has_passport else 0.0,
            "passport_premium": r2(premium), "recommended_price": r2(base_price + premium)}


def channel_prices(recommended: float, floor: float, p: dict = CHANNEL_PARAMS) -> dict:
    """Buyer-facing price per channel, what the artisan nets, and a visible floor check."""
    d2c_fee = p["d2c_payment_fee_pct"] * recommended
    d2c_price = recommended + p["d2c_packaging"] + d2c_fee

    mkt_base = recommended / (1 - p["mkt_commission_pct"])
    mkt_fee = p["mkt_payment_fee_pct"] * mkt_base
    mkt_commission = p["mkt_commission_pct"] * mkt_base
    mkt_price = mkt_base + p["mkt_packaging"] + mkt_fee

    b2b_unit_uncapped = recommended * (1 - p["b2b_wholesale_discount_pct"])
    b2b_unit = max(b2b_unit_uncapped, floor)             # a wholesale discount may never cut into the wage floor
    b2b_price = b2b_unit + p["b2b_logistics_per_unit"]
    eff_discount = 1 - b2b_unit / recommended if recommended else 0.0

    def entry(price, net, **breakdown):
        return {"price": r2(price), "artisan_net": r2(net), "floor": r2(floor),
                "floor_ok": bool(net + 1e-6 >= floor), "breakdown": {k: r2(v) for k, v in breakdown.items()}}

    return {
        "d2c": entry(d2c_price, d2c_price - p["d2c_packaging"] - d2c_fee,
                     base=recommended, packaging=p["d2c_packaging"], payment_fee=d2c_fee),
        "marketplace": entry(mkt_price, mkt_price - p["mkt_packaging"] - mkt_fee - mkt_commission,
                             base_grossed_up=mkt_base, commission=mkt_commission,
                             packaging=p["mkt_packaging"], payment_fee=mkt_fee),
        "b2b": {**entry(b2b_price, b2b_unit, unit_before_logistics=b2b_unit,
                        logistics=p["b2b_logistics_per_unit"]),
                "wholesale_discount_pct": p["b2b_wholesale_discount_pct"],
                "effective_discount_pct": round(eff_discount, 4),
                "discount_capped_by_floor": bool(b2b_unit_uncapped < floor)},
    }


def allocate_bulk_order(quantity: int, artisans: list[dict], product_category: str,
                        unit_payout: float | None = None, deadline_days: int | None = None) -> dict:
    """
    Deterministic Capacity-Priority Allocation (capacity pooling + order splitting).
      1 filter artisans whose compatible_categories contain the product category (capacity > 0)
      2 cap each artisan's capacity at what their daily rate can deliver by `deadline_days`
      3 sort by effective capacity desc, then artisan id asc
      4 greedily give each min(effective capacity, remaining)
      5 report allocations, per-artisan ETA, shortfall, and the payout split
    """
    if int(quantity) != quantity or quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    quantity = int(quantity)
    eligible, excluded = [], []
    for a in artisans:
        cap = int(a.get("capacity", 0))
        if cap < 0:
            raise ValueError(f"artisan {a.get('id')} has negative capacity")
        rate = float(a.get("units_per_day") or 0)
        effective = cap
        if deadline_days and rate > 0:
            effective = min(cap, int(rate * deadline_days))
        if product_category not in a.get("compatible_categories", []):
            excluded.append({"artisan_id": a["id"], "reason": "category not compatible"})
        elif cap == 0:
            excluded.append({"artisan_id": a["id"], "reason": "no capacity"})
        elif effective == 0:
            excluded.append({"artisan_id": a["id"],
                             "reason": f"cannot deliver any unit within {deadline_days} days"})
        else:
            eligible.append({**a, "capacity": cap, "effective_capacity": effective, "units_per_day": rate})
    eligible.sort(key=lambda a: (-a["effective_capacity"], str(a["id"])))

    remaining, allocations = quantity, []
    for a in eligible:
        if remaining == 0:
            break
        units = min(a["effective_capacity"], remaining)
        remaining -= units
        row = {"artisan_id": a["id"], "name": a.get("name"), "region": a.get("region"),
               "units": units, "capacity": a["capacity"],
               "effective_capacity": a["effective_capacity"],
               "utilisation_pct": round(100 * units / a["capacity"], 1)}
        if a["units_per_day"] > 0:
            row["eta_days"] = int(math.ceil(units / a["units_per_day"]))
        if unit_payout is not None:
            row["payout"] = r2(units * unit_payout)
        allocations.append(row)
    allocated = quantity - remaining
    for row in allocations:
        row["share_pct"] = round(100 * row["units"] / allocated, 2) if allocated else 0.0
    etas = [r["eta_days"] for r in allocations if "eta_days" in r]
    return {"algorithm": "deterministic capacity-priority allocation",
            "product_category": product_category, "requested": quantity,
            "deadline_days": deadline_days,
            "estimated_days_to_complete": max(etas) if etas else None,
            "meets_deadline": (max(etas) <= deadline_days) if (etas and deadline_days) else None,
            "allocated": allocated, "shortfall": remaining, "fully_met": remaining == 0,
            "eligible_artisans": len(eligible),
            "total_eligible_capacity": sum(a["capacity"] for a in eligible),
            "total_capacity_within_deadline": sum(a["effective_capacity"] for a in eligible),
            "unit_payout": r2(unit_payout) if unit_payout is not None else None,
            "total_payout": r2(allocated * unit_payout) if unit_payout is not None else None,
            "allocations": allocations, "excluded": excluded}


# ═════════════════════════════════════════════════════════════════════════════
# CRAFT PASSPORT + QR
# ═════════════════════════════════════════════════════════════════════════════
_passport_lock = threading.Lock()


def create_passport(product: dict, artisan: dict, pricing: dict, provenance: dict | None = None) -> dict:
    import qrcode
    PASSPORT_DIR.mkdir(parents=True, exist_ok=True)
    with _passport_lock:
        seq = len(list(PASSPORT_DIR.glob("CP-*.json"))) + 1
        pid = f"CP-{time.strftime('%Y%m%d%H%M%S')}-{seq:04d}"
        prov = ({"available": True, **provenance} if provenance
                else {"available": False, "note": "Provenance not provided by artisan"})
        passport = {
            "passport_id": pid,
            "product": {k: product.get(k) for k in
                        ("title", "category", "subcategory", "material", "making_time_hours", "description")},
            "artisan": {k: artisan.get(k) for k in ("id", "name", "region", "craft_cluster")},
            "pricing": {k: pricing.get(k) for k in
                        ("ai_market_price", "sustainable_floor", "recommended_price", "model_version")},
            "provenance": prov,
            "verify_url": f"{PUBLIC_BASE_URL}/api/passport/{pid}",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        (PASSPORT_DIR / f"{pid}.json").write_text(json.dumps(passport, indent=2, ensure_ascii=False))
        img = qrcode.make(passport["verify_url"])
        img.save(PASSPORT_DIR / f"{pid}.png")
    qr = PASSPORT_DIR / f"{pid}.png"
    passport["qr_path"] = str(qr.relative_to(ROOT)) if qr.is_relative_to(ROOT) else str(qr)
    return passport


def load_passport(passport_id: str) -> dict | None:
    if not passport_id.startswith("CP-") or "/" in passport_id or ".." in passport_id:
        return None
    f = PASSPORT_DIR / f"{passport_id}.json"
    return json.loads(f.read_text()) if f.exists() else None


# ═════════════════════════════════════════════════════════════════════════════
# ML INFERENCE
# ═════════════════════════════════════════════════════════════════════════════
class PricingEngine:
    def __init__(self, model_dir: Path = MODEL_DIR, device: str | None = None):
        if not (model_dir / "model.pkl").exists():
            raise FileNotFoundError(f"{model_dir}/model.pkl missing — run data.py, features.py, train.py")
        bundle = joblib.load(model_dir / "model.pkl")
        # bundle v2: an ensemble of members + post-hoc calibration + retrieval features.
        # v1 bundles (a single model, 904 features, no calibration) still load unchanged.
        self.members = bundle.get("members") or [(bundle["model"], bundle["target_transform"])]
        self.calibration = bundle.get("calibration")
        self.extra_features = bundle.get("extra_features") or []
        self.blend_weight = float(bundle.get("blend_model_weight", BLEND_MODEL_WEIGHT))
        self.transform = bundle["target_transform"]
        self.band_lo, self.band_hi = bundle["band_lo"], bundle["band_hi"]
        self.band_offset = float(bundle.get("band_log_offset", 0.0))
        self.version = bundle["version"]
        self.algo = bundle["algo"]
        self.encoders = joblib.load(model_dir / "encoders.pkl")
        self.category_stats = joblib.load(model_dir / "category_stats.pkl")
        self.metadata = json.loads((model_dir / "metadata.json").read_text())
        self.metrics = json.loads((model_dir / "metrics.json").read_text())
        self.idx_img = np.load(model_dir / "index_img.npy")
        self.idx_txt = np.load(model_dir / "index_txt.npy")
        self.idx_meta = pd.read_pickle(model_dir / "index_meta.pkl")
        self.idx_has = self.idx_meta["has_image"].to_numpy(bool)
        self.idx_cat = self.idx_meta["category"].to_numpy()
        if "size_cm" not in self.idx_meta.columns:      # index built before size parsing existed
            self.idx_meta["size_cm"] = [parse_size_cm(t) for t in self.idx_meta["title"]]
        self.idx_size = self.idx_meta["size_cm"].to_numpy(float)
        self.idx_price = self.idx_meta["price"].to_numpy(float)
        self.device = device or os.environ.get("B1_DEVICE", "cpu")
        self._encoders = None
        self._encoder_error = None
        self._image_error = None
        self._lock = threading.Lock()
        print(f"[engine] model {self.version} loaded; encoders run "
              f"{'in a worker process' if use_encoder_worker() else 'in this process'} "
              f"on device '{self.device}'", flush=True)
        if not clip_weights_cached():
            print("[engine] note: CLIP weights are not in the local cache yet. The first request with a "
                  "photo downloads ~600 MB. Run `python features.py --download-encoders` to do it "
                  "up front, or set B1_DISABLE_IMAGE_ENCODER=1 to price on text only.", flush=True)

    # ── encoders (loaded on first use, with a timeout so a request never blocks for ever) ──
    def encoder_client(self):
        """The CLIP+MiniLM runner (worker process or in-process). Not to be confused with
        self.encoders, which holds the label vocabularies."""
        with self._lock:
            if self._encoders is None and self._encoder_error is None:
                box = {}

                def build():
                    try:
                        box["enc"] = make_encoders(None if self.device == "auto" else self.device)
                    except Exception as ex:
                        box["err"] = f"{type(ex).__name__}: {ex}"

                th = threading.Thread(target=build, name="encoder-load", daemon=True)
                th.start()
                th.join(ENCODER_TIMEOUT)
                if th.is_alive():
                    self._encoder_error = f"timed out after {ENCODER_TIMEOUT:.0f}s"
                else:
                    self._encoder_error, self._encoders = box.get("err"), box.get("enc")
                if self._encoder_error:
                    print(f"[engine] encoders unavailable ({self._encoder_error})", flush=True)
            return self._encoders

    def encode_text(self, texts: list) -> np.ndarray:
        enc = self.encoder_client()
        if enc is None:
            raise RuntimeError(f"text encoder unavailable: {self._encoder_error}")
        return enc.encode_text(texts)

    def encode_image(self, images: list):
        """Returns None instead of failing the request when the image branch cannot run."""
        enc = self.encoder_client()
        if enc is None:
            self._image_error = self._encoder_error
            return None
        try:
            return enc.encode_image(images)
        except Exception as ex:
            self._image_error = f"{type(ex).__name__}: {ex}"
            print(f"[engine] image encoding failed ({self._image_error}) — "
                  f"pricing continues on text + attributes only", flush=True)
            return None

    def taxonomy(self) -> dict:
        subs: dict[str, list[str]] = {c: [] for c in CATEGORIES}
        for key in self.encoders["subcategory"]:
            c, s = key.split("::", 1)
            subs.setdefault(c, []).append(s)
        return {"categories": CATEGORIES, "subcategories": subs, "materials": MATERIALS + [UNKNOWN],
                "note": "unknown subcategory/material values are accepted and encoded as 'other'"}

    def _raw_predict(self, X: np.ndarray, category: str | None = None) -> float:
        """Geometric mean over the ensemble members, then the validation-fitted bias correction
        (the category's own calibration line when one was fitted, otherwise the global one)."""
        logs = []
        for model, transform in self.members:
            p = float(model.predict(X)[0])
            logs.append(p if transform == "log" else np.log(max(p, 1.0)))
        price = float(np.exp(np.mean(logs)))
        cal = self.calibration
        if cal:
            line = (cal.get("per_category", {}).get(category) or cal.get("global")
                    or {"a": cal.get("a", 0.0), "b": cal.get("b", 1.0)})
            price = float(np.exp(line["a"] + line["b"] * np.log(max(price, 1.0))))
        return price

    def featurize(self, *, image, title: str, description: str, category: str,
                  subcategory: str | None, material: str | None) -> dict:
        norm_category = normalise_category(category)
        if norm_category is None:
            raise ValueError(f"category must be one of {CATEGORIES} (got {category!r})")
        category = norm_category
        if not title or not title.strip():
            raise ValueError("title is required")
        pil = load_image(image) if image is not None else None
        emb = self.encode_image([pil]) if pil is not None else None
        has_image = emb is not None
        img_emb = emb[0] if has_image else np.zeros(self.idx_img.shape[1], np.float32)
        txt_emb = self.encode_text([compose_text(title, description or "")])[0]
        material_n = normalise_material(material)
        struct = build_structured_row(title=title, description=description or "", category=category,
                                      subcategory=subcategory or UNKNOWN, material=material_n,
                                      has_image=has_image, encoders=self.encoders,
                                      category_stats=self.category_stats)
        # the retrieval block needs the comparables, so they are searched before the model runs
        size_cm = parse_size_cm(title, description or "")
        comp = self.comparables(img_emb, txt_emb, has_image, category, size_cm=size_cm)
        parts = [img_emb, txt_emb, struct]
        if self.extra_features:
            st = comp["stats"]
            parts.append(build_extra_row(st["median"], st["mean_similarity"],
                                         comp["items"][0]["price"] if comp["items"] else st["median"],
                                         st["iqr"][1] / max(st["iqr"][0], 1.0)))
        X = np.concatenate(parts)[None, :].astype(np.float32)
        sub_key = f"{category}::{normalise_subcategory(subcategory)}"
        return {"X": X, "img": img_emb, "txt": txt_emb, "has_image": has_image,
                "material": material_n, "comparables": comp, "size_cm": size_cm,
                "pack_count": parse_pack_count(title, description or ""),
                "subcategory_known": sub_key in self.encoders["subcategory"],
                "image_encoder_error": self._image_error if pil is not None and not has_image else None,
                "dims": {"image": int(img_emb.shape[0]), "text": int(txt_emb.shape[0]),
                         "structured": int(struct.shape[0]),
                         "retrieval": len(self.extra_features), "total": int(X.shape[1])}}

    def market_price(self, X: np.ndarray, category: str | None = None) -> dict:
        price = self._raw_predict(X, category)
        a, b = float(self.band_lo.predict(X)[0]), float(self.band_hi.predict(X)[0])
        lo, hi = np.exp(min(a, b) - self.band_offset), np.exp(max(a, b) + self.band_offset)
        # the band comes from separate quantile models; never show a band that excludes the point estimate
        lo, hi = min(lo, price), max(hi, price)
        return {"ai_market_price": r2(price), "price_band_80pct": [r2(lo), r2(hi)]}

    def comparables(self, img_emb, txt_emb, has_image: bool, category: str, k: int = K_COMPARABLES,
                    size_cm: float = 0.0) -> dict:
        sims = combined_similarity(img_emb[None, :], txt_emb[None, :], np.array([has_image]),
                                   self.idx_img, self.idx_txt, self.idx_has)[0]
        top = top_k_indices(sims, k, self.idx_cat, category, self.idx_size, size_cm)
        rows = self.idx_meta.iloc[top]
        items = [{"title": r.title, "category": r.category, "subcategory": r.subcategory,
                  "material": r.material, "size_cm": r2(r.size_cm), "price": r2(r.price),
                  "marketplace": r.source,
                  "similarity": round(float(sims[i]), 4), "image_url": r.image_url}
                 for i, r in zip(top, rows.itertuples(index=False))]
        prices = rows["price"].to_numpy(float)
        stats = {"n": int(len(prices)), "median": r2(np.median(prices)), "mean": r2(prices.mean()),
                 "min": r2(prices.min()), "max": r2(prices.max()),
                 "std": r2(prices.std(ddof=1)) if len(prices) > 1 else 0.0,
                 "iqr": [r2(np.percentile(prices, 25)), r2(np.percentile(prices, 75))],
                 "mean_similarity": round(float(sims[top].mean()), 4),
                 "size_aware": bool(size_cm),
                 "searched_within": category if (self.idx_cat == category).sum() >= k else "all categories"}
        return {"items": items, "stats": stats}

    def recommend(self, *, image=None, title: str, description: str = "", category: str,
                  subcategory: str | None = None, material: str | None = None,
                  material_cost: float, labour_hours: float,
                  hourly_wage: float = HOURLY_WAGE_INR, overhead_pct: float = OVERHEAD_PCT,
                  with_passport_premium: bool = True) -> dict:
        t0 = time.time()
        norm_category = normalise_category(category)
        if norm_category is None:
            raise ValueError(f"category must be one of {CATEGORIES} (got {category!r})")
        category = norm_category
        f = self.featurize(image=image, title=title, description=description, category=category,
                           subcategory=subcategory, material=material)
        # 1 comparables (already retrieved while featurising — they are model inputs now)
        comp = f["comparables"]
        # 2 ML
        ml = self.market_price(f["X"], category)
        # 3 blend, with the weight chosen on validation data at training time —
        # but leaned further toward the comparables when the model and the real comparable sales
        # disagree sharply AND the comparables are themselves trustworthy (similar enough listings,
        # not noise). The comparables are actual transacted prices; a model this far from them is
        # more likely extrapolating on an unusual item than the market itself being wrong. The
        # weight never drops below half its trained value, so a validated, calibrated model still
        # anchors the estimate rather than being discarded outright.
        w = self.blend_weight
        ratio = max(ml["ai_market_price"], comp["stats"]["median"]) / max(
            1.0, min(ml["ai_market_price"], comp["stats"]["median"]))
        w_eff = w
        if ratio > DISAGREEMENT_RATIO and comp["stats"]["mean_similarity"] >= 0.4:
            excess = min(ratio - DISAGREEMENT_RATIO, DISAGREEMENT_RATIO)   # cap the adjustment
            w_eff = max(w / 2, w - 0.15 * excess)
        market_estimate = w_eff * ml["ai_market_price"] + (1 - w_eff) * comp["stats"]["median"]
        # 4–7 business rules
        floor = sustainable_floor(material_cost, labour_hours, hourly_wage, overhead_pct)
        fl = enforce_floor(market_estimate, floor["sustainable_floor"])
        prem = apply_passport_premium(fl["base_price"], has_passport=with_passport_premium)
        channels = channel_prices(prem["recommended_price"], floor["sustainable_floor"])
        rounded = retail_round(prem["recommended_price"], floor["sustainable_floor"])
        confidence = confidence_label(ml["price_band_80pct"], comp["stats"], ratio, f["has_image"])

        warnings = []
        if f.get("image_encoder_error"):
            warnings.append(f"Photo received but the image encoder could not be loaded "
                            f"({f['image_encoder_error']}) — priced on text and attributes only.")
        elif not f["has_image"]:
            warnings.append("No usable photo — price is based on text and attributes only.")
        if ratio > DISAGREEMENT_RATIO:
            note = (" The estimate below already leans toward the comparables to compensate."
                    if w_eff != w else "")
            warnings.append(f"Model and comparable listings disagree by {ratio:.1f}× — "
                            f"review the price manually.{note}")
        if not f["subcategory_known"]:
            warnings.append("Subcategory not in the training vocabulary — treated as 'other'.")
        if fl["floor_binding"]:
            warnings.append("Market prices for similar items are below this artisan's sustainable cost; "
                            "the floor price was used. Consider a premium channel (D2C / passport buyers).")

        explanation = [
            f"The {self.algo} model ({self.transform} target) estimates a market price of "
            f"₹{ml['ai_market_price']:,.0f} (80% band ₹{ml['price_band_80pct'][0]:,.0f}–₹{ml['price_band_80pct'][1]:,.0f}).",
            f"{comp['stats']['n']} similar products ({comp['stats']['searched_within']}) sell for a median of "
            f"₹{comp['stats']['median']:,.0f} (middle half ₹{comp['stats']['iqr'][0]:,.0f}–₹{comp['stats']['iqr'][1]:,.0f}).",
            (f"Model and comparables disagree {ratio:.1f}× — leaning more on the {comp['stats']['n']} real "
             f"comparable sales: blended {w_eff:.0%} model + {1 - w_eff:.0%} comparables = "
             f"₹{market_estimate:,.0f} (trained split is {w:.0%}/{1 - w:.0%})."
             if w_eff != w else
             f"Market estimate ₹{market_estimate:,.0f}: the comparables already feed the model as "
             f"features, and blending them in again did not help on validation data."
             if w >= 0.999 else
             f"Blended market estimate: {w:.0%} model + {1 - w:.0%} comparables = ₹{market_estimate:,.0f}."),
            f"Sustainable floor: materials ₹{floor['material_cost']:,.0f} + {floor['labour_hours']:g} h × "
            f"₹{floor['hourly_wage']:,.0f} = ₹{floor['subtotal']:,.0f}, plus {overhead_pct:.0%} overhead "
            f"= ₹{floor['sustainable_floor']:,.0f}.",
            ("The floor is higher than the market estimate, so the floor is used."
             if fl["floor_binding"] else
             f"The market estimate is ₹{fl['gap_above_floor']:,.0f} above the floor, so the market estimate is used."),
            (f"Craft passport premium +{prem['passport_premium_pct']:.0%} → recommended price "
             f"₹{prem['recommended_price']:,.0f}." if with_passport_premium else
             f"Recommended price ₹{prem['recommended_price']:,.0f} (no passport premium)."),
            f"Shown to the artisan as ₹{rounded:,.0f} (rounded to a sellable price, never below the floor). "
            f"Confidence: {confidence['level']}"
            + (f" — {'; '.join(confidence['reasons'])}." if confidence["reasons"] else "."),
        ]
        return {
            "input": {"title": title, "category": category, "subcategory": subcategory,
                      "material": f["material"], "has_image": f["has_image"],
                      "size_cm": f["size_cm"], "pack_count": f["pack_count"],
                      "material_cost": material_cost, "labour_hours": labour_hours},
            "features": f["dims"],
            "model": {"version": self.version, "algo": self.algo, "target_transform": self.transform,
                      "ensemble_members": len(self.members), "calibrated": bool(self.calibration),
                      "blend_model_weight": w, "blend_model_weight_used": r2(w_eff)},
            "ai_market_price": ml["ai_market_price"],
            "price_band_80pct": ml["price_band_80pct"],
            "comparables": comp["items"],
            "comparable_stats": comp["stats"],
            "market_estimate": r2(market_estimate),
            "model_comparable_ratio": r2(ratio),
            "floor": floor,
            "floor_enforcement": fl,
            "passport_premium": prem,
            "recommended_price": prem["recommended_price"],
            "recommended_price_rounded": rounded,
            "confidence": confidence,
            "channels": channels,
            "explanation": explanation,
            "warnings": warnings,
            "latency_ms": int((time.time() - t0) * 1000),
        }


@lru_cache(maxsize=1)
def get_engine() -> PricingEngine:
    return PricingEngine()


def load_roster() -> list[dict]:
    return json.loads(ARTISAN_ROSTER.read_text())["artisans"]


# ═════════════════════════════════════════════════════════════════════════════
# FASTAPI
# ═════════════════════════════════════════════════════════════════════════════
def create_app():
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse
    from pydantic import BaseModel, Field, ValidationError

    class ProductInput(BaseModel):
        title: str = Field(min_length=1)
        description: str = ""
        category: str
        subcategory: str | None = None
        material: str | None = None
        material_cost: float = Field(ge=0)
        labour_hours: float = Field(ge=0)
        hourly_wage: float = Field(default=HOURLY_WAGE_INR, ge=0)
        overhead_pct: float = Field(default=OVERHEAD_PCT, ge=0, le=1)
        with_passport_premium: bool = True

    class Artisan(BaseModel):
        id: str
        name: str | None = None
        capacity: int = Field(ge=0)
        compatible_categories: list[str]
        region: str | None = None
        units_per_day: float | None = Field(default=None, ge=0)

    class BulkOrder(BaseModel):
        product_category: str
        quantity: int = Field(gt=0)
        artisans: list[Artisan] | None = None      # omitted → demo roster
        unit_payout: float | None = Field(default=None, ge=0)
        deadline_days: int | None = Field(default=None, gt=0)

    class PassportRequest(BaseModel):
        product: dict[str, Any]
        artisan: dict[str, Any]
        pricing: dict[str, Any]
        provenance: dict[str, Any] | None = None

    app = FastAPI(title="Craftly B1 — pricing intelligence", version="1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/", include_in_schema=False)
    def root():
        """The interactive API docs are the useful landing page here."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/docs")

    @app.get("/api/health")
    def health():
        ok = (MODEL_DIR / "model.pkl").exists()
        return {"status": "ok" if ok else "model_missing", "model_artifacts_present": ok,
                "engine_loaded": get_engine.cache_info().currsize > 0}

    @app.get("/api/model/info")
    def model_info():
        try:
            e = get_engine()
        except FileNotFoundError as ex:
            raise HTTPException(503, str(ex))
        return {"metadata": e.metadata, "metrics": e.metrics, "taxonomy": e.taxonomy(),
                "business_parameters": {"hourly_wage_inr": HOURLY_WAGE_INR, "overhead_pct": OVERHEAD_PCT,
                                        "passport_premium_pct": PASSPORT_PREMIUM_PCT,
                                        "blend_model_weight": e.blend_weight,
                                        "k_comparables": K_COMPARABLES, **CHANNEL_PARAMS}}

    @app.get("/api/taxonomy")
    def taxonomy():
        return get_engine().taxonomy()

    @app.post("/api/predict")
    async def predict(data: str = Form(..., description="JSON: title, description, category, "
                                                        "subcategory, material, material_cost, labour_hours"),
                      image: UploadFile | None = File(None)):
        try:
            inp = ProductInput(**json.loads(data))
        except (json.JSONDecodeError, ValidationError) as ex:
            raise HTTPException(422, f"invalid data field: {ex}")
        img_bytes = await image.read() if image is not None else None
        if img_bytes is not None and load_image(img_bytes) is None:
            raise HTTPException(422, "image could not be decoded")
        try:
            return get_engine().recommend(image=img_bytes, **inp.model_dump())
        except ValueError as ex:
            raise HTTPException(422, str(ex))
        except FileNotFoundError as ex:
            raise HTTPException(503, str(ex))

    @app.post("/api/bulk-order")
    def bulk_order(req: BulkOrder):
        if req.product_category not in CATEGORIES:
            raise HTTPException(422, f"product_category must be one of {CATEGORIES}")
        artisans = [a.model_dump() for a in req.artisans] if req.artisans is not None else load_roster()
        try:
            return allocate_bulk_order(req.quantity, artisans, req.product_category,
                                       req.unit_payout, req.deadline_days)
        except ValueError as ex:
            raise HTTPException(422, str(ex))

    @app.post("/api/passport")
    def passport_create(req: PassportRequest):
        p = create_passport(req.product, req.artisan, req.pricing, req.provenance)
        p["qr_url"] = f"{PUBLIC_BASE_URL}/api/passport/{p['passport_id']}/qr"
        return p

    @app.get("/api/passport/{passport_id}")
    def passport_get(passport_id: str):
        p = load_passport(passport_id)
        if p is None:
            raise HTTPException(404, "passport not found")
        return p

    @app.get("/api/passport/{passport_id}/qr")
    def passport_qr(passport_id: str):
        if load_passport(passport_id) is None:
            raise HTTPException(404, "passport not found")
        return FileResponse(PASSPORT_DIR / f"{passport_id}.png", media_type="image/png")

    # ── Thin aliases for C1's HttpPriceEngine/HttpOrderEngine adapter
    #    (market/app/adapters/http_platform.py), which POST to /price and /split
    #    with PriceQuote/SplitPlan shapes (market/app/contracts.py) rather than
    #    the native /api/predict and /api/bulk-order ones above. ──
    class ListingIn(BaseModel):
        listing_id: str
        title_en: str | None = None
        title_hi: str | None = None
        description_en: str | None = None
        description_hi: str | None = None
        category: str | None = None
        craft_type: str | None = None
        material: str | None = None
        material_cost_inr: float | None = None
        hours_worked: float | None = None

    class ArtisanIn(BaseModel):
        artisan_id: str
        name: str | None = None
        village: str | None = None
        cluster_id: str | None = None

    class InventoryIn(BaseModel):
        listing_id: str | None = None

    class PriceRequest(BaseModel):
        listing: ListingIn
        artisan: ArtisanIn
        inventory: InventoryIn | None = None
        channel: str = "own_store"
        quantity: int = 1

    class SplitRequest(BaseModel):
        listing: ListingIn
        artisan: ArtisanIn
        inventory: InventoryIn | None = None
        quantity: int = Field(gt=0)
        deadline_days: int | None = None

    CHANNEL_TO_ENGINE = {"own_store": "d2c", "mela_qr": "d2c", "b2b": "b2b",
                        "amazon": "marketplace", "flipkart": "marketplace", "ebay": "marketplace"}

    def _recommend_for_listing(listing: "ListingIn") -> tuple[dict, bool]:
        # category validation/normalisation happens inside recommend() itself (features.normalise_category),
        # so a listing category that is a close synonym of B1's taxonomy (e.g. another slice sending
        # "jewelry" or "decor") still resolves correctly instead of failing on an exact-string mismatch.
        title = listing.title_en or listing.title_hi
        if not title:
            raise HTTPException(422, "listing.title_en or listing.title_hi is required")
        floor_incomplete = listing.material_cost_inr is None or listing.hours_worked is None
        try:
            res = get_engine().recommend(
                title=title, description=listing.description_en or listing.description_hi or "",
                category=listing.category, subcategory=listing.craft_type, material=listing.material,
                material_cost=float(listing.material_cost_inr or 0),
                labour_hours=float(listing.hours_worked or 0))
        except ValueError as ex:
            raise HTTPException(422, str(ex))
        except FileNotFoundError as ex:
            raise HTTPException(503, str(ex))
        return res, floor_incomplete

    @app.post("/price")
    def price_quote(req: PriceRequest):
        res, floor_incomplete = _recommend_for_listing(req.listing)
        engine_channel = CHANNEL_TO_ENGINE.get(req.channel, "d2c")
        ch = res["channels"][engine_channel]
        return {
            "listing_id": req.listing.listing_id,
            "channel": req.channel,
            "floor_inr": int(round(res["floor"]["sustainable_floor"])),
            "market_low_inr": int(round(res["price_band_80pct"][0])),
            "market_high_inr": int(round(res["price_band_80pct"][1])),
            "passport_premium_inr": int(round(res["passport_premium"]["passport_premium"])),
            "price_inr": int(round(ch["price"])),
            "artisan_take_home_inr": int(round(ch["artisan_net"])),
            "channel_fee_inr": int(round(ch["price"] - ch["artisan_net"])),
            "below_floor": res["floor_enforcement"]["floor_binding"],
            "floor_incomplete": floor_incomplete,
            "explanation": res["explanation"],
        }

    @app.post("/split")
    def split_plan(req: SplitRequest):
        res, _ = _recommend_for_listing(req.listing)
        # use the category recommend() actually priced against (normalised — see _recommend_for_listing),
        # not the raw listing field, so a synonym like "decor" still matches artisans' compatible_categories
        category = res["input"]["category"]
        unit_payout = res["channels"]["b2b"]["artisan_net"]
        try:
            alloc = allocate_bulk_order(req.quantity, load_roster(), category,
                                        unit_payout=unit_payout, deadline_days=req.deadline_days)
        except ValueError as ex:
            raise HTTPException(422, str(ex))
        allocations = [{
            "artisan_id": a["artisan_id"], "artisan_name": a.get("name"), "village": a.get("region"),
            "quantity": a["units"], "lead_time_days": a.get("eta_days"),
            "take_home_inr": int(round(unit_payout)),
        } for a in alloc["allocations"]]
        return {
            "listing_id": req.listing.listing_id,
            "quantity_requested": alloc["requested"],
            "quantity_allocated": alloc["allocated"],
            "allocations": allocations,
            "lead_time_days": alloc["estimated_days_to_complete"],
            "feasible": alloc["fully_met"],
            "shortfall": alloc["shortfall"],
            "notes": [f"{e['artisan_id']}: {e['reason']}" for e in alloc["excluded"]],
        }

    return app


try:
    app = create_app()
except ImportError:          # FastAPI not installed — business/ML functions remain importable
    app = None


# ═════════════════════════════════════════════════════════════════════════════
# DEMO
# ═════════════════════════════════════════════════════════════════════════════
def _pick_demo_product() -> dict:
    """A real held-out TEST product (never seen in training or in the comparables index)."""
    meta = pd.read_pickle(CACHE / "meta.pkl")
    products = pd.read_pickle(ROOT / "data" / "processed" / "products.pkl").set_index("pid")
    test = meta[meta["split"] == "test"]
    for pref in (test[(test["category"] == "home_decor") & test["has_image"]],
                 test[test["has_image"]], test[test["category"] == "home_decor"], test):
        if len(pref):
            row = pref.sort_values("pid").iloc[0]
            break
    img = IMAGES / f"{row.pid}.jpg"
    return {"title": row.title, "description": products.at[row.pid, "description"],
            "category": row.category, "subcategory": row.subcategory, "material": row.material,
            "image": str(img) if img.exists() else None, "actual_price": float(row.price),
            "pid": row.pid, "source": row.source}


def _hr(title: str) -> None:
    print(f"\n{'─' * 78}\n{title}\n{'─' * 78}")


def run_demo(args) -> None:
    print(f"[demo] python {__import__('sys').version.split()[0]} — loading model artifacts …", flush=True)
    engine = get_engine()
    if getattr(args, "no_image", False):
        os.environ["B1_DISABLE_IMAGE_ENCODER"] = "1"
    if args.title:
        prod = {"title": args.title, "description": args.description or "", "category": args.category,
                "subcategory": args.subcategory, "material": args.material, "image": args.image,
                "actual_price": None}
    else:
        prod = _pick_demo_product()

    _hr("1. PRODUCT INPUT")
    print(f"title        : {prod['title']}")
    print(f"category     : {prod['category']} / {prod['subcategory']}   material: {prod['material']}")
    print(f"description  : {(prod['description'] or '')[:160]}{'…' if len(prod['description'] or '') > 160 else ''}")
    print(f"photo        : {prod['image'] or 'none'}")
    print(f"artisan costs: materials ₹{args.material_cost:,.0f}, {args.labour_hours:g} hours  (demo inputs)")
    if prod.get("actual_price") is not None:
        print(f"[held-out test product {prod['pid']} from {prod['source']}; "
              f"actual selling price ₹{prod['actual_price']:,.0f} — not used by the model]")

    if getattr(args, "no_image", False):
        prod["image"] = None
    print("\n[demo] running inference (first photo request loads CLIP) …", flush=True)
    res = engine.recommend(image=prod["image"], title=prod["title"], description=prod["description"],
                           category=prod["category"], subcategory=prod["subcategory"],
                           material=prod["material"], material_cost=args.material_cost,
                           labour_hours=args.labour_hours)

    _hr("2. FEATURES + MODEL")
    print(f"feature dims : {res['features']}")
    m = engine.metrics
    t = m["test"]["selected_model"]
    print(f"model        : {engine.version}")
    print(f"test metrics : MAE ₹{t['MAE']:,.0f}  RMSE ₹{t['RMSE']:,.0f}  R² {t['R2']:.3f}  "
          f"MedAE ₹{t['MedAE']:,.0f}  MAPE {t['MAPE_pct']:.1f}%   (from metrics.json)")

    _hr("3. AI MARKET PRICE")
    print(f"model prediction : ₹{res['ai_market_price']:,.2f}   80% band ₹{res['price_band_80pct'][0]:,.0f}"
          f"–₹{res['price_band_80pct'][1]:,.0f}")
    if prod.get("actual_price") is not None:
        err = res["ai_market_price"] - prod["actual_price"]
        print(f"vs actual        : ₹{prod['actual_price']:,.2f}  (error {err:+,.0f}, "
              f"{100 * err / prod['actual_price']:+.1f}%)")

    _hr("4. COMPARABLE PRODUCTS (training index, real listings)")
    for c in res["comparables"]:
        print(f"  {c['similarity']:.3f}  ₹{c['price']:>9,.0f}  {c['marketplace']:9s}  {c['title'][:58]}")
    s = res["comparable_stats"]
    print(f"stats: median ₹{s['median']:,.0f}  mean ₹{s['mean']:,.0f}  min ₹{s['min']:,.0f}  "
          f"max ₹{s['max']:,.0f}  std ₹{s['std']:,.0f}  IQR ₹{s['iqr'][0]:,.0f}–₹{s['iqr'][1]:,.0f}  "
          f"(searched within: {s['searched_within']})")

    _hr("5. PRICE RECOMMENDATION")
    f = res["floor"]
    print(f"market estimate   : ₹{res['market_estimate']:,.2f}")
    print(f"sustainable floor : ₹{f['sustainable_floor']:,.2f}  = (₹{f['material_cost']:,.0f} + "
          f"{f['labour_hours']:g}×₹{f['hourly_wage']:,.0f}) × {1 + f['overhead_pct']:.2f}")
    print(f"base price        : ₹{res['floor_enforcement']['base_price']:,.2f}  "
          f"(floor binding: {res['floor_enforcement']['floor_binding']})")
    print(f"recommended       : ₹{res['recommended_price']:,.2f}  "
          f"(passport premium ₹{res['passport_premium']['passport_premium']:,.2f})")
    print(f"shown to artisan  : ₹{res['recommended_price_rounded']:,.0f}   "
          f"confidence: {res['confidence']['level']} ({res['confidence']['score']}/4)")
    for line in res["explanation"]:
        print(f"  • {line}")
    for w in res["warnings"]:
        print(f"  ! {w}")

    _hr("6. CHANNEL PRICES")
    for ch, v in res["channels"].items():
        extra = " (discount capped by floor)" if v.get("discount_capped_by_floor") else ""
        print(f"  {ch:12s} buyer pays ₹{v['price']:>9,.2f}   artisan nets ₹{v['artisan_net']:>9,.2f}   "
              f"floor ₹{v['floor']:,.0f} {'✓' if v['floor_ok'] else '✗'}{extra}")

    _hr(f"7. BULK ORDER — {args.quantity} units")
    unit = res["channels"]["b2b"]["artisan_net"]
    alloc = allocate_bulk_order(args.quantity, load_roster(), prod["category"], unit_payout=unit,
                                deadline_days=args.deadline_days)
    for a in alloc["allocations"]:
        print(f"  {a['artisan_id']:6s} {a['name']:<22s} {a['region']:<24s} {a['units']:>4d} units "
              f"({a['share_pct']:>5.1f}%)  {a.get('eta_days', '-'):>3} d  payout ₹{a['payout']:>11,.2f}")
    print(f"allocated {alloc['allocated']}/{alloc['requested']}  shortfall {alloc['shortfall']}  "
          f"eligible artisans {alloc['eligible_artisans']}  total payout ₹{alloc['total_payout']:,.2f}")
    print(f"deadline {alloc['deadline_days']} days → completes in {alloc['estimated_days_to_complete']} "
          f"days (meets deadline: {alloc['meets_deadline']}); pooled capacity within deadline "
          f"{alloc['total_capacity_within_deadline']} units")

    _hr("8. CRAFT PASSPORT + QR")
    # passport is issued to the lead artisan of the bulk order (largest allocation)
    lead = next((a for a in load_roster() if alloc["allocations"]
                 and a["id"] == alloc["allocations"][0]["artisan_id"]), load_roster()[0])
    passport = create_passport(
        product={**prod, "making_time_hours": args.labour_hours},
        artisan=lead,
        pricing={"ai_market_price": res["ai_market_price"],
                 "sustainable_floor": res["floor"]["sustainable_floor"],
                 "recommended_price": res["recommended_price"], "model_version": engine.version})
    print(json.dumps({k: v for k, v in passport.items() if k != "qr_path"}, indent=2, ensure_ascii=False))
    print(f"QR code PNG: {passport['qr_path']}")
    print(f"\ninference latency: {res['latency_ms']} ms")


# ═════════════════════════════════════════════════════════════════════════════
# SELF-TEST — the gate for locking this model into the final system
# ═════════════════════════════════════════════════════════════════════════════
def run_selftest(args) -> int:
    """Checks every contract B2 / A1 / C1 depend on. Exit code 0 = safe to lock in."""
    checks, engine = [], None

    def check(name, fn):
        try:
            detail = fn()
            checks.append((True, name, detail))
        except Exception as ex:
            checks.append((False, name, f"{type(ex).__name__}: {ex}"))

    def load():
        nonlocal engine
        engine = get_engine()
        return engine.version

    check("model artifacts load", load)
    if engine is None:
        print("[selftest] FAIL — the model could not be loaded; nothing else can be checked")
        return 1

    def dims():
        f = engine.featurize(image=None, title="Brass diya lamp 10 cm", description="hand cast brass lamp",
                             category="home_decor", subcategory="Showpieces", material="brass")
        assert f["X"].shape[1] == len(engine.metadata["feature_names"]) \
            if "feature_names" in engine.metadata else True
        assert f["X"].shape[1] == engine.metadata["feature_dim"], "feature width ≠ trained width"
        assert np.isfinite(f["X"]).all(), "non-finite feature values"
        return f"{f['X'].shape[1]} features, size parsed = {f['size_cm']} cm"

    def taxonomy():
        t = engine.taxonomy()
        assert len(t["categories"]) == 10 and t["materials"], "taxonomy incomplete"
        assert sum(len(v) for v in t["subcategories"].values()) > 0, "no subcategories"
        return f"{len(t['categories'])} categories, {sum(len(v) for v in t['subcategories'].values())} subcategories"

    def determinism():
        kw = dict(title="Jute table mat set", description="woven by hand", category="home_furnishing",
                  material="jute", material_cost=150, labour_hours=3)
        a = engine.recommend(**kw)["recommended_price"]
        b = engine.recommend(**kw)["recommended_price"]
        assert a == b, f"two identical requests gave {a} and {b}"
        return f"₹{a:,.0f} twice"

    def ordering():
        cheap = engine.recommend(title="Small clay diya", description="single small oil lamp 4 cm",
                                 category="home_decor", material="terracotta",
                                 material_cost=30, labour_hours=1)
        big = engine.recommend(title="Large carved teak temple 90 cm", description="hand carved mandir",
                               category="home_decor", material="wood",
                               material_cost=4000, labour_hours=60)
        assert cheap["recommended_price"] < big["recommended_price"], "cheap item priced above big item"
        return f"₹{cheap['recommended_price']:,.0f} < ₹{big['recommended_price']:,.0f}"

    def floor_rule():
        r = engine.recommend(title="Tiny bead ring", description="small glass bead ring",
                             category="jewellery", material="beads_crystal",
                             material_cost=3000, labour_hours=20)     # costs far above any market price
        assert r["floor_enforcement"]["floor_binding"], "floor should bind when costs exceed the market"
        assert r["recommended_price"] >= r["floor"]["sustainable_floor"], "price below the wage floor"
        for name, ch in r["channels"].items():
            assert ch["floor_ok"], f"{name} pays the artisan below the floor"
        return f"floor ₹{r['floor']['sustainable_floor']:,.0f} enforced on all three channels"

    def comparables_real():
        r = engine.recommend(title="Terracotta Ganesha statue 6 cm", description="hand painted idol",
                             category="home_decor", subcategory="Showpieces", material="terracotta",
                             material_cost=250, labour_hours=6)
        assert len(r["comparables"]) == K_COMPARABLES, "wrong number of comparables"
        titles = set(engine.idx_meta["title"])
        assert all(c["title"] in titles and c["price"] > 0 for c in r["comparables"]), "fabricated comparable"
        assert r["confidence"]["level"] in ("high", "medium", "low")
        return (f"{len(r['comparables'])} real listings, mean similarity "
                f"{r['comparable_stats']['mean_similarity']:.2f}, confidence {r['confidence']['level']}")

    def bulk():
        a = allocate_bulk_order(500, load_roster(), "home_decor", unit_payout=700, deadline_days=30)
        assert sum(x["units"] for x in a["allocations"]) == a["allocated"], "units do not sum"
        assert a["allocated"] + a["shortfall"] == 500, "allocation does not account for every unit"
        assert abs(sum(x["share_pct"] for x in a["allocations"]) - 100) < 0.5, "shares do not sum to 100 %"
        return (f"{a['allocated']}/500 units across {len(a['allocations'])} artisans, "
                f"ETA {a['estimated_days_to_complete']} days, payout ₹{a['total_payout']:,.0f}")

    def passport():
        p = create_passport({"title": "selftest", "category": "home_decor", "material": "brass",
                             "making_time_hours": 2, "description": "selftest"},
                            load_roster()[0],
                            {"ai_market_price": 100, "sustainable_floor": 90,
                             "recommended_price": 105, "model_version": engine.version})
        png = PASSPORT_DIR / f"{p['passport_id']}.png"
        assert png.exists() and png.read_bytes()[:4] == b"\x89PNG", "QR not written"
        assert load_passport(p["passport_id"]), "passport not readable back"
        return f"{p['passport_id']} + QR"

    def quality():
        t = engine.metrics["test"]["selected_model"]
        band = engine.metrics["price_band"]
        assert t["MAPE_pct"] <= args.max_mape, f"MAPE {t['MAPE_pct']}% above the {args.max_mape}% limit"
        assert t["R2"] >= args.min_r2, f"R² {t['R2']} below the {args.min_r2} limit"
        assert abs(band["test_coverage_pct"] - band["nominal_coverage_pct"]) <= 10, "band coverage off"
        return (f"MAE ₹{t['MAE']:,.0f}, MAPE {t['MAPE_pct']:.1f}%, R² {t['R2']:.3f}, "
                f"band {band['test_coverage_pct']}%")

    for name, fn in [("feature contract", dims), ("taxonomy", taxonomy), ("determinism", determinism),
                     ("price ordering", ordering), ("wage floor enforced", floor_rule),
                     ("comparables are real listings", comparables_real),
                     ("bulk allocation + payout split", bulk), ("passport + QR", passport),
                     ("held-out quality thresholds", quality)]:
        check(name, fn)

    print(f"\n{'─' * 78}\nB1 SELF-TEST — model {engine.version}\n{'─' * 78}")
    for ok, name, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:34s} {detail}")
    failed = [c for c in checks if not c[0]]
    print(f"{'─' * 78}\n{len(checks) - len(failed)}/{len(checks)} checks passed — "
          f"{'SAFE TO LOCK IN' if not failed else 'DO NOT LOCK IN'}\n")
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="end-to-end demo")
    d.add_argument("--image")
    d.add_argument("--title")
    d.add_argument("--description", default="")
    d.add_argument("--category", default="home_decor", choices=CATEGORIES)
    d.add_argument("--subcategory")
    d.add_argument("--material")
    d.add_argument("--material-cost", type=float, default=250.0)
    d.add_argument("--labour-hours", type=float, default=6.0)
    d.add_argument("--quantity", type=int, default=500)
    d.add_argument("--deadline-days", type=int, default=30, help="bulk-order deadline for capacity pooling")
    d.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"],
                   help="compute device override (same as B1_DEVICE); cpu rules out Metal/CUDA problems")
    d.add_argument("--no-image", action="store_true",
                   help="skip the photo entirely (no CLIP load — useful if the weights are still downloading)")
    st = sub.add_parser("selftest", help="verify every contract the rest of the system depends on")
    st.add_argument("--max-mape", type=float, default=60.0, help="fail if test MAPE exceeds this")
    st.add_argument("--min-r2", type=float, default=0.30, help="fail if test R² falls below this")
    st.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"])
    s = sub.add_parser("serve", help="run the API")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"], help="compute device override")
    args = ap.parse_args()
    if getattr(args, "device", None):
        os.environ["B1_DEVICE"] = args.device
        features.DEVICE_OVERRIDE = args.device
    if args.cmd == "demo":
        run_demo(args)
    elif args.cmd == "selftest":
        raise SystemExit(run_selftest(args))
    else:
        import uvicorn
        uvicorn.run("service:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
