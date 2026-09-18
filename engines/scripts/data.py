"""
Craftly B1 — Step 1: datasets  (download → verify → clean → split)

    python data.py                 # all three stages
    python data.py download        # Kaggle CLI download only
    python data.py verify          # schema / checksum / price-format / image-URL probe report
    python data.py preprocess      # clean, harmonise, split → data/processed/products.pkl
    python data.py --skip-amazon   # Flipkart only (the pipeline must work with it alone)

Datasets
    PRIMARY  (required)  PromptCloudHQ/flipkart-products                     CC0
    OPTIONAL (enrich)    asaniczka/amazon-india-products-2023-1-5m-products   ODbL

Download uses the Kaggle CLI that is already configured on this machine (~/.kaggle).
This script never reads, prints or stores credentials.

Manual fallback (if the CLI is unavailable): download the CSVs from the Kaggle web UI
and place them in
    data/raw/flipkart/   → flipkart_com-ecommerce_sample.csv
    data/raw/amazon_in/  → the CSV file(s) of the Amazon India dataset
then run `python data.py verify preprocess`.

Outputs
    data/processed/products.pkl     one row per product, columns:
        pid, source, title, description, category, subcategory, material, brand,
        price (observed selling price, ₹), image_url, group, split
    data/reports/data_quality.json  everything measured during verify/preprocess
    data/DATA_SOURCES.md            generated from the measured numbers (no hand-typed stats)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import StratifiedGroupKFold

from features import (CATEGORIES, HTTP_HEADERS, MATERIAL_SPEC_KEYS, UNKNOWN,
                         _candidate_urls, material_from_text, scrub_text)

ROOT = Path(__file__).resolve().parent.parent  # engines/ (this file lives in engines/scripts/)
RAW = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
REPORTS = ROOT / "data" / "reports"
QUALITY = REPORTS / "data_quality.json"
SEED = 42

DATASETS = {
    "flipkart": {
        "slug": "PromptCloudHQ/flipkart-products",
        "url": "https://www.kaggle.com/datasets/PromptCloudHQ/flipkart-products",
        "license": "CC0: Public Domain",
        "required": True,
        "expected_columns": ["uniq_id", "product_name", "product_category_tree", "retail_price",
                             "discounted_price", "image", "description", "brand",
                             "product_specifications"],
        "target": "discounted_price",
    },
    "amazon_in": {
        "slug": "asaniczka/amazon-india-products-2023-1-5m-products",
        "url": "https://www.kaggle.com/datasets/asaniczka/amazon-india-products-2023-1-5m-products",
        "license": "ODbL (as listed on the Kaggle page)",
        "required": False,
        "expected_columns": ["title", "price", "imgUrl", "category_id"],
        "target": "price",
    },
}

# Price window for the artisan market segment (₹). Documented, configurable.
MIN_PRICE, MAX_PRICE = 30.0, 50_000.0
AMAZON_MAX_ROWS = 15_000
AMAZON_MAX_PER_CATEGORY = 2_500
SPLIT_FOLDS, TEST_FOLDS, VAL_FOLDS = 20, 3, 3          # → 70 / 15 / 15

# Flipkart top-level category → canonical craft category (None = not an artisan product line)
FLIPKART_CATEGORY_MAP = {
    "Clothing": "apparel",
    "Jewellery": "jewellery",
    "Footwear": "footwear",
    "Kitchen & Dining": "kitchen_dining",
    "Home & Kitchen": "kitchen_dining",
    "Home Decor & Festive Needs": "home_decor",
    "Home Furnishing": "home_furnishing",
    "Bags, Wallets & Belts": "bags_accessories",
    "Toys & School Supplies": "toys_games",
    "Pens & Stationery": "stationery",
    "Furniture": "furniture",
}

# Amazon category name → canonical category. Ordered; first regex match wins.
AMAZON_CATEGORY_RULES = [
    ("jewellery",        r"jewel|necklace|earring|bangle|bracelet|anklet|\brings?\b|pendant|mangalsutra|nose ?pin"),
    ("footwear",         r"sandal|footwear|shoe|jutti|mojari|slipper|flip.?flop|kolhapuri"),
    ("bags_accessories", r"handbag|\bbags?\b|wallet|clutch|belt|tote|purse|pouch"),
    ("apparel",          r"saree|sari\b|kurta|kurti|ethnic|dupatta|shawl|stole|lehenga|salwar|dress|clothing|shirt|tops?\b|scar(f|ves)|sweater|jacket"),
    ("home_furnishing",  r"bedsheet|bedding|bed linen|cushion|curtain|\brugs?\b|carpet|\bmats?\b|quilt|blanket|towel|table linen|furnishing|dohar|pillow"),
    ("kitchen_dining",   r"kitchen|dining|cookware|serveware|\bmugs?\b|tableware|glassware|pottery|dinnerware|drinkware|bakeware"),
    ("home_decor",       r"decor|showpiece|figurine|sculpture|idol|wall art|painting|vases?\b|candle|handicraft|artificial (flower|plant)|clock|lamp|lantern|diya|home accent"),
    ("toys_games",       r"\btoys?\b|puzzle|board game|doll"),
    ("stationery",       r"stationery|notebook|diar(y|ies)|\bpaper\b|\bpens?\b|arts? (&|and) crafts?|craft supplies"),
    ("furniture",        r"furniture"),
]
_AMZ_RX = [(c, re.compile(rx, re.I)) for c, rx in AMAZON_CATEGORY_RULES]

# Rows that are not artisan-segment products even inside craft categories (documented domain filters)
EXCLUDE_FINE_JEWELLERY = True      # certified gold / diamond / platinum pieces priced by metal weight
FINE_JEWELLERY_RX = re.compile(r"\bdiamond\b|\b\d{1,2}\s?kt?\b|\bcarat\b|\bsolitaire\b|\bplatinum\b|\bhallmark", re.I)
NON_ARTISAN_RX = re.compile(
    r"\b(?:microwave|oven|otg|mixer|grinder|juicer|blender|toaster|induction|refrigerator|kettle|"
    r"cooktop|chimney|water purifier|vacuum|trimmer|shaver|hair dryer|straightener|calculator|"
    r"usb|bluetooth|charger|battery|biometric|led tv|speaker|headphone|earphone|power bank)\b", re.I)

_COLOUR_WORDS = (r"black|white|red|blue|green|yellow|pink|purple|orange|brown|grey|gray|gold|silver|"
                 r"beige|maroon|navy|multicolou?r|multi|cream|violet|magenta|turquoise|peach|khaki|"
                 r"olive|mustard|off white|dark|light")
_SIZE_WORDS = r"\b(xs|s|m|l|xl|xxl|xxxl|free size|small|medium|large|pack|set|of|combo|pcs|pieces?)\b"


def log(msg: str) -> None:
    print(f"[data] {msg}", flush=True)


def load_quality() -> dict:
    return json.loads(QUALITY.read_text()) if QUALITY.exists() else {}


def save_quality(q: dict) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    QUALITY.write_text(json.dumps(q, indent=2, default=str))


# ═════════════════════════════════════════════════════════════════════════════
# 1. DOWNLOAD
# ═════════════════════════════════════════════════════════════════════════════
def _csvs(name: str) -> list[Path]:
    return sorted((RAW / name).glob("**/*.csv"))


def download(skip_amazon: bool) -> None:
    kaggle = shutil.which("kaggle")
    q = load_quality()
    q.setdefault("download", {})
    for name, spec in DATASETS.items():
        if name == "amazon_in" and skip_amazon:
            continue
        dest = RAW / name
        dest.mkdir(parents=True, exist_ok=True)
        if _csvs(name):
            log(f"{name}: CSV already present in {dest} — skipping download")
            q["download"].setdefault(name, {"method": "pre-existing files"})
            continue
        if kaggle is None:
            msg = (f"{name}: Kaggle CLI not found. Download manually from {spec['url']} "
                   f"and put the CSV(s) in {dest}")
            if spec["required"]:
                sys.exit(msg)
            log(msg + " (optional dataset — continuing without it)")
            continue
        log(f"{name}: kaggle datasets download {spec['slug']} …")
        t0 = time.time()
        # Positional slug works with Kaggle CLI 1.5+ and 2.x. Output is not echoed
        # beyond the return code so nothing credential-related can leak into logs.
        proc = subprocess.run([kaggle, "datasets", "download", spec["slug"], "-p", str(dest), "-q"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["unknown error"]
            msg = f"{name}: Kaggle download failed ({err[0][:200]}). Manual fallback: {spec['url']} → {dest}"
            if spec["required"]:
                sys.exit(msg)
            log(msg + " (optional dataset — continuing without it)")
            continue
        for z in dest.glob("*.zip"):
            with zipfile.ZipFile(z) as zf:
                zf.extractall(dest)
            z.unlink()
        q["download"][name] = {"method": "kaggle CLI", "slug": spec["slug"],
                               "date": time.strftime("%Y-%m-%d"), "seconds": round(time.time() - t0, 1)}
        log(f"{name}: downloaded {[p.name for p in _csvs(name)]} in {time.time() - t0:.0f}s")
    save_quality(q)


# ═════════════════════════════════════════════════════════════════════════════
# 2. VERIFY
# ═════════════════════════════════════════════════════════════════════════════
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _price_format(series: pd.Series) -> dict:
    s = series.dropna()
    as_str = s.astype(str)
    parsed = parse_price(s)
    return {
        "dtype": str(series.dtype),
        "non_null": int(len(s)),
        "has_rupee_symbol": bool(as_str.str.contains("₹").any()),
        "has_commas": bool(as_str.str.contains(",").any()),
        "unparseable": int(parsed.isna().sum()),
        "zero_or_negative": int((parsed <= 0).sum()),
        "min": float(parsed.min()) if parsed.notna().any() else None,
        "median": float(parsed.median()) if parsed.notna().any() else None,
        "max": float(parsed.max()) if parsed.notna().any() else None,
    }


def _probe_urls(urls: list[str], n: int = 100) -> dict:
    import requests
    from concurrent.futures import ThreadPoolExecutor
    urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
    sample = random.Random(SEED).sample(urls, min(n, len(urls)))

    def probe(u):
        for cand in _candidate_urls(u):
            try:
                r = requests.get(cand, headers=HTTP_HEADERS, timeout=10, stream=True)
                ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("image")
                r.close()
                if ok:
                    return cand.split("/")[2]
            except Exception:
                pass
        return None

    with ThreadPoolExecutor(16) as ex:
        hosts = list(ex.map(probe, sample))
    ok = [h for h in hosts if h]
    by_host: dict[str, int] = {}
    for h in ok:
        by_host[h] = by_host.get(h, 0) + 1
    return {"tested": len(sample), "accessible": len(ok),
            "accessible_pct": round(100 * len(ok) / max(1, len(sample)), 1),
            "served_by": by_host}


def verify(probe_images: bool = True) -> None:
    q = load_quality()
    q["verify"] = {}
    for name, spec in DATASETS.items():
        files = _csvs(name)
        if not files:
            if spec["required"]:
                sys.exit(f"{name}: no CSV in {RAW / name}. Run `python data.py download` "
                         f"or place the file there manually ({spec['url']}).")
            q["verify"][name] = {"available": False}
            log(f"{name}: not available (optional)")
            continue
        info = {"available": True, "files": []}
        for f in files:
            head = pd.read_csv(f, nrows=5, low_memory=False)
            info["files"].append({"name": f.name, "bytes": f.stat().st_size,
                                  "sha256": _sha256(f), "columns": list(head.columns)})
        main = _main_csv(name, files)
        # count rows in chunks (the Amazon file is ~1.5M rows); inspect the first chunk in detail
        rows, df = 0, None
        for chunk in pd.read_csv(main, chunksize=200_000, low_memory=False):
            chunk = chunk.dropna(how="all")
            rows += len(chunk)
            if df is None:
                df = chunk
        cols = list(df.columns)
        missing = [c for c in spec["expected_columns"] if c not in cols]
        pcol = _price_col(name, df)
        info.update({
            "main_file": main.name, "rows": int(rows), "columns": cols,
            "expected_columns_missing": missing,
            "price_format": _price_format(df[pcol]) if pcol else None,
            "price_format_scope": "all rows" if rows == len(df) else f"first {len(df)} rows",
        })
        if probe_images:
            img_col = "image" if name == "flipkart" else _find_col(df, ["imgUrl", "image", "img_link", "image_url"])
            if img_col:
                urls = df[img_col].map(first_image_url).tolist() if name == "flipkart" else df[img_col].tolist()
                log(f"{name}: probing 100 image URLs …")
                info["image_url_probe"] = _probe_urls(urls)
        q["verify"][name] = info
        log(f"{name}: {info['rows']} rows, {len(cols)} columns, missing expected={missing}, "
            f"image probe={info.get('image_url_probe')}")
        if missing and spec["required"]:
            sys.exit(f"{name}: schema differs from expectation — missing {missing}")
        del df
    save_quality(q)


# ═════════════════════════════════════════════════════════════════════════════
# 3. PREPROCESS
# ═════════════════════════════════════════════════════════════════════════════
def parse_price(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce").astype(float)
    cleaned = s.astype(str).str.replace(r"[₹,\s]|Rs\.?|INR", "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce").astype(float)


def first_image_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        urls = json.loads(value)
        return urls[0] if urls else None
    except Exception:
        m = re.search(r"https?://[^\s\"',\]]+", value)
        return m.group(0) if m else None


def parse_tree(value) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        path = json.loads(value)[0]
    except Exception:
        path = value.strip("[]\"")
    return [p.strip() for p in path.split(">>") if p.strip()]


def parse_specs(value) -> dict[str, str]:
    if not isinstance(value, str):
        return {}
    pairs = re.findall(r'"key"=>"([^"]*)",\s*"value"=>"([^"]*)"', value)
    return {k.strip().lower(): v.strip() for k, v in pairs}


def material_from_row(specs: dict, title: str, description: str) -> str:
    for key in MATERIAL_SPEC_KEYS:
        if key in specs and specs[key]:
            m = material_from_text(specs[key])
            if m != UNKNOWN:
                return m
    return material_from_text(title, (description or "")[:600])


def group_key(title: str) -> str:
    """Normalised product name: colour/size/pack variants of one product share a key."""
    t = title.lower()
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(_COLOUR_WORDS, " ", t)
    t = re.sub(_SIZE_WORDS, " ", t)
    t = re.sub(r"[^a-z]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _find_col(df: pd.DataFrame, names: list[str]) -> str | None:
    lower = {c.lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def _price_col(name: str, df: pd.DataFrame) -> str | None:
    if name == "flipkart":
        return "discounted_price" if "discounted_price" in df.columns else None
    # observed selling price — never list price / MRP
    return _find_col(df, ["price", "discount_price", "discounted_price", "selling_price", "sale_price"])


def _main_csv(name: str, files: list[Path]) -> Path:
    if name == "flipkart":
        for f in files:
            if "flipkart" in f.name.lower():
                return f
    return max(files, key=lambda p: p.stat().st_size)


def load_flipkart() -> tuple[pd.DataFrame, dict]:
    files = _csvs("flipkart")
    df = pd.read_csv(_main_csv("flipkart", files), low_memory=False)
    st = {"raw_rows": int(len(df))}
    df = df.dropna(how="all").dropna(subset=["uniq_id", "product_name"])
    st["non_empty_rows"] = int(len(df))
    tree = df["product_category_tree"].map(parse_tree)
    l0 = tree.str[0]
    l1 = tree.map(lambda t: t[1] if len(t) > 1 else UNKNOWN)
    l2 = tree.map(lambda t: t[2] if len(t) > 2 else "")
    df["category"] = l0.map(FLIPKART_CATEGORY_MAP)
    st["top_level_counts"] = l0.value_counts().head(30).to_dict()
    st["dropped_non_craft_category"] = int(df["category"].isna().sum())
    fine = df["category"].isin(["apparel", "footwear"])
    df["subcategory"] = np.where(fine & (l2 != ""), l1 + " / " + l2, l1)
    specs = df["product_specifications"].map(parse_specs)
    out = pd.DataFrame({
        "pid": "fk_" + df["uniq_id"].astype(str),
        "source": "flipkart",
        "title": df["product_name"].astype(str).str.strip(),
        "description": df["description"].fillna("").astype(str),
        "category": df["category"],
        "subcategory": df["subcategory"],
        "specs": specs,
        "brand": df["brand"].fillna(""),
        "price": parse_price(df["discounted_price"]),
        "image_url": df["image"].map(first_image_url),
    })
    return out.dropna(subset=["category"]), st


def load_amazon(max_rows: int) -> tuple[pd.DataFrame | None, dict]:
    files = _csvs("amazon_in")
    if not files:
        return None, {"available": False}
    main = _main_csv("amazon_in", files)
    head = pd.read_csv(main, nrows=5)
    title_c = _find_col(head, ["title", "name", "product_name"])
    price_c = _price_col("amazon_in", head)
    img_c = _find_col(head, ["imgUrl", "image", "img_link", "image_url"])
    cat_id_c = _find_col(head, ["category_id"])
    cat_name_c = _find_col(head, ["category_name", "category", "sub_category", "main_category"])
    id_c = _find_col(head, ["asin", "product_id", "id"])
    st = {"available": True, "main_file": main.name,
          "columns_used": {"title": title_c, "price": price_c, "image": img_c,
                           "category_id": cat_id_c, "category_name": cat_name_c, "id": id_c}}
    if not (title_c and price_c and (cat_id_c or cat_name_c)):
        st["skipped_reason"] = f"schema differs materially: columns={list(head.columns)}"
        log(f"amazon_in: {st['skipped_reason']} — continuing without it")
        return None, st

    cat_names = None
    if cat_id_c and not cat_name_c:
        cat_file = next((f for f in files if f != main and
                         {"id", "category_name"} <= set(pd.read_csv(f, nrows=2).columns)), None)
        if cat_file is None:
            st["skipped_reason"] = "category_id present but no category lookup file"
            return None, st
        cats = pd.read_csv(cat_file)
        cat_names = dict(zip(cats["id"], cats["category_name"].astype(str)))

    usecols = [c for c in {title_c, price_c, img_c, cat_id_c, cat_name_c, id_c} if c]
    parts, raw_rows = [], 0
    for chunk in pd.read_csv(main, usecols=usecols, chunksize=200_000, low_memory=False):
        raw_rows += len(chunk)
        name = chunk[cat_name_c].astype(str) if cat_name_c else chunk[cat_id_c].map(cat_names)
        canon = name.map(_amazon_category)
        keep = canon.notna()
        if keep.any():
            c = chunk[keep]
            parts.append(pd.DataFrame({
                "pid": "amz_" + (c[id_c].astype(str) if id_c else c.index.astype(str)),
                "source": "amazon_in",
                "title": c[title_c].astype(str).str.strip(),
                "description": "",
                "category": canon[keep].values,
                "subcategory": name[keep].values,
                "specs": [{}] * int(keep.sum()),
                "brand": "",
                "price": parse_price(c[price_c]).values,
                "image_url": c[img_c].values if img_c else None,
            }))
    st["raw_rows"] = raw_rows
    if not parts:
        st["skipped_reason"] = "no rows in craft-relevant categories"
        return None, st
    df = pd.concat(parts, ignore_index=True)
    st["craft_relevant_rows"] = int(len(df))
    df = df[(df["price"] >= MIN_PRICE) & (df["price"] <= MAX_PRICE)]
    df = df.drop_duplicates("pid")
    # balanced, seeded sample so no single category dominates
    df = df.sample(frac=1.0, random_state=SEED)
    df = df[df.groupby("category").cumcount() < AMAZON_MAX_PER_CATEGORY]
    if len(df) > max_rows:
        df = df.sample(max_rows, random_state=SEED)
    st["sampled_rows"] = int(len(df))
    return df.reset_index(drop=True), st


def _amazon_category(name: str) -> str | None:
    if not isinstance(name, str):
        return None
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()   # "Décor" → "Decor"
    for canon, rx in _AMZ_RX:
        if rx.search(name):
            return canon
    return None


def assign_splits(df: pd.DataFrame) -> pd.Series:
    """Grouped (near-duplicate names never cross splits) + stratified by category: 70/15/15."""
    sgkf = StratifiedGroupKFold(n_splits=SPLIT_FOLDS, shuffle=True, random_state=SEED)
    fold = np.empty(len(df), dtype=int)
    for k, (_, idx) in enumerate(sgkf.split(df, df["category"], groups=df["group"])):
        fold[idx] = k
    return pd.Series(np.select([fold < TEST_FOLDS, fold < TEST_FOLDS + VAL_FOLDS],
                               ["test", "val"], "train"), index=df.index)


def preprocess(skip_amazon: bool, amazon_max_rows: int) -> None:
    q = load_quality()
    pp: dict = {}
    fk, pp["flipkart"] = load_flipkart()
    frames = [fk]
    if not skip_amazon:
        amz, pp["amazon_in"] = load_amazon(amazon_max_rows)
        if amz is not None:
            frames.append(amz)
    df = pd.concat(frames, ignore_index=True)
    pp["combined_rows_before_cleaning"] = int(len(df))

    # target ----------------------------------------------------------------
    n = len(df)
    df = df[df["price"].notna() & (df["price"] > 0)]
    pp["dropped_missing_or_nonpositive_price"] = n - len(df)
    n = len(df)
    df = df[(df["price"] >= MIN_PRICE) & (df["price"] <= MAX_PRICE)]
    pp["dropped_outside_price_window"] = {"n": n - len(df), "window_inr": [MIN_PRICE, MAX_PRICE]}

    # domain filters --------------------------------------------------------------
    n = len(df)
    df = df[~df["title"].str.contains(NON_ARTISAN_RX)]
    pp["dropped_appliances_electronics"] = n - len(df)
    if EXCLUDE_FINE_JEWELLERY:
        n = len(df)
        df = df[~((df["category"] == "jewellery") & df["title"].str.contains(FINE_JEWELLERY_RX))]
        pp["dropped_fine_jewellery"] = n - len(df)

    # text --------------------------------------------------------------------
    df = df[df["title"].str.len() > 2]
    pp["rows_before_scrub"] = int(len(df))
    pp["description_rows_with_price_mentions_before_scrub"] = int(
        df["description"].str.contains(r"(?:rs\.?|₹|inr)\s*\d", case=False, regex=True).sum())
    df["material"] = [material_from_row(s, t, d) for s, t, d in
                      zip(df["specs"], df["title"], df["description"])]
    df["description"] = df["description"].map(scrub_text)
    df["subcategory"] = df["subcategory"].fillna(UNKNOWN).astype(str)
    df["group"] = df["title"].map(group_key)
    df.loc[df["group"] == "", "group"] = df["pid"]

    # duplicates ----------------------------------------------------------------
    n = len(df)
    df = df.drop_duplicates(subset=["source", "title", "price", "description"])
    df = df.drop_duplicates(subset=["pid"])
    pp["dropped_exact_duplicates"] = n - len(df)

    assert set(df["category"]) <= set(CATEGORIES), set(df["category"]) - set(CATEGORIES)
    df = df.drop(columns=["specs"]).reset_index(drop=True)
    df["split"] = assign_splits(df)

    # leakage checks ------------------------------------------------------------
    groups_per_split = df.groupby("group")["split"].nunique()
    assert (groups_per_split == 1).all(), "a name group crosses splits"
    remaining = int(df["description"].str.contains(r"(?:rs\.?|₹|inr)\s*\d", case=False, regex=True).sum())
    pp["description_rows_with_price_mentions_after_scrub"] = remaining

    pp.update({
        "final_rows": int(len(df)),
        "rows_by_source": df["source"].value_counts().to_dict(),
        "rows_by_category": df["category"].value_counts().to_dict(),
        "rows_by_split": df["split"].value_counts().to_dict(),
        "material_distribution": df["material"].value_counts().to_dict(),
        "price_summary_inr": df["price"].describe().round(2).to_dict(),
        "price_median_by_category": df.groupby("category")["price"].median().round(1).to_dict(),
        "missing_image_url": int(df["image_url"].isna().sum()),
        "empty_description": int((df["description"] == "").sum()),
        "distinct_name_groups": int(df["group"].nunique()),
        "target": "observed selling price (Flipkart discounted_price / Amazon price), INR",
    })
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df.to_pickle(PROCESSED_DIR / "products.pkl")
    df.drop(columns=["description"]).to_csv(PROCESSED_DIR / "products_preview.csv", index=False)
    q["preprocess"] = pp
    save_quality(q)
    write_data_sources(q)
    log(f"final dataset: {len(df)} rows  by split {pp['rows_by_split']}  by source {pp['rows_by_source']}")
    log(f"price-in-description rows: {pp['description_rows_with_price_mentions_before_scrub']} → {remaining} after scrub")
    log("→ data/processed/products.pkl, data/reports/data_quality.json, data/DATA_SOURCES.md")


def write_data_sources(q: dict) -> None:
    pp = q.get("preprocess", {})
    lines = ["# Data sources (generated by `data.py` from measured values)", "",
             f"Generated: {time.strftime('%Y-%m-%d %H:%M')}", ""]
    for name, spec in DATASETS.items():
        v = q.get("verify", {}).get(name, {})
        d = q.get("download", {}).get(name, {})
        lines += [f"## {name}", "",
                  f"- Source: {spec['url']}",
                  f"- License: {spec['license']}",
                  f"- Role: {'primary (required)' if spec['required'] else 'optional enrichment'}",
                  f"- Obtained via: {d.get('method', 'n/a')} {d.get('date', '')}".rstrip()]
        if not v.get("available"):
            lines += ["- Status: **not available in this run** — pipeline ran without it", ""]
            continue
        lines += [f"- Main file: `{v['main_file']}` — {v['rows']:,} non-empty rows, {len(v['columns'])} columns",
                  f"- Target column: `{spec['target']}` (observed selling price, INR)",
                  f"- Expected columns missing: {v['expected_columns_missing'] or 'none'}",
                  f"- Price format: `{json.dumps(v.get('price_format'))}`",
                  f"- Image URL probe: `{json.dumps(v.get('image_url_probe'))}`"]
        for f in v["files"]:
            lines.append(f"- `{f['name']}`: {f['bytes']:,} bytes, sha256 `{f['sha256'][:16]}…`")
        src = pp.get(name, {})
        if src:
            lines.append(f"- Preprocessing stats: `{json.dumps({k: v2 for k, v2 in src.items() if k != 'top_level_counts'}, default=str)}`")
        lines.append("")
    lines += ["## Final training table", "",
              f"- Rows: {pp.get('final_rows')}",
              f"- By source: {pp.get('rows_by_source')}",
              f"- By category: {pp.get('rows_by_category')}",
              f"- By split: {pp.get('rows_by_split')}",
              f"- Price window kept: ₹{MIN_PRICE:.0f}–₹{MAX_PRICE:,.0f} (artisan market segment)",
              f"- Rows whose description mentioned the price: "
              f"{pp.get('description_rows_with_price_mentions_before_scrub')} before scrubbing → "
              f"{pp.get('description_rows_with_price_mentions_after_scrub')} after",
              "", "## Known limitations", "",
              "- Flipkart snapshot is from 2016 — prices are not inflation-adjusted; many 2016 image URLs may no longer resolve (see probe).",
              "- Marketplace listings are mostly factory-made goods; handmade premium is modelled by the business layer, not learned.",
              "- Amazon rows have titles only (no description), so their text embedding carries less information.",
              "- Category mapping into the craft taxonomy is rule-based (see FLIPKART_CATEGORY_MAP / AMAZON_CATEGORY_RULES)."]
    (ROOT / "data" / "DATA_SOURCES.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stages", nargs="*", metavar="stage",
                    help="download | verify | preprocess | all (default: all)")
    ap.add_argument("--skip-amazon", action="store_true")
    ap.add_argument("--amazon-max-rows", type=int, default=AMAZON_MAX_ROWS)
    ap.add_argument("--no-probe", action="store_true", help="skip the 100-URL image probe")
    args = ap.parse_args()
    stages = args.stages or ["all"]
    bad = set(stages) - {"download", "verify", "preprocess", "all"}
    if bad:
        ap.error(f"unknown stage(s): {sorted(bad)}")
    if "all" in stages:
        stages = ["download", "verify", "preprocess"]
    if "download" in stages:
        download(args.skip_amazon)
    if "verify" in stages:
        verify(probe_images=not args.no_probe)
    if "preprocess" in stages:
        preprocess(args.skip_amazon, args.amazon_max_rows)


if __name__ == "__main__":
    main()
