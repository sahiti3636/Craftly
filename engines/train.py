"""
Craftly B1 — Step 3: train, compare, select, evaluate, save

    python train.py                    # select by validation RMSE (plan default)
    python train.py --select-by MAE    # or MedAE / MAPE_pct — relative-error view of "best"

Trains four regressors on the 904-d feature matrix produced by features.py
    A  LightGBM   raw price        B  LightGBM   log(price)
    C  XGBoost    raw price        D  XGBoost    log(price)
selects the one with the lowest VALIDATION RMSE in ₹ (log models are inverse-transformed first),
and evaluates only that model ONCE on the held-out test split.

Also produced (all from real runs, nothing hand-entered):
    - reference baseline: category-median predictor (what a non-ML rule would say)
    - comparable-search (k-NN) estimator and the 0.7·model + 0.3·comparables blend used at runtime,
      evaluated on val and test, plus the val-optimal blend weight for information
    - an 80% price band (LightGBM quantile regressors, q10/q90 on log price) with measured test coverage
    - feature importance by block (image / text / structured) and top features

Artifacts → models/price_model/
    model.pkl  metadata.json  metrics.json  feature_importance.json
    index_img.npy  index_txt.npy  index_meta.pkl      (comparable-search index = TRAIN split)
models/experiments.json   (append-only experiment registry)
MODEL_CARD.md             (generated from metrics.json)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
import warnings
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import (mean_absolute_error, mean_squared_error, median_absolute_error,
                             r2_score)

from features import (CACHE, CATEGORICAL_STRUCT, EXTRA_FEATURES, FEATURE_NAMES, IMG_DIM,
                         MODEL_DIR, STRUCT_FEATURES, TXT_DIM, combined_similarity, top_k_indices)

warnings.filterwarnings("ignore", message=".*eval_set.*deprecated.*")   # LightGBM ≥4.6; kept for older versions
ROOT = Path(__file__).resolve().parent
SEED = 42
BAND_COVERAGE = 0.80
K_COMPARABLES = 8
BLEND_MODEL_WEIGHT = 0.7            # runtime rule (plan §6): 0.7·model + 0.3·comparable median
EARLY_STOPPING = 50

LGBM_PARAMS = dict(n_estimators=1000, learning_rate=0.05, max_depth=8, num_leaves=63,
                   min_child_samples=20,
                   # added for 896 dense embedding dims: column/row subsampling (speed + regularisation)
                   colsample_bytree=0.5, subsample=0.8, subsample_freq=1,
                   random_state=SEED, n_jobs=-1, verbose=-1, deterministic=True, force_row_wise=True)
XGB_PARAMS = dict(n_estimators=1000, learning_rate=0.05, max_depth=8, min_child_weight=5,
                  colsample_bytree=0.5, subsample=0.8,
                  tree_method="hist", eval_metric="rmse", early_stopping_rounds=EARLY_STOPPING,
                  random_state=SEED, n_jobs=-1)

# Bounded, fixed (therefore reproducible) search grid for the log-target LightGBM.
TUNING_GRID = [
    {"learning_rate": 0.05, "num_leaves": 63, "min_child_samples": 20, "colsample_bytree": 0.5},
    {"learning_rate": 0.05, "num_leaves": 31, "min_child_samples": 20, "colsample_bytree": 0.5},
    {"learning_rate": 0.05, "num_leaves": 127, "min_child_samples": 40, "colsample_bytree": 0.5},
    {"learning_rate": 0.03, "num_leaves": 63, "min_child_samples": 20, "colsample_bytree": 0.3},
    {"learning_rate": 0.03, "num_leaves": 127, "min_child_samples": 40, "colsample_bytree": 0.5},
    {"learning_rate": 0.05, "num_leaves": 63, "min_child_samples": 10, "colsample_bytree": 0.3},
    {"learning_rate": 0.03, "num_leaves": 31, "min_child_samples": 10, "colsample_bytree": 0.5},
    {"learning_rate": 0.08, "num_leaves": 63, "min_child_samples": 20, "colsample_bytree": 0.5},
]

VARIANTS = [
    ("A", "lightgbm", "raw"),
    ("B", "lightgbm", "log"),
    ("C", "xgboost", "raw"),
    ("D", "xgboost", "log"),
    ("E", "lightgbm-huber", "log"),      # robust loss: large outliers stop dominating the fit
]
ENSEMBLE_MEMBERS = ["B", "D", "E"]       # variant F = geometric mean of the log-target models
CAT_IDX = [IMG_DIM + TXT_DIM + STRUCT_FEATURES.index(c) for c in CATEGORICAL_STRUCT]


def log(msg: str) -> None:
    print(f"[train] {msg}", flush=True)


# ── metrics (always in ₹) ────────────────────────────────────────────────────
def regression_metrics(y: np.ndarray, p: np.ndarray) -> dict:
    p = np.maximum(p, 1.0)
    return {
        "MAE": round(float(mean_absolute_error(y, p)), 2),
        "RMSE": round(float(np.sqrt(mean_squared_error(y, p))), 2),
        "R2": round(float(r2_score(y, p)), 4),
        "MedAE": round(float(median_absolute_error(y, p)), 2),
        "MAPE_pct": round(float(np.mean(np.abs(p - y) / y) * 100), 2),
        "within_25pct": round(float(np.mean(np.abs(p - y) / y <= 0.25) * 100), 2),
        "n": int(len(y)),
    }


# ── model fitting ────────────────────────────────────────────────────────────
def fit_variant(algo: str, transform: str, Xtr, ytr, Xva, yva, overrides: dict | None = None):
    t_tr = np.log(ytr) if transform == "log" else ytr
    t_va = np.log(yva) if transform == "log" else yva
    if algo.startswith("lightgbm"):
        objective = "huber" if algo.endswith("huber") else "regression"
        model = lgb.LGBMRegressor(objective=objective, **{**LGBM_PARAMS, **(overrides or {})})
        model.fit(Xtr, t_tr, eval_set=[(Xva, t_va)], eval_metric="l2",
                  categorical_feature=CAT_IDX,
                  callbacks=[lgb.early_stopping(EARLY_STOPPING, verbose=False)])
        best_iter = int(model.best_iteration_ or model.n_estimators)
    else:
        model = xgb.XGBRegressor(objective="reg:squarederror", **XGB_PARAMS)
        model.fit(Xtr, t_tr, eval_set=[(Xva, t_va)], verbose=False)
        best_iter = int(model.best_iteration) + 1
    return model, best_iter


def predict_price(model, transform: str, X: np.ndarray) -> np.ndarray:
    raw = model.predict(X)
    return np.exp(raw) if transform == "log" else np.maximum(raw, 1.0)


def predict_members(members: list, X: np.ndarray) -> np.ndarray:
    """Geometric mean across ensemble members (i.e. the average in log space)."""
    preds = np.vstack([np.log(np.maximum(predict_price(m, t, X), 1.0)) for m, t in members])
    return np.exp(preds.mean(axis=0))


MIN_CALIBRATION_ROWS = 80        # a per-category fit needs enough validation rows to be meaningful


def _fit_line(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    b, a = np.polyfit(np.log(np.maximum(y_pred, 1.0)), np.log(y_true), 1)
    return {"a": float(a), "b": float(b)}


def fit_calibration(y_true: np.ndarray, y_pred: np.ndarray, categories: np.ndarray | None = None) -> dict:
    """
    Post-hoc bias correction fitted on VALIDATION only: log(y) ≈ a + b·log(pred).
    A separate line is fitted per category where there are enough validation rows — that is what
    lifts the weak categories — and every other category falls back to the global line.
    """
    cal = {"global": _fit_line(y_true, y_pred), "per_category": {}}
    if categories is not None:
        for c in sorted(set(categories)):
            m = categories == c
            if m.sum() >= MIN_CALIBRATION_ROWS:
                line = _fit_line(y_true[m], y_pred[m])
                if 0.3 <= line["b"] <= 2.0:              # ignore degenerate fits
                    cal["per_category"][str(c)] = {**line, "n_val": int(m.sum())}
    return cal


def apply_calibration(pred: np.ndarray, cal: dict | None, categories: np.ndarray | None = None) -> np.ndarray:
    if not cal:
        return pred
    logp = np.log(np.maximum(pred, 1.0))
    g = cal["global"]
    out = np.exp(g["a"] + g["b"] * logp)
    if categories is not None and cal.get("per_category"):
        for c, line in cal["per_category"].items():
            m = categories == c
            if m.any():
                out[m] = np.exp(line["a"] + line["b"] * logp[m])
    return out


def fit_quantile(alpha: float, Xtr, ytr, Xva, yva):
    params = {**LGBM_PARAMS}
    model = lgb.LGBMRegressor(objective="quantile", alpha=alpha, **params)
    model.fit(Xtr, np.log(ytr), eval_set=[(Xva, np.log(yva))], eval_metric="quantile",
              categorical_feature=CAT_IDX,
              callbacks=[lgb.early_stopping(EARLY_STOPPING, verbose=False)])
    return model


# ── comparable search evaluation ────────────────────────────────────────────



def comparable_features(q_img, q_txt, q_has, q_cat, db_img, db_txt, db_has, db_cat, db_price,
                        q_groups=None, db_groups=None, q_size=None, db_size=None,
                        k=K_COMPARABLES, batch=256):
    """
    For every query row: median price and mean similarity of its k nearest DB listings.
    When q_groups/db_groups are given (training rows querying the training index), every row of the
    query's own name-group is removed first — so a product never sees itself or its own variants.
    """
    med = np.empty(len(q_img), dtype=np.float64)
    sim = np.empty(len(q_img), dtype=np.float64)
    top1 = np.empty(len(q_img), dtype=np.float64)
    iqr = np.empty(len(q_img), dtype=np.float64)
    for s0 in range(0, len(q_img), batch):
        sims = combined_similarity(q_img[s0:s0 + batch], q_txt[s0:s0 + batch], q_has[s0:s0 + batch],
                                   db_img, db_txt, db_has)
        for j in range(sims.shape[0]):
            i = s0 + j
            row = sims[j]
            if q_groups is not None:
                row = row.copy()
                row[db_groups == q_groups[i]] = -1.0
            top = top_k_indices(row, k, db_cat, q_cat[i], db_size,
                                float(q_size[i]) if q_size is not None else 0.0)
            prices = db_price[top]
            med[i] = np.median(prices)
            sim[i] = float(row[top].mean())
            top1[i] = float(prices[0])
            lo, hi = np.percentile(prices, [25, 75])
            iqr[i] = float(hi / max(lo, 1.0))          # dispersion: how sure the neighbourhood is
    return med, sim, top1, iqr


# ── retrieval features ──────────────────────────────────────────────────────
# ── importance ───────────────────────────────────────────────────────────────
def importances(model, algo: str) -> np.ndarray:
    if algo == "lightgbm":
        return model.booster_.feature_importance(importance_type="gain").astype(float)
    score = model.get_booster().get_score(importance_type="total_gain")
    imp = np.zeros(len(FEATURE_NAMES))
    for k, v in score.items():
        imp[int(k[1:])] = v
    return imp


def write_model_card(meta: dict, metrics: dict, fi: dict, manifest: dict) -> None:
    sel = metrics["selected_model"]
    t = metrics["test"]
    val_rows = "\n".join(
        f"| {v['id']} | {v['algo']} | {v['target']} | {v['val']['RMSE']:,.2f} | {v['val']['MAE']:,.2f} | "
        f"{v['val']['R2']:.4f} | {v['val']['MAPE_pct']:.1f} | {v['best_iteration']} | {v['train_seconds']} |"
        for v in metrics["validation"])
    test_rows = "\n".join(
        f"| {name} | {m['MAE']:,.2f} | {m['RMSE']:,.2f} | {m['R2']:.4f} | {m['MedAE']:,.2f} | "
        f"{m['MAPE_pct']:.1f} | {m['within_25pct']:.1f} |"
        for name, m in t.items())
    cat_rows = "\n".join(f"| {c} | {m['n']} | {m['MAE']:,.2f} | {m['MedAE']:,.2f} | {m['MAPE_pct']:.1f} |"
                         for c, m in metrics["per_category_test"].items())
    blocks = ", ".join(f"{k}: {v:.1%}" for k, v in fi["gain_share_by_block"].items())
    band = metrics["price_band"]
    c = metrics.get("calibration", {})
    g = c.get("global", {})
    cal_line = (f"Post-hoc calibration fitted on validation only: global `log(y) = {g.get('a', 0):.3f} + "
                f"{g.get('b', 1):.3f}·log(pred)` plus {len(c.get('per_category', {}))} per-category lines "
                f"({', '.join(sorted(c.get('per_category', {}))) or 'none'}), applied: **{c.get('applied')}** "
                f"(validation {list(c.get('validation_before', {}).items())[:1]} → "
                f"{list(c.get('validation_after', {}).items())[:1]}). "
                f"Runtime blend weight, chosen on validation: "
                f"{metrics['blend_weight_grid_val']['runtime_model_weight']:.0%} model.")
    card = f"""# Model card — Craftly B1 market-price model

*Generated by `train.py` on {meta['created_at']}. Every number below is read from `models/price_model/metrics.json`.*

## Summary
- **Version:** `{meta['version']}`
- **Selected model:** {sel['id']} — {sel['algo']}, target = {sel['target']} price (lowest {sel['selected_by']})
- **Task:** predict the observed marketplace selling price (₹) of a craft product from its photo, text and a few structured attributes.
- **Not learned:** wage floor, overheads, craft-passport premium, channel fees, bulk allocation — these are explicit business rules in `service.py`.

## Inputs (identical at training and inference)
| Block | Dim | Source |
|---|---|---|
| Image embedding | {IMG_DIM} | {manifest.get('image_encoder')} (frozen), zero vector when no photo |
| Text embedding | {TXT_DIM} | {manifest.get('text_encoder')} (frozen) on `title \\| description` after price/boilerplate scrubbing |
| Structured | {len(STRUCT_FEATURES)} | {', '.join(STRUCT_FEATURES)} |
| Retrieval | {len(EXTRA_FEATURES)} | {', '.join(EXTRA_FEATURES)} — from the k nearest TRAIN listings, a row's own name-group excluded |

Excluded on purpose: MRP / retail price and discount (target leakage), ratings / reviews / bestseller / marketplace
(unavailable for a new artisan product), material cost / labour hours / region (not present in marketplace data).
All price anchors (category median/std, subcategory median, material median) and the retrieval features are
computed on the training split only and read from the saved tables at inference, so no row ever sees its own price.

## Data
- Rows: {meta['n_rows']} (train {meta['split_sizes'].get('train')}, val {meta['split_sizes'].get('val')}, test {meta['split_sizes'].get('test')}); dataset hash `{meta['dataset_hash']}`
- Split: 70/15/15, stratified by category, grouped by normalised product name (variants never cross splits).
- Image coverage: {manifest.get('image_coverage', 0):.1%}  by source: {manifest.get('image_coverage_by_source')}
- Details: `data/DATA_SOURCES.md`.

## Validation (model selection)
| ID | Algorithm | Target | RMSE ₹ | MAE ₹ | R² | MAPE % | Trees | Train s |
|---|---|---|---|---|---|---|---|---|
{val_rows}

## Test (held-out, evaluated once)
| Estimator | MAE ₹ | RMSE ₹ | R² | MedAE ₹ | MAPE % | within ±25 % |
|---|---|---|---|---|---|---|
{test_rows}

`selected_model` is the ML prediction alone; `knn_comparables` is the median price of the {K_COMPARABLES} most similar
training products; `runtime_blend` is {BLEND_MODEL_WEIGHT}·model + {1 - BLEND_MODEL_WEIGHT:.1f}·comparables (the rule used by the service);
`category_median_baseline` is the non-ML reference. Validation-optimal blend weight (information only, not used): {metrics['blend_weight_grid_val']['best_model_weight']}.

### Per-category test error (selected model)
| Category | n | MAE ₹ | MedAE ₹ | MAPE % |
|---|---|---|---|---|
{cat_rows}

### 80 % price band
{band['method']}. Test coverage: {band['test_coverage_uncalibrated_pct']}% before calibration → **{band['test_coverage_pct']}%** after
(nominal 80 %), median band width ₹{band['test_median_width_inr']:,.0f}.

## Calibration and blending
{cal_line}

## Feature importance (gain share)
{blocks}. Top features: {', '.join(f"{n} ({s:.1%})" for n, s in fi['top_features'][:10])}.

## Intended use
Price *suggestion* for artisans listing handmade goods; the service combines it with comparable listings,
enforces a sustainable wage floor and shows the reasoning. A human (the artisan) makes the final decision.

## Limitations and known biases
- Training prices are marketplace prices of mostly factory-made goods; handmade premium is not learned (handled by the passport premium rule).
- The Flipkart snapshot is from 2016 (no inflation adjustment); Amazon India (if present) is 2023.
- Categories with few rows ({', '.join(c for c, m in metrics['per_category_test'].items() if m['n'] < 30) or 'none'}) have unreliable per-category error.
- Brand effects in marketplace data (e.g. premium brands) cannot be expressed by an unbranded artisan listing.
- The text encoder is English; regional-language input must be translated upstream (A2's cataloguer does this).
- Environment: {meta['environment']}
"""
    (ROOT / "MODEL_CARD.md").write_text(card)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-tune", dest="tune", action="store_false",
                    help="skip the bounded LightGBM hyper-parameter search")
    ap.add_argument("--select-by", default="MAE", choices=["RMSE", "MAE", "MedAE", "MAPE_pct"],
                    help="validation metric used to pick the model (lower is better; default RMSE)")
    args = ap.parse_args()
    t_all = time.time()
    manifest = json.loads((CACHE / "features_manifest.json").read_text())
    img = np.load(CACHE / "image_emb.npy")
    txt = np.load(CACHE / "text_emb.npy")
    struct = np.load(CACHE / "struct.npy")
    meta_df = pd.read_pickle(CACHE / "meta.pkl").reset_index(drop=True)
    y = meta_df["price"].to_numpy(dtype=np.float64)
    has_img = meta_df["has_image"].to_numpy(dtype=bool)
    split = meta_df["split"].to_numpy()
    tr, va, te = split == "train", split == "val", split == "test"
    cats = meta_df["category"].to_numpy()
    groups = meta_df["group"].to_numpy()

    # ── retrieval block: what the k nearest real listings sell for ─────────────
    # Training rows query the training index with their own name-group removed, so this can never
    # leak a row's own price; val/test/inference query the same index with no exclusion needed
    # (name groups never cross splits).
    t0 = time.time()
    size_col = STRUCT_FEATURES.index("size_cm")
    sizes = struct[:, size_col].astype(np.float64)
    db = dict(db_img=img[tr], db_txt=txt[tr], db_has=has_img[tr], db_cat=cats[tr], db_price=y[tr],
              db_size=sizes[tr])
    cols = [np.empty(len(y)) for _ in EXTRA_FEATURES]
    out = comparable_features(img[tr], txt[tr], has_img[tr], cats[tr], q_groups=groups[tr],
                              db_groups=groups[tr], q_size=sizes[tr], **db)
    for c, v in zip(cols, out):
        c[tr] = v
    for mask in (va, te):
        out = comparable_features(img[mask], txt[mask], has_img[mask], cats[mask],
                                  q_size=sizes[mask], **db)
        for c, v in zip(cols, out):
            c[mask] = v
    knn_med = cols[0]
    extra = np.stack(cols, axis=1).astype(np.float32)
    log(f"retrieval features built in {time.time() - t0:.0f}s "
        f"(median comparable price ₹{np.median(knn_med):,.0f})")

    X = np.hstack([img, txt, struct, extra]).astype(np.float32)
    assert X.shape[1] == len(FEATURE_NAMES), (X.shape, len(FEATURE_NAMES))
    log(f"X={X.shape}  train={tr.sum()} val={va.sum()} test={te.sum()}  image coverage={has_img.mean():.1%}")
    dataset_hash = hashlib.sha256(
        ("\n".join(meta_df["pid"]) + str(y.sum())).encode()).hexdigest()[:16]

    # ── baseline (category median from TRAIN, already stored in the struct block) ──
    med_col = STRUCT_FEATURES.index("category_median_price")
    base_val = regression_metrics(y[va], struct[va, med_col])
    base_test = regression_metrics(y[te], struct[te, med_col])
    log(f"baseline (category median)  val RMSE ₹{base_val['RMSE']:,.0f}  MAE ₹{base_val['MAE']:,.0f}")

    # ── four variants ──────────────────────────────────────────────────────────
    # bounded hyper-parameter search on the log-target LightGBM (the family that wins on relative error)
    tuned = dict(TUNING_GRID[0])
    if args.tune:
        log(f"tuning LightGBM(log) over {len(TUNING_GRID)} fixed configurations …")
        best_tune = None
        for i, cfg in enumerate(TUNING_GRID):
            m, it = fit_variant("lightgbm", "log", X[tr], y[tr], X[va], y[va], overrides=cfg)
            score = regression_metrics(y[va], predict_price(m, "log", X[va]))[args.select_by]
            log(f"  cfg {i + 1}/{len(TUNING_GRID)} {cfg} → val {args.select_by} {score:,.1f} ({it} trees)")
            if best_tune is None or score < best_tune:
                best_tune, tuned = score, dict(cfg)
        log(f"tuned config: {tuned}  (val {args.select_by} {best_tune:,.1f})")

    results, models = [], {}
    for vid, algo, transform in VARIANTS:
        t0 = time.time()
        overrides = tuned if (algo.startswith("lightgbm") and transform == "log") else None
        model, best_iter = fit_variant(algo, transform, X[tr], y[tr], X[va], y[va], overrides)
        secs = round(time.time() - t0, 1)
        m = regression_metrics(y[va], predict_price(model, transform, X[va]))
        results.append({"id": vid, "algo": algo, "target": transform, "val": m,
                        "best_iteration": best_iter, "train_seconds": secs,
                        "hyperparams": ({**LGBM_PARAMS, **(overrides or {})} if algo.startswith("lightgbm")
                                        else dict(XGB_PARAMS))})
        models[vid] = (model, algo, transform)
        log(f"{vid} {algo:8s} {transform:3s}  val RMSE ₹{m['RMSE']:,.0f}  MAE ₹{m['MAE']:,.0f}  "
            f"R² {m['R2']:.3f}  MAPE {m['MAPE_pct']:.1f}%  trees={best_iter}  {secs}s")

    # ── variant F: ensemble of the best log-target models (geometric mean) ─────
    log_ids = [r["id"] for r in sorted((r for r in results if r["target"] == "log"),
                                       key=lambda r: r["val"][args.select_by])][:3]
    ens = [(models[v][0], models[v][2]) for v in log_ids]
    if len(ens) > 1:
        m = regression_metrics(y[va], predict_members(ens, X[va]))
        results.append({"id": "F", "algo": f"ensemble({'+'.join(log_ids)})", "target": "log",
                        "val": m, "best_iteration": sum(r["best_iteration"] for r in results
                                                        if r["id"] in log_ids),
                        "train_seconds": 0.0, "hyperparams": {"members": log_ids}})
        models["F"] = (ens, "ensemble", "log")
        log(f"F ensemble({'+'.join(log_ids)})  log  val RMSE ₹{m['RMSE']:,.0f}  MAE ₹{m['MAE']:,.0f}  "
            f"R² {m['R2']:.3f}  MAPE {m['MAPE_pct']:.1f}%")

    best = min(results, key=lambda r: r["val"][args.select_by])
    model, algo, transform = models[best["id"]]
    members = model if algo == "ensemble" else [(model, transform)]
    log(f"selected {best['id']} ({algo}, {transform}) by validation {args.select_by}")

    def raw_predict(Xs):
        return predict_members(members, Xs)

    # ── post-hoc calibration, fitted on validation only ────────────────────────
    cal = fit_calibration(y[va], raw_predict(X[va]), cats[va])
    cal_val = regression_metrics(y[va], apply_calibration(raw_predict(X[va]), cal, cats[va]))
    raw_val = regression_metrics(y[va], raw_predict(X[va]))
    keep_cal = cal_val[args.select_by] <= raw_val[args.select_by]
    calibration = {**cal, "applied": bool(keep_cal),
                   "validation_before": raw_val, "validation_after": cal_val}
    log(f"calibration: global log(y)≈{cal['global']['a']:.3f}+{cal['global']['b']:.3f}·log(pred), "
        f"{len(cal['per_category'])} per-category lines; val {args.select_by} "
        f"{raw_val[args.select_by]:,.1f} → {cal_val[args.select_by]:,.1f}  (applied: {keep_cal})")

    def predict_final(Xs, cs=None):
        return apply_calibration(raw_predict(Xs), cal if keep_cal else None, cs)

    # ── comparables & blend (val for info, test for reporting) ─────────────────
    knn_val, knn_test = knn_med[va], knn_med[te]
    p_val = predict_final(X[va], cats[va])
    p_test = predict_final(X[te], cats[te])
    grid = {}
    for w in np.round(np.arange(0.0, 1.01, 0.1), 1):
        grid[str(w)] = regression_metrics(y[va], w * p_val + (1 - w) * knn_val)["RMSE"]
    best_w = float(min(grid, key=grid.get))
    # the service uses the validation-optimal weight (clipped to a sane range), not a hard-coded one
    runtime_w = float(np.clip(best_w, 0.5, 1.0))
    blend_test = runtime_w * p_test + (1 - runtime_w) * knn_test

    test = {
        "selected_model": regression_metrics(y[te], p_test),
        "runtime_blend": regression_metrics(y[te], blend_test),
        "knn_comparables": regression_metrics(y[te], knn_test),
        "category_median_baseline": base_test,
    }
    for k, v in test.items():
        log(f"TEST {k:26s} RMSE ₹{v['RMSE']:,.0f}  MAE ₹{v['MAE']:,.0f}  R² {v['R2']:.3f}  "
            f"MedAE ₹{v['MedAE']:,.0f}  MAPE {v['MAPE_pct']:.1f}%")

    per_cat = {}
    cats_te = meta_df.loc[te, "category"].to_numpy()
    for c in sorted(set(cats_te)):
        mk = cats_te == c
        per_cat[c] = regression_metrics(y[te][mk], p_test[mk])

    # ── price band ────────────────────────────────────────────────────────────
    t0 = time.time()
    q_lo = fit_quantile(0.10, X[tr], y[tr], X[va], y[va])
    q_hi = fit_quantile(0.90, X[tr], y[tr], X[va], y[va])
    # Conformalised quantile regression: widen the raw q10/q90 band (in log space) by the
    # validation-set conformity score so that coverage is calibrated to the nominal 80 %.
    def raw_band(Xs):
        a, b = q_lo.predict(Xs), q_hi.predict(Xs)
        return np.minimum(a, b), np.maximum(a, b)
    lo_v, hi_v = raw_band(X[va])
    ly_v = np.log(y[va])
    scores = np.maximum(lo_v - ly_v, ly_v - hi_v)
    level = min(1.0, np.ceil((len(scores) + 1) * BAND_COVERAGE) / len(scores))
    band_offset = float(np.quantile(scores, level, method="higher"))
    lo_t, hi_t = raw_band(X[te])
    raw_cov = float(np.mean((y[te] >= np.exp(lo_t)) & (y[te] <= np.exp(hi_t))) * 100)
    lo, hi = np.exp(lo_t - band_offset), np.exp(hi_t + band_offset)
    band = {"nominal_coverage_pct": BAND_COVERAGE * 100,
            "method": "LightGBM quantile q10/q90 on log price + split-conformal calibration on val",
            "log_offset": round(band_offset, 4),
            "test_coverage_uncalibrated_pct": round(raw_cov, 2),
            "test_coverage_pct": round(float(np.mean((y[te] >= lo) & (y[te] <= hi)) * 100), 2),
            "test_median_width_inr": round(float(np.median(hi - lo)), 2),
            "train_seconds": round(time.time() - t0, 1)}
    log(f"price band: test coverage {band['test_coverage_uncalibrated_pct']}% raw → "
        f"{band['test_coverage_pct']}% calibrated  "
        f"median width ₹{band['test_median_width_inr']:,.0f}")

# ── importance ────────────────────────────────────────────────────────────
    imp_model, imp_algo = (members[0][0], "lightgbm") if algo == "ensemble" else (model, algo)
    imp = importances(imp_model, imp_algo)
    total = imp.sum() or 1.0
    n_struct = len(STRUCT_FEATURES)
    blocks = {"image": imp[:IMG_DIM].sum() / total,
              "text": imp[IMG_DIM:IMG_DIM + TXT_DIM].sum() / total,
              "structured": imp[IMG_DIM + TXT_DIM:IMG_DIM + TXT_DIM + n_struct].sum() / total,
              "retrieval": imp[IMG_DIM + TXT_DIM + n_struct:].sum() / total}
    order = np.argsort(-imp)[:30]
    fi = {"importance_type": "gain",
          "gain_share_by_block": {k: round(float(v), 4) for k, v in blocks.items()},
          "structured_features": {n: round(float(imp[IMG_DIM + TXT_DIM + i] / total), 4)
                                  for i, n in enumerate(STRUCT_FEATURES + EXTRA_FEATURES)},
          "top_features": [(FEATURE_NAMES[i], round(float(imp[i] / total), 4)) for i in order]}

    # ── save ──────────────────────────────────────────────────────────────────
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    created = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    version = f"b1-price-{best['id']}-{algo}-{transform}-{time.strftime('%Y%m%d%H%M%S')}"
    joblib.dump({"bundle_version": 2, "members": members, "algo": algo,
                 "target_transform": transform, "model_id": best["id"],
                 "calibration": calibration if keep_cal else None,
                 "band_lo": q_lo, "band_hi": q_hi, "band_log_offset": band_offset,
                 "feature_names": FEATURE_NAMES, "extra_features": EXTRA_FEATURES,
                 "categorical_idx": CAT_IDX, "blend_model_weight": runtime_w,
                 "version": version}, MODEL_DIR / "model.pkl")
    np.save(MODEL_DIR / "index_img.npy", img[tr])
    np.save(MODEL_DIR / "index_txt.npy", txt[tr])
    index_meta = meta_df.loc[tr, ["pid", "source", "title", "category", "subcategory", "material",
                                  "price", "image_url", "has_image"]].reset_index(drop=True)
    index_meta["size_cm"] = sizes[tr]
    index_meta.to_pickle(MODEL_DIR / "index_meta.pkl")

    meta = {
        "version": version, "created_at": created,
        "model_id": best["id"], "algo": algo, "target_transform": transform,
        "best_iteration": best["best_iteration"], "hyperparams": best["hyperparams"],
        "early_stopping_rounds": EARLY_STOPPING,
        "selection_rule": f"lowest validation {args.select_by} (INR, after inverse transform)",
        "feature_dim": int(X.shape[1]),
        "feature_blocks": {"image": IMG_DIM, "text": TXT_DIM, "structured": STRUCT_FEATURES,
                           "retrieval": EXTRA_FEATURES},
        "image_encoder": manifest.get("image_encoder"), "text_encoder": manifest.get("text_encoder"),
        "n_rows": int(len(y)), "split_sizes": {k: int(v) for k, v in
                                               zip(*np.unique(split, return_counts=True))},
        "dataset_hash": dataset_hash, "comparables_index_rows": int(tr.sum()),
        "k_comparables": K_COMPARABLES, "blend_model_weight": runtime_w,
        "extra_features": EXTRA_FEATURES, "calibration": calibration,
        "ensemble_members": ENSEMBLE_MEMBERS if algo == "ensemble" else None,
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "lightgbm": lgb.__version__, "xgboost": xgb.__version__,
                        "device_used_for_embeddings": manifest.get("device")},
    }
    metrics = {
        "version": version,
        "selected_model": {"id": best["id"], "algo": algo, "target": transform,
                           "selected_by": f"validation {args.select_by}"},
        "validation": results,
        "validation_baseline": base_val,
        "test": test,
        "per_category_test": per_cat,
        "price_band": band,
        "calibration": calibration,
        "blend_weight_grid_val": {"rmse_by_model_weight": grid, "best_model_weight": best_w,
                                  "runtime_model_weight": runtime_w},
    }
    (MODEL_DIR / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    (MODEL_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    (MODEL_DIR / "feature_importance.json").write_text(json.dumps(fi, indent=2))

    reg_path = ROOT / "models" / "experiments.json"
    registry = json.loads(reg_path.read_text()) if reg_path.exists() else []
    for r in results:
        registry.append({"run_version": version, "timestamp": created, "dataset_hash": dataset_hash,
                         "model_id": r["id"], "model_type": r["algo"], "target_transform": r["target"],
                         "hyperparams": r["hyperparams"], "best_iteration": r["best_iteration"],
                         "validation_metrics": r["val"], "train_seconds": r["train_seconds"],
                         "selected": r["id"] == best["id"]})
    reg_path.write_text(json.dumps(registry, indent=2, default=str))

    write_model_card(meta, metrics, fi, manifest)
    log(f"feature gain share: {fi['gain_share_by_block']}")
    log(f"saved {version} → models/price_model/  (total {time.time() - t_all:.0f}s)")


if __name__ == "__main__":
    main()
