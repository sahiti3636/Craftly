"""Environment-driven settings for the buyer surfaces.

Read once at import. Everything has a working default so `uvicorn
app.main:app` runs with no `.env` at all — a demo that needs
configuration before it shows anything is a demo that fails on someone
else's laptop.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # optional: .env support is a convenience, not a requirement
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is in the dependency list
    pass

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

#: "stub" runs entirely on seed data; "http" calls the real B1/B2 services.
ADAPTERS = os.environ.get("CRAFTLY_ADAPTERS", "stub").strip().lower()

PLATFORM_URL = os.environ.get("CRAFTLY_PLATFORM_URL", "http://localhost:8000").rstrip("/")
ENGINES_URL = os.environ.get("CRAFTLY_ENGINES_URL", "http://localhost:8010").rstrip("/")

SEED_DIR = Path(os.environ.get("CRAFTLY_SEED_DIR", PROJECT_DIR / "seed"))
MEDIA_DIR = Path(os.environ.get("CRAFTLY_MEDIA_DIR", SEED_DIR / "media"))
ORDERS_PATH = Path(os.environ.get("CRAFTLY_ORDERS_PATH", PROJECT_DIR / "orders.jsonl"))
REELS_DIR = Path(os.environ.get("CRAFTLY_REELS_DIR", PROJECT_DIR / "reels"))
QR_DIR = Path(os.environ.get("CRAFTLY_QR_DIR", PROJECT_DIR / "qr"))

FFMPEG = os.environ.get("CRAFTLY_FFMPEG", "ffmpeg")

#: Baked into QR codes. Must be reachable from a phone on the same
#: network — a QR pointing at localhost scans to nothing.
PUBLIC_URL = os.environ.get("CRAFTLY_PUBLIC_URL", "http://localhost:8100").rstrip("/")

#: Languages the buyer surfaces render in. The artisan side speaks many
#: more; a buyer page only has text A2 actually generates, which is
#: English and Hindi.
LANGUAGES = ("en", "hi")
DEFAULT_LANGUAGE = "en"


def using_stubs() -> bool:
    return ADAPTERS != "http"
