"""
Craftly B1 tests — run with:  pytest -q test.py

* Business-logic tests always run (pure functions).
* ML / integration tests need the trained artifacts (data → features → train)
  and are skipped automatically if they are missing.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import features as F
import service as S

ROOT = Path(__file__).resolve().parent
HAS_MODEL = (F.MODEL_DIR / "model.pkl").exists()
HAS_CACHE = (F.CACHE / "meta.pkl").exists()
needs_model = pytest.mark.skipif(not HAS_MODEL, reason="trained model not found — run the pipeline first")
needs_cache = pytest.mark.skipif(not HAS_CACHE, reason="feature cache not found — run features.py first")
# The image branch is only exercised when the CLIP weights are already on disk, so a test run never
# blocks on a ~600 MB download. Fetch them with `python features.py --download-encoders`.
IMAGE_READY = F.clip_weights_cached() and not os.environ.get("B1_DISABLE_IMAGE_ENCODER")
needs_clip = pytest.mark.skipif(not IMAGE_READY,
                                reason="CLIP weights not cached — run `python features.py --download-encoders`")


def roster():
    return [
        {"id": "A1", "name": "a", "capacity": 200, "compatible_categories": ["home_decor"], "region": "x"},
        {"id": "A2", "name": "b", "capacity": 150, "compatible_categories": ["home_decor", "apparel"], "region": "y"},
        {"id": "A3", "name": "c", "capacity": 150, "compatible_categories": ["home_decor"], "region": "z"},
        {"id": "A4", "name": "d", "capacity": 300, "compatible_categories": ["apparel"], "region": "w"},
        {"id": "A5", "name": "e", "capacity": 0, "compatible_categories": ["home_decor"], "region": "v"},
    ]


# ═════════════════════════════════════════════════════════════════════════════
# Business logic
# ═════════════════════════════════════════════════════════════════════════════
def test_wage_floor_formula():
    f = S.sustainable_floor(material_cost=1000, labour_hours=10, hourly_wage=60, overhead_pct=0.15)
    assert f["labour_cost"] == 600
    assert f["subtotal"] == 1600
    assert f["sustainable_floor"] == pytest.approx(1840.0)


def test_wage_floor_rejects_negative():
    with pytest.raises(ValueError):
        S.sustainable_floor(material_cost=-1, labour_hours=2)


def test_floor_enforcement_both_ways():
    above = S.enforce_floor(market_estimate=2500, floor=1840)
    assert above["base_price"] == 2500 and not above["floor_binding"]
    below = S.enforce_floor(market_estimate=900, floor=1840)
    assert below["base_price"] == 1840 and below["floor_binding"]


def test_passport_premium_5pct():
    p = S.apply_passport_premium(2000)
    assert p["passport_premium"] == pytest.approx(100.0)
    assert p["recommended_price"] == pytest.approx(2100.0)
    assert S.apply_passport_premium(2000, has_passport=False)["recommended_price"] == 2000


def test_channel_d2c():
    c = S.channel_prices(1000, floor=500)["d2c"]
    assert c["price"] == pytest.approx(1000 + 50 + 0.025 * 1000)
    assert c["artisan_net"] == pytest.approx(1000) and c["floor_ok"]


def test_channel_marketplace():
    c = S.channel_prices(1000, floor=500)["marketplace"]
    base = 1000 / (1 - 0.15)
    assert c["price"] == pytest.approx(base + 80 + 0.03 * base, abs=0.01)
    assert c["breakdown"]["commission"] == pytest.approx(0.15 * base, abs=0.01)
    assert c["artisan_net"] == pytest.approx(1000, abs=0.01)


def test_channel_b2b_and_floor_cap():
    c = S.channel_prices(1000, floor=500)["b2b"]
    assert c["artisan_net"] == pytest.approx(800)
    assert c["price"] == pytest.approx(830)
    assert not c["discount_capped_by_floor"]
    capped = S.channel_prices(1000, floor=900)["b2b"]       # 20 % discount would go below the floor
    assert capped["artisan_net"] == pytest.approx(900)
    assert capped["discount_capped_by_floor"] and capped["floor_ok"]
    assert capped["effective_discount_pct"] == pytest.approx(0.10)


def test_allocation_respects_capacity_and_compatibility():
    r = S.allocate_bulk_order(500, roster(), "home_decor")
    assert r["fully_met"] and r["allocated"] == 500 and r["shortfall"] == 0
    caps = {a["id"]: a["capacity"] for a in roster()}
    for a in r["allocations"]:
        assert a["units"] <= caps[a["artisan_id"]]
        assert a["artisan_id"] in {"A1", "A2", "A3"}
    assert {e["artisan_id"] for e in r["excluded"]} == {"A4", "A5"}


def test_allocation_order_and_tiebreak():
    r = S.allocate_bulk_order(500, roster(), "home_decor")
    assert [a["artisan_id"] for a in r["allocations"]] == ["A1", "A2", "A3"]   # 200, then 150/150 by id
    assert [a["units"] for a in r["allocations"]] == [200, 150, 150]


def test_allocation_shortfall():
    r = S.allocate_bulk_order(800, roster(), "home_decor")
    assert r["allocated"] == 500 and r["shortfall"] == 300 and not r["fully_met"]


def test_allocation_exact_and_partial_last():
    r = S.allocate_bulk_order(260, roster(), "home_decor")
    assert [a["units"] for a in r["allocations"]] == [200, 60]
    assert sum(a["units"] for a in r["allocations"]) == 260


def test_allocation_deterministic_and_order_independent():
    ros = roster()
    a = S.allocate_bulk_order(500, ros, "home_decor")
    b = S.allocate_bulk_order(500, list(reversed(ros)), "home_decor")
    c = S.allocate_bulk_order(500, ros, "home_decor")
    assert a == c
    assert a["allocations"] == b["allocations"] and a["shortfall"] == b["shortfall"]


def test_allocation_payout_split():
    r = S.allocate_bulk_order(500, roster(), "home_decor", unit_payout=100)
    assert r["total_payout"] == 50000
    assert sum(a["payout"] for a in r["allocations"]) == pytest.approx(50000)
    assert sum(a["share_pct"] for a in r["allocations"]) == pytest.approx(100)


def test_allocation_invalid_quantity():
    with pytest.raises(ValueError):
        S.allocate_bulk_order(0, roster(), "home_decor")


def test_passport_and_qr(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "PASSPORT_DIR", tmp_path)
    p = S.create_passport({"title": "Brass diya", "category": "home_decor", "material": "brass",
                           "making_time_hours": 8, "description": "hand cast"},
                          {"id": "A1", "name": "a", "region": "r", "craft_cluster": "c"},
                          {"ai_market_price": 500, "sustainable_floor": 400,
                           "recommended_price": 525, "model_version": "v"})
    for key in ("passport_id", "product", "artisan", "pricing", "provenance", "created_at"):
        assert key in p
    assert p["provenance"]["available"] is False
    stored = json.loads((tmp_path / f"{p['passport_id']}.json").read_text())
    assert stored["passport_id"] == p["passport_id"]
    png = (tmp_path / f"{p['passport_id']}.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    assert Image.open(io.BytesIO(png)).size[0] > 50


def test_passport_id_path_traversal_rejected():
    assert S.load_passport("../../etc/passwd") is None


# ═════════════════════════════════════════════════════════════════════════════
# Feature definitions
# ═════════════════════════════════════════════════════════════════════════════
def test_price_leakage_scrubbed():
    t = F.scrub_text("Buy X for Rs.379 online. Price: Rs. 1,299 MRP 999/- 62% off ₹450 at Flipkart.com")
    assert not any(ch.isdigit() for ch in t), t
    assert "flipkart" not in t.lower()


def test_material_parsing():
    assert F.material_from_text("Sterling Silver") == "silver"
    assert F.material_from_text("German Silver Rings") == "alloy_metal"
    assert F.material_from_text("Gold-plated brass necklace") == "brass"
    assert F.normalise_material("Mango Wood") == "wood"
    assert F.normalise_material("terracotta_ceramic") == "terracotta_ceramic"
    assert F.normalise_material(None) == F.UNKNOWN


def test_structured_row_shape_and_unknowns():
    enc = {"category": {c: i + 1 for i, c in enumerate(F.CATEGORIES)},
           "subcategory": {"home_decor::showpieces": 1},
           "material": {m: i + 1 for i, m in enumerate(F.MATERIALS)}}
    stats = {"per_category": {"home_decor": {"median_price": 800.0, "price_std": 50.0}},
             "global_median": 450.0, "global_std": 20.0}
    row = F.build_structured_row(title="a b c", description="", category="home_decor",
                                 subcategory="Showpieces", material="xyz", has_image=False,
                                 encoders=enc, category_stats=stats)
    assert row.shape == (len(F.STRUCT_FEATURES),)
    assert row[1] == 1 and row[2] == 0 and row[3] == 3 and row[4] == 0 and row[5] == 0
    assert row[6] == 800.0
    unk = F.build_structured_row(title="a", description="d", category="furniture", subcategory=None,
                                 material="wood", has_image=True, encoders=enc, category_stats=stats)
    assert unk[6] == 450.0 and unk[7] == 20.0 and unk[5] == 1   # unseen category → global stats


def test_similarity_bounds_and_missing_image_rule():
    rng = np.random.default_rng(0)
    def unit(n, d):
        v = rng.normal(size=(n, d)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)
    qi, qt, di, dt = unit(2, 512), unit(2, 384), unit(5, 512), unit(5, 384)
    sims = F.combined_similarity(qi, qt, np.array([True, False]), di, dt, np.array([True] * 5))
    assert sims.shape == (2, 5) and (sims >= 0).all() and (sims <= 1).all()
    # query without photo → text-only
    np.testing.assert_allclose(sims[1], np.clip(qt[1] @ dt.T, 0, 1), rtol=1e-5)
    idx = F.top_k_indices(np.array([0.1, 0.9, 0.5, 0.7]), 2, np.array(["a", "b", "a", "a"]), "a")
    assert list(idx) == [3, 2]


# ═════════════════════════════════════════════════════════════════════════════
# ML pipeline (needs artifacts)
# ═════════════════════════════════════════════════════════════════════════════
@needs_cache
def test_processed_data_valid():
    df = pd.read_pickle(ROOT / "data" / "processed" / "products.pkl")
    assert len(df) > 1000
    assert df["price"].notna().all() and (df["price"] > 0).all()
    assert set(df["split"]) == {"train", "val", "test"}
    assert (df.groupby("group")["split"].nunique() == 1).all()          # no name group crosses splits
    frac = df["split"].value_counts(normalize=True)
    assert 0.62 < frac["train"] < 0.78
    assert not df["description"].str.contains(r"(?:rs\.?|₹)\s*\d", case=False, regex=True).any()


@needs_cache
def test_feature_dimensions():
    n = len(pd.read_pickle(F.CACHE / "meta.pkl"))
    assert np.load(F.CACHE / "image_emb.npy").shape == (n, 512)
    assert np.load(F.CACHE / "text_emb.npy").shape == (n, 384)
    assert np.load(F.CACHE / "struct.npy").shape == (n, len(F.STRUCT_FEATURES))


@needs_cache
def test_missing_image_rows_are_zero_vectors():
    meta = pd.read_pickle(F.CACHE / "meta.pkl")
    img = np.load(F.CACHE / "image_emb.npy")
    struct = np.load(F.CACHE / "struct.npy")
    no = ~meta["has_image"].to_numpy(bool)
    assert np.all(img[no] == 0)
    assert np.all(struct[no, F.STRUCT_FEATURES.index("has_image")] == 0)


@needs_model
def test_category_stats_from_train_only():
    import joblib
    stats = joblib.load(F.MODEL_DIR / "category_stats.pkl")
    meta = pd.read_pickle(F.CACHE / "meta.pkl")
    train = meta[meta["split"] == "train"]
    for cat, s in stats["per_category"].items():
        assert s["median_price"] == pytest.approx(train.loc[train["category"] == cat, "price"].median())
    struct = np.load(F.CACHE / "struct.npy")
    col = F.STRUCT_FEATURES.index("category_median_price")
    for split in ("val", "test"):                       # val/test rows carry TRAIN statistics
        rows = meta.index[meta["split"] == split][:50]
        for i in rows:
            exp = stats["per_category"].get(meta.at[i, "category"], {}).get("median_price", stats["global_median"])
            assert struct[i, col] == pytest.approx(exp)


@needs_model
def test_model_metadata_and_metrics():
    meta = json.loads((F.MODEL_DIR / "metadata.json").read_text())
    metrics = json.loads((F.MODEL_DIR / "metrics.json").read_text())
    assert meta["version"] and meta["created_at"] and meta["feature_dim"] == len(F.FEATURE_NAMES)
    for k in ("MAE", "RMSE", "R2", "MedAE", "MAPE_pct"):
        assert np.isfinite(metrics["test"]["selected_model"][k])
    assert len(metrics["validation"]) >= 4      # A–D, plus the robust and ensemble variants
    reg = json.loads((ROOT / "models" / "experiments.json").read_text())
    assert len(reg) >= 4


@pytest.fixture(scope="module")
def engine():
    if not HAS_MODEL:
        pytest.skip("trained model not found")
    return S.PricingEngine()


@needs_model
def test_inference_positive_and_deterministic(engine):
    kw = dict(title="Handmade brass Ganesha idol", description="Hand cast brass idol for pooja room",
              category="home_decor", subcategory="Showpieces", material="brass")
    f = engine.featurize(image=None, **kw)
    assert f["dims"]["total"] == len(engine.members[0][0].feature_name_) \
        if hasattr(engine.members[0][0], "feature_name_") else True
    assert (f["dims"]["image"], f["dims"]["text"]) == (512, 384)
    assert f["dims"]["structured"] == len(F.STRUCT_FEATURES)
    assert f["X"].shape[1] == len(F.FEATURE_NAMES)
    prices = [engine.market_price(engine.featurize(image=None, **kw)["X"])["ai_market_price"] for _ in range(3)]
    assert prices[0] > 0 and len(set(prices)) == 1
    lo, hi = engine.market_price(f["X"])["price_band_80pct"]
    assert lo <= prices[0] <= hi


@needs_model
def test_missing_text_still_valid(engine):
    f = engine.featurize(image=None, title="Bag", description="", category="bags_accessories",
                         subcategory=None, material=None)
    assert np.isfinite(f["X"]).all() and np.linalg.norm(f["txt"]) > 0.99


@needs_model
def test_comparables_are_real_training_rows(engine):
    f = engine.featurize(image=None, title="Cotton block print kurta", description="hand block printed",
                         category="apparel", subcategory=None, material="cotton")
    comp = engine.comparables(f["img"], f["txt"], False, "apparel", k=8)
    assert len(comp["items"]) == 8
    titles = set(engine.idx_meta["title"])
    for c in comp["items"]:
        assert 0 <= c["similarity"] <= 1 and c["price"] > 0 and c["title"] in titles
    sims = [c["similarity"] for c in comp["items"]]
    assert sims == sorted(sims, reverse=True)


# ═════════════════════════════════════════════════════════════════════════════
# Integration: full chain through the HTTP API
# ═════════════════════════════════════════════════════════════════════════════
@needs_model
def test_api_end_to_end(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from PIL import Image                                     # noqa: F401  (used when IMAGE_READY)
    monkeypatch.setattr(S, "PASSPORT_DIR", tmp_path)
    client = TestClient(S.app)
    assert client.get("/api/health").json()["status"] == "ok"
    info = client.get("/api/model/info").json()
    assert info["metadata"]["feature_dim"] == len(F.FEATURE_NAMES)

    data = {"title": "Handwoven jute table mat set", "description": "Set of 6 mats woven by hand from jute",
            "category": "home_furnishing", "material": "jute", "material_cost": 200, "labour_hours": 5}
    files = None
    if IMAGE_READY:                      # only send a photo when CLIP is cached (no download inside a test)
        buf = io.BytesIO()
        Image.new("RGB", (256, 256), (180, 120, 40)).save(buf, "JPEG")
        files = {"image": ("mat.jpg", buf.getvalue(), "image/jpeg")}
    r = client.post("/api/predict", data={"data": json.dumps(data)}, files=files)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["input"]["has_image"] is IMAGE_READY and res["features"]["total"] == len(F.FEATURE_NAMES)
    assert res["ai_market_price"] > 0 and res["recommended_price"] >= res["floor"]["sustainable_floor"]
    assert len(res["comparables"]) == 8
    assert all(ch["price"] > 0 and ch["floor_ok"] for ch in res["channels"].values())

    r = client.post("/api/bulk-order", json={"product_category": "home_furnishing", "quantity": 500,
                                             "unit_payout": res["channels"]["b2b"]["artisan_net"]})
    assert r.status_code == 200 and r.json()["allocated"] > 0

    r = client.post("/api/passport", json={
        "product": {**data, "making_time_hours": 5},
        "artisan": {"id": "A001", "name": "demo", "region": "r"},
        "pricing": {"ai_market_price": res["ai_market_price"],
                    "sustainable_floor": res["floor"]["sustainable_floor"],
                    "recommended_price": res["recommended_price"], "model_version": res["model"]["version"]}})
    assert r.status_code == 200
    pid = r.json()["passport_id"]
    assert client.get(f"/api/passport/{pid}").json()["passport_id"] == pid
    qr = client.get(f"/api/passport/{pid}/qr")
    assert qr.status_code == 200 and qr.content[:4] == b"\x89PNG"

    bad = client.post("/api/predict", data={"data": json.dumps({**data, "category": "cars"})})
    assert bad.status_code == 422


@needs_model
@needs_clip
def test_image_branch_uses_clip(engine):
    """Only runs when the CLIP weights are already cached, so it never waits on a download."""
    from PIL import Image
    img = Image.new("RGB", (224, 224), (120, 80, 40))
    f = engine.featurize(image=img, title="Brass diya lamp", description="hand cast brass lamp",
                         category="home_decor", subcategory=None, material="brass")
    assert f["has_image"] is True
    assert np.linalg.norm(f["img"]) == pytest.approx(1.0, abs=1e-3)
    assert engine.market_price(f["X"])["ai_market_price"] > 0


@needs_model
def test_retrieval_features_and_calibration(engine):
    """The retrieval block must carry the comparables actually shown, and calibration must be sane."""
    f = engine.featurize(image=None, title="Terracotta Ganesha statue", description="hand painted",
                         category="home_decor", subcategory="Showpieces", material="terracotta")
    assert f["comparables"]["stats"]["median"] > 0
    if engine.extra_features:                      # model trained with the retrieval block
        n = len(engine.extra_features)
        assert f["X"].shape[1] == len(F.FEATURE_NAMES)
        assert f["X"][0, -n] == pytest.approx(f["comparables"]["stats"]["median"], rel=1e-3)
        assert 0 <= f["X"][0, -n + 1] <= 1                      # mean similarity
        assert f["X"][0, -1] >= 1.0                             # IQR ratio (p75 / p25)
    if engine.calibration:
        lines = [engine.calibration["global"], *engine.calibration.get("per_category", {}).values()]
        assert all(0.3 <= ln["b"] <= 2.0 for ln in lines)   # a wild slope would mean a broken fit
    assert 0.5 <= engine.blend_weight <= 1.0


@needs_model
def test_prediction_sane_range(engine):
    """A cheap product must not be priced like an expensive one (guards against scale blow-ups)."""
    cheap = engine.recommend(title="Small clay diya set", description="set of 12 small oil lamps",
                             category="home_decor", subcategory="Showpieces", material="terracotta",
                             material_cost=40, labour_hours=1)
    pricey = engine.recommend(title="Large carved wooden temple", description="hand carved teak temple",
                              category="home_decor", subcategory="Showpieces", material="wood",
                              material_cost=4000, labour_hours=60)
    assert cheap["recommended_price"] < pricey["recommended_price"]
    assert cheap["recommended_price"] >= cheap["floor"]["sustainable_floor"]


def test_size_and_pack_parsing():
    assert F.parse_size_cm("Ganesha Showpiece  -  6 cm") == 6.0
    assert F.parse_size_cm('12" Chatri Ganesha') == pytest.approx(30.48)
    assert F.parse_size_cm("no dimensions here") == 0.0
    assert F.parse_pack_count("Cushion Cover (Pack of 5)") == 5
    assert F.parse_pack_count("single item") == 1


def test_size_affinity_ranking():
    """A 6 cm idol must rank 5-7 cm listings above 40 cm ones, all else equal."""
    sims = np.array([0.60, 0.61, 0.62])
    sizes = np.array([6.5, 40.0, 45.0])
    top = F.top_k_indices(sims, 1, db_sizes=sizes, q_size=6.0)
    assert top[0] == 0


def test_retail_rounding_never_breaks_floor():
    assert S.retail_round(826.0) == 830
    assert S.retail_round(1234.0) == 1250
    assert S.retail_round(704.0, floor=710.0) >= 710


def test_deadline_aware_allocation():
    roster_with_rates = [{**a, "units_per_day": 5} for a in roster()]
    fast = S.allocate_bulk_order(500, roster_with_rates, "home_decor", deadline_days=40)
    tight = S.allocate_bulk_order(500, roster_with_rates, "home_decor", deadline_days=5)
    assert fast["allocated"] == 500 and fast["meets_deadline"] is True
    assert tight["allocated"] < 500 and tight["shortfall"] > 0     # 5 days × 5/day each = 125 units
    for a in tight["allocations"]:
        assert a["units"] <= 25 and a["eta_days"] <= 5


@needs_model
def test_confidence_and_rounded_price_present(engine):
    r = engine.recommend(title="Brass diya lamp 10 cm", description="hand cast", category="home_decor",
                         material="brass", material_cost=200, labour_hours=4)
    assert r["confidence"]["level"] in ("high", "medium", "low")
    assert r["recommended_price_rounded"] >= r["floor"]["sustainable_floor"]
    assert abs(r["recommended_price_rounded"] - r["recommended_price"]) <= 100


@needs_model
def test_price_anchor_stats_are_train_only(engine):
    """Subcategory / material medians must come from the saved TRAIN statistics, not the live row."""
    import joblib
    stats = joblib.load(F.MODEL_DIR / "category_stats.pkl")
    meta = pd.read_pickle(F.CACHE / "meta.pkl")
    train = meta[meta["split"] == "train"]
    for mat, s in list(stats["per_material"].items())[:5]:
        rows = train[train["material"] == mat]["price"]
        assert s["median_price"] == pytest.approx(rows.median())
        assert s["n"] >= stats["min_group_rows"]
    f = engine.featurize(image=None, title="unheard-of craft item", description="",
                         category="furniture", subcategory="no such subcategory",
                         material="no such material")
    i = F.STRUCT_FEATURES.index("subcategory_median_price") + F.IMG_DIM + F.TXT_DIM
    assert f["X"][0, i] == pytest.approx(stats["global_median"])      # unseen → global fallback
