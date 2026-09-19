# Pricing Intelligence & Commerce Engine

## 1. Project Overview

Craftly helps artisans and micro-entrepreneurs sell online year-round. **B1 owns the intelligence layer**:
the market-price model, comparable-listing search, the sustainable wage floor, the craft-passport premium,
per-channel pricing, bulk-order capacity allocation with payout splits, and the craft passport + QR object.

This is a working engine, not a mockup:

- Real marketplace data from Kaggle, cleaned and split with a leakage-controlled methodology.
- Frozen pretrained encoders — **OpenCLIP ViT-B/32** for photos, **all-MiniLM-L6-v2** for text.
- A **gradient-boosted price model** trained and evaluated on a held-out test split.
- A **k-NN comparable search** over real marketplace listings, used both as model input and as evidence
  shown to the artisan.
- Business rules implemented as explicit, tested formulas — never learned, never guessed.

Every number in `MODEL_CARD.md`, `models/price_model/metrics.json` and `data/DATA_SOURCES.md` is written by the
scripts from their own runs. Nothing is hand-entered.

---

## 2. What the System Does / End-to-End Flow

```
artisan photo + spoken description (from A2)   buyer / B2B order (from C1)
            │                                              │
            ▼                                              ▼
   ┌──────────────────────┐                     ┌────────────────────────┐
   │ 1  Featurise         │                     │ 6  Capacity allocation │
   │    CLIP + MiniLM     │                     │    500-unit split      │
   │    + structured      │                     │    + payout per artisan│
   └──────────┬───────────┘                     └────────────────────────┘
              ▼
   ┌──────────────────────┐   ┌────────────────────────────────────────┐
   │ 2  Comparable search │──▶│ 3  ML market price + 80 % price band    │
   │    k = 8 real listings│   │    (comparables are also model inputs) │
   └──────────────────────┘   └──────────────────┬─────────────────────┘
                                                 ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │ 4  Business rules: wage floor → floor enforcement → passport      │
   │    premium → D2C / Marketplace / B2B prices + floor check         │
   └──────────────────────────────┬────────────────────────────────────┘
                                  ▼
                    ┌────────────────────────────┐
                    │ 5  Craft passport + QR PNG │
                    └────────────────────────────┘
```

ML and business logic are strictly separated: the model only ever predicts a **market price**. The floor,
premium, channel fees, allocation and passport are deterministic rules in `service.py`.

---

## 3. Inputs & Outputs

**Inputs** (`POST /api/predict`, multipart — an optional image file plus a `data` JSON string):

| Field | Type | Notes |
|---|---|---|
| `image` | file | optional; missing photo → zero image vector and a warning |
| `title` | string | required |
| `description` | string | prices and marketplace boilerplate are scrubbed before embedding |
| `category` | string | one of 10 canonical categories (`GET /api/taxonomy`); close synonyms/typos from another slice's taxonomy (`"jewelry"`, `"decor"`, `"stationary"`, plurals, …) are normalised to the canonical id — see `normalise_category()` in `features.py` |
| `subcategory` | string | optional; unknown values encode as `other` |
| `material` | string | canonical id or free text ("mango wood" → `wood`) |
| `material_cost` | number | ₹, artisan-supplied |
| `labour_hours` | number | hours, artisan-supplied |
| `hourly_wage`, `overhead_pct`, `with_passport_premium` | optional | override the defaults below |

**Outputs**:

- `ai_market_price` and `price_band_80pct` (conformally calibrated)
- `comparables[]` — 8 real listings with price, size, marketplace and similarity
- `comparable_stats` — median, mean, min, max, std, IQR, mean similarity
- `market_estimate`, `floor`, `floor_enforcement`, `passport_premium`
- `recommended_price` and `recommended_price_rounded` (a sellable ₹10/₹50/₹100 step, never below the floor)
- `channels.{d2c, marketplace, b2b}` — buyer price, `artisan_net`, `floor_ok`
- `confidence` — high / medium / low with reasons
- `explanation[]` — plain-language reasoning A2 can translate, plus `warnings[]` and `latency_ms`

---

## 4. ML Architecture

```
product photo ─────► OpenCLIP ViT-B/32 (frozen) ──────────► 512-d  (L2-normalised)
title | description ► all-MiniLM-L6-v2 (frozen) ──────────► 384-d  (L2-normalised)
category / subcategory / material / size / … ─────────────►  13-d  structured
k nearest real listings ──────────────────────────────────►   4-d  retrieval
                                                            ─────
                                          concatenated →     913-d
                                                              │
                                    LightGBM / XGBoost regressor (log or raw target)
                                                              │
                                    validation-fitted calibration → AI market price
                                    LightGBM quantile q10/q90 + conformal → 80 % band
```

**Structured block (13):** `category_enc`, `subcategory_enc`, `material_enc`, `title_word_count`,
`desc_word_count`, `has_image`, `category_median_price`, `category_price_std`, `subcategory_median_price`,
`material_median_price`, `size_cm`, `has_size`, `pack_count`.

**Retrieval block (4):** `comparable_median_price`, `comparable_mean_similarity`, `comparable_top1_price`,
`comparable_iqr_ratio`. A training row queries the index with its own name-group removed, so it can never see
its own price.

**Comparable search:** cosine similarity over both embeddings,
`score = (0.4·cos(image) + 0.6·cos(text)) / 1.0`, text-only when either side has no photo. Ranking prefers the
same category, and a size-affinity multiplier pulls down listings of a very different physical size. Median of
the top k = 8 is the comparable price.

**Training procedure (`train.py`):**

1. Bounded, fixed 8-configuration grid search for the log-target LightGBM (reproducible; `--no-tune` skips it).
2. Six candidates — LightGBM/XGBoost × raw/log price, a Huber-loss LightGBM, and an ensemble (geometric mean of
   the three best log-target models).
3. Selection on **validation** by MAE in ₹ (`--select-by RMSE|MAE|MedAE|MAPE_pct`).
4. Post-hoc calibration `log(y) = a + b·log(pred)` fitted on validation only — per category where there are
   enough validation rows, global elsewhere — applied only if it improves the selection metric.
5. The selected model is scored **once** on the held-out test split.

**Leakage controls:** MRP/discount, ratings and marketplace flags are excluded entirely; prices and boilerplate
are scrubbed from descriptions before embedding; all price anchors and category statistics are computed on the
training split only and read from saved tables at inference; colour/size variants of one product share a
normalised name key so they never cross splits.

---

## 5. Pricing Logic and Formulas

All parameters are named constants in `service.py` and can be overridden per request.

```
Defaults: hourly_wage ₹60   overhead 15 %   passport premium 5 %   k comparables 8

1  AI market price        model prediction, calibrated              ai_market_price
2  Comparable median      median price of the k = 8 nearest listings comparable_median
3  Market estimate        w · ai_market_price + (1 − w) · comparable_median
                          w is chosen on validation at training time (metadata.blend_model_weight),
                          but leaned further toward the comparables when the model and the
                          comparables disagree by more than 2× AND the comparables are themselves
                          trustworthy (mean similarity ≥ 0.4) — real transacted prices outrank a
                          model that is likely extrapolating, capped so w never drops below half its
                          trained value (model.blend_model_weight_used in the API response)

4  Sustainable floor      labour_cost = labour_hours × hourly_wage
                          subtotal    = material_cost + labour_cost
                          overhead    = 15 % × subtotal
                          floor       = subtotal + overhead

5  Floor enforcement      base_price  = max(market_estimate, floor)          (floor_binding flag)
6  Passport premium       recommended = base_price × 1.05

7  Channel pricing
   D2C           price = recommended + ₹50 + 2.5 % × recommended
                 artisan_net = recommended
   Marketplace   base  = recommended / (1 − 15 %)
                 price = base + ₹80 + 3 % × base
                 artisan_net = price − packaging − payment fee − commission = recommended
   B2B           unit  = max(recommended × (1 − 20 %), floor)   ← discount never breaks the floor
                 price = unit + ₹30 logistics
                 artisan_net = unit
```

Every channel returns `artisan_net`, the `floor`, and a boolean `floor_ok` — the visible floor check. When the
20 % wholesale discount would pay the artisan below the floor, the unit price is capped at the floor and
`discount_capped_by_floor` is set with the `effective_discount_pct` actually applied.

A worked example is in the tests: `material_cost ₹1000`, `labour_hours 10` → `(1000 + 600) × 1.15 = ₹1840`.

---

## 6. Bulk Order Allocation

**Deterministic Capacity-Priority Allocation** (`allocate_bulk_order`):

1. Keep artisans whose `compatible_categories` contain the product category and whose capacity > 0.
2. Cap each artisan at what their `units_per_day` can deliver by `deadline_days` (when both are given).
3. Sort by effective capacity descending, then artisan id ascending — deterministic, order-independent.
4. Greedily assign `min(effective_capacity, remaining)` to each artisan.
5. Report allocations, per-artisan ETA, share %, payout, shortfall, and whether the deadline is met.

A 500-unit `home_decor` order against `demo_artisans.json` (30-day deadline):

| Artisan | Region | Units | Share | ETA | Payout |
|---|---|---|---|---|---|
| A002 Ramesh Kumar | Moradabad, Uttar Pradesh | 150 | 30.0 % | 30 d | ₹131,455.50 |
| A003 Fatima Begum | Channapatna, Karnataka | 140 | 28.0 % | 28 d | ₹122,691.80 |
| A004 Suresh Prajapati | Khurja, Uttar Pradesh | 130 | 26.0 % | 29 d | ₹113,928.10 |
| A006 Anil Oraon | Bastar, Chhattisgarh | 80 | 16.0 % | 27 d | ₹70,109.60 |

`allocated 500/500, shortfall 0, eligible artisans 6, total payout ₹438,185.00, pooled capacity within
deadline 660 units.` Payout per artisan is `units × B2B artisan_net`. Ineligible artisans are returned with a
reason (category mismatch, no capacity, or cannot deliver within the deadline).

---

## 7. Craft Passport + QR

`create_passport()` writes a JSON passport and a scannable QR PNG to `data/passports/`:

- `passport_id` — `CP-<timestamp>-<sequence>`
- `product` — title, category, subcategory, material, making-time hours, description
- `artisan` — id, name, region, craft cluster
- `pricing` — `ai_market_price`, `sustainable_floor`, `recommended_price`, `model_version`
- `provenance` — `{"available": false, "note": "Provenance not provided by artisan"}` when the artisan supplied
  none. **Provenance is never fabricated.**
- `verify_url` — `{B1_PUBLIC_BASE_URL}/api/passport/{id}`, which is what the QR encodes

`GET /api/passport/{id}` returns the JSON; `GET /api/passport/{id}/qr` returns the PNG. Passport ids are
validated against path traversal.

---

## 8. Dataset, Training Setup, and Actual Model Results

**Data**

- Primary: [PromptCloudHQ/flipkart-products](https://www.kaggle.com/datasets/PromptCloudHQ/flipkart-products) (CC0)
- Optional enrichment: [asaniczka/amazon-india-products-2023-1-5m-products](https://www.kaggle.com/datasets/asaniczka/amazon-india-products-2023-1-5m-products) (ODbL) — the pipeline runs fully without it
- Target: the observed selling price in ₹ (`discounted_price` / `price`), never MRP
- Cleaning: mapped onto 10 craft categories; electronics, appliances and certified gold/diamond jewellery
  dropped; price window ₹30–₹50,000; duplicates removed; every count recorded in `data/reports/data_quality.json`
  and `data/DATA_SOURCES.md`

**Evaluation methodology**

70 / 15 / 15 split, stratified by category and **grouped by normalised product name** (`StratifiedGroupKFold`,
20 folds, seed 42), so colour and size variants never cross splits. Validation selects the model and fits the
calibration; the test split is scored once. Metrics are always in ₹ after inverse-transforming log predictions.

**Results — last full-dataset run** (11,866 products, 90.5 % image coverage; train 8,296 / val 1,779 / test 1,791).
Every training run rewrites `MODEL_CARD.md`, `metrics.json`, `metadata.json`, `feature_importance.json` and
appends to `models/experiments.json` — **those files are the source of truth**.

| Test set (n = 1,791) | MAE | RMSE | R² | MedAE | MAPE |
|---|---|---|---|---|---|
| Selected model | ₹316 | ₹871 | 0.658 | ₹127 | 41.4 % |
| Runtime blend (model + comparables) | ₹320 | ₹839 | 0.683 | ₹128 | 41.0 % |
| k-NN comparables alone | ₹357 | ₹1,133 | 0.422 | ₹130 | 42.5 % |
| Category-median baseline (no ML) | ₹443 | ₹1,430 | 0.079 | ₹199 | 50.8 % |

- 80 % price band: **80.2 % measured coverage** on the test split after conformal calibration.
- Feature importance by block (gain): retrieval 33 %, text 30 %, **image 22 %**, structured 15 %.
- The model card also reports per-category test error and the full validation table for every candidate.

---

## 9. Repository Structure

```
engines/
├── scripts/
│   ├── data.py              stage 1 — download (Kaggle CLI), verify, clean, taxonomy, 70/15/15 split
│   ├── features.py          stage 2 — images, CLIP + MiniLM embeddings, structured features
│   │                                  (also the shared feature code used at inference)
│   ├── train.py             stage 3 — tuning, 6 candidates, selection, calibration, band, model card
│   ├── service.py           stage 4 — inference, comparables, business rules, allocation,
│   │                                  passport + QR, FastAPI app, CLI demo, selftest
│   └── test.py              38 tests — business rules, features, ML artifacts, full API run
├── demo_artisans.json      fictional artisan roster (capacity, categories, units_per_day)
├── requirements.txt
├── MODEL_CARD.md                         generated by train.py
├── data/
│   ├── raw/                              downloaded CSVs
│   ├── processed/products.pkl            cleaned table + preview CSV
│   ├── cache/*.npy, meta.pkl             embeddings, structured features, manifest
│   ├── images/<pid>.jpg                  downloaded product photos
│   ├── reports/data_quality.json         every measured count, checksum, schema check
│   ├── DATA_SOURCES.md                   generated from the quality report
│   └── passports/CP-*.json / .png        issued passports + QR codes
└── models/
    ├── price_model/model.pkl             selected model(s) + calibration + band models
    ├── price_model/{metadata,metrics,feature_importance}.json
    ├── price_model/{encoders,category_stats}.pkl        fit on TRAIN only
    ├── price_model/index_{img,txt}.npy, index_meta.pkl  comparable-search index
    └── experiments.json                  append-only experiment registry
```

`data.py`, `train.py` and `service.py` import their feature code from `features.py` so that training and
inference featurise a product with exactly the same code. All five scripts live in `scripts/` and are run
from the `engines/` directory (e.g. `python scripts/data.py`); paths such as `data/`, `models/` and
`demo_artisans.json` still resolve relative to `engines/`, not to `scripts/`.

---

## 10. Installation and Setup

Python 3.10+ (tested on 3.11 and 3.14). Apple Silicon (MPS) and CUDA are both supported.

```bash
cd engines
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### Kaggle API Setup

The pipeline downloads the datasets automatically using the Kaggle CLI. Go to **Kaggle → Settings → API** and generate a new API token. Then configure it locally:

```bash
mkdir -p ~/.kaggle
echo "YOUR_KAGGLE_TOKEN" > ~/.kaggle/access_token
chmod 600 ~/.kaggle/access_token
```

**Manual data fallback** — download from the Kaggle pages above and place the files here, then run
`python scripts/data.py verify preprocess`:

```
data/raw/flipkart/flipkart_com-ecommerce_sample.csv
data/raw/amazon_in/*.csv          (optional)
```

**Useful environment variables**

| Variable | Effect |
|---|---|
| `B1_DEVICE` | `auto` / `cpu` / `mps` / `cuda` (the service defaults to `cpu`) |
| `B1_ENCODER_WORKER` | `auto` (separate encoder process on macOS) / `1` / `0` |
| `B1_DISABLE_IMAGE_ENCODER` | `1` → text-only pricing, no CLIP |
| `B1_TEXT_MODEL`, `B1_CLIP_PRETRAINED` | local encoder checkpoints for offline use |
| `B1_PUBLIC_BASE_URL` | base URL encoded in passport QR codes |

---

## 11. How to Run the Complete Pipeline

```bash
python scripts/data.py                 # download → verify → clean → split
python scripts/features.py             # images + CLIP + MiniLM + structured features
python scripts/train.py                # tune, train, select, calibrate, evaluate, write MODEL_CARD.md
python scripts/service.py selftest     # 10 contract checks — exit 0 = safe to lock in
pytest -q scripts/test.py              # 38 tests
python scripts/service.py demo         # end-to-end demo on a held-out real product
python scripts/service.py serve        # API on :8000 (or: uvicorn service:app --port 8000 --app-dir scripts)
```

Useful flags:

```bash
python scripts/data.py --skip-amazon                 # Flipkart only
python scripts/data.py verify preprocess             # after a manual download
python scripts/features.py --no-images               # skip the image download
python scripts/features.py --download-encoders       # fetch encoder weights once, with progress
python scripts/train.py --select-by RMSE             # different selection metric
python scripts/train.py --no-tune                    # skip the hyper-parameter grid
python scripts/service.py demo --image my.jpg --title "..." --category home_decor \
       --material brass --material-cost 400 --labour-hours 10
python scripts/service.py serve --device cpu --port 8000
```

`selftest` verifies: artifacts load, feature width matches the trained width, taxonomy is complete, identical
requests give identical prices, a cheap item prices below an expensive one, the wage floor binds and holds on
all three channels, comparables are real training listings, a 500-unit order splits with shares summing to
100 %, the passport and QR write and read back, and the held-out metrics stay within thresholds
(`--max-mape`, `--min-r2`). Pin the version string it prints for the rest of the system.

---

## 12. API Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/predict` | multipart: optional `image` + `data` JSON → full pricing result |
| `POST` | `/api/bulk-order` | `{product_category, quantity, unit_payout?, deadline_days?, artisans?}` → allocation, ETAs, payout split |
| `POST` | `/api/passport` | `{product, artisan, pricing, provenance?}` → passport + QR path |
| `GET` | `/api/passport/{id}` | passport JSON |
| `GET` | `/api/passport/{id}/qr` | QR code PNG |
| `GET` | `/api/taxonomy` | accepted categories, subcategories, materials |
| `GET` | `/api/model/info` | model metadata, metrics, taxonomy, business parameters |
| `GET` | `/api/health` | service and artifact status |
| `GET` | `/` | redirects to the interactive Swagger docs at `/docs` |
| `POST` | `/price` | `{listing, artisan, inventory, channel, quantity}` → `PriceQuote` JSON |
| `POST` | `/split` | `{listing, artisan, inventory, quantity, deadline_days}` → `SplitPlan` JSON |

```bash
curl -X POST localhost:8000/api/predict \
  -F image=@vase.jpg \
  -F 'data={"title":"Hand-painted terracotta flower vase","description":"25 cm, hand painted",
            "category":"home_decor","material":"terracotta","material_cost":120,"labour_hours":4}'

curl -X POST localhost:8000/api/bulk-order -H 'content-type: application/json' \
  -d '{"product_category":"home_decor","quantity":500,"unit_payout":700,"deadline_days":30}'
```

**`/price` and `/split`** are thin aliases over the same pricing/allocation logic above, shaped to match
C1's `HttpPriceEngine` / `HttpOrderEngine` adapter (`market/app/adapters/http_platform.py`), which speaks
`PriceQuote` / `SplitPlan` (`market/app/contracts.py`) rather than B1's native `/api/*` shapes. `channel`
(`own_store` / `mela_qr` / `b2b` / `amazon` / `flipkart` / `ebay`) maps onto the `d2c` / `b2b` / `marketplace`
buckets from §5; a listing missing `material_cost_inr` or `hours_worked` still prices (treated as `0`) but
comes back with `floor_incomplete: true`, matching the contract's "do not sell until the artisan is asked"
rule. `/split` prices the listing on the `b2b` channel to get a per-unit payout, then runs the same
`allocate_bulk_order` as `/api/bulk-order` against `demo_artisans.json`.

```bash
curl -X POST localhost:8000/price -H 'content-type: application/json' -d '{
  "listing": {"listing_id":"L1","artisan_id":"A1","title_en":"Brass diya lamp","category":"home_decor",
              "material":"brass","material_cost_inr":250,"hours_worked":6},
  "artisan": {"artisan_id":"A1","name":"Lakshmi Devi"},
  "channel": "own_store", "quantity": 1}'
```

---

## 13. Example / Demo Output

Excerpt from `python scripts/service.py demo` — a held-out **test-split** product the model never trained on:

```
1. PRODUCT INPUT
title        : Adaa Terracotta - Bulge Green Ganesha Statue - Green Showpiece  -  6 cm
category     : home_decor / Showpieces   material: terracotta_ceramic
photo        : data/images/fk_0238c6178c72d8bcfddbab831d726bce.jpg
artisan costs: materials ₹250, 6 hours  (demo inputs)

2. FEATURES + MODEL
feature dims : {'image': 512, 'text': 384, 'structured': 13, 'retrieval': 4, 'total': 913}
test metrics : MAE ₹316  RMSE ₹871  R² 0.658  MedAE ₹127  MAPE 41.4%   (from metrics.json)

3. AI MARKET PRICE
model prediction : ₹1,012.60   80% band ₹460–₹2,222

4. COMPARABLE PRODUCTS (training index, real listings)
  0.608  ₹      899  flipkart   Exotic India Baby Ganesha Granting Abhaya Showpiece - 6.x cm
  0.585  ₹    1,380  flipkart   Ruchiworld Vighneshvara Ganpati Showpiece  -  5 cm
  0.601  ₹      699  flipkart   Arghyam Ganesha Showpiece  -  4.2 cm
  ...
stats: median ₹1,074  mean ₹1,403  min ₹590  max ₹2,860  IQR ₹774–₹1,722

5. PRICE RECOMMENDATION
market estimate   : ₹1,043.30
sustainable floor : ₹701.50  = (₹250 + 6×₹60) × 1.15
base price        : ₹1,043.30  (floor binding: False)
recommended       : ₹1,095.46  (passport premium ₹52.16)
shown to artisan  : ₹1,100   confidence: medium (3/4)

6. CHANNEL PRICES
  d2c          buyer pays ₹ 1,172.85   artisan nets ₹ 1,095.46   floor ₹702 ✓
  marketplace  buyer pays ₹ 1,407.44   artisan nets ₹ 1,095.46   floor ₹702 ✓
  b2b          buyer pays ₹   906.37   artisan nets ₹   876.37   floor ₹702 ✓

7. BULK ORDER — 500 units
  allocated 500/500  shortfall 0  eligible artisans 6  total payout ₹438,185.00
  deadline 30 days → completes in 30 days (meets deadline: True)

8. CRAFT PASSPORT + QR
  passport CP-…  →  data/passports/CP-….png
```

The demo prints the held-out product's real selling price alongside the prediction, so the model is always shown
against ground truth it never saw.

---

## 14. Testing

```bash
pytest -q scripts/test.py              # 38 tests
python scripts/service.py selftest     # 10 contract checks on the trained artifacts
```

Covered:

- **Business rules** — wage-floor arithmetic, floor enforcement both ways, passport premium, D2C / marketplace /
  B2B formulas, the B2B floor cap, retail rounding.
- **Allocation** — capacity and compatibility respected, deterministic and order-independent, exact quantities,
  shortfall reporting, payout shares summing to 100 %, deadline-aware capacity caps.
- **Feature definitions** — price-leakage scrubbing, material parsing, size and pack parsing, size-aware
  ranking, structured-row shape, similarity bounds and the missing-photo rule.
- **ML artifacts** — split integrity (no name group crosses splits), feature dimensions, zero vectors for
  missing photos, category statistics computed on the training split only, model metadata and metrics present.
- **Inference** — deterministic predictions, price band contains the estimate, comparables trace to real
  training rows, confidence and rounded price present.
- **Integration** — a full FastAPI run: predict with an image upload, bulk order, passport creation, passport
  and QR retrieval, and input validation.

Tests that need trained artifacts skip automatically when they are absent, and the image branch is exercised
only when the CLIP weights are already cached, so a test run never blocks on a download.