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

# 127.0.0.1, not localhost: on Windows "localhost" tries IPv6 first and every
# service-to-service call waits ~2s for it to fail.
PLATFORM_URL = os.environ.get("CRAFTLY_PLATFORM_URL", "http://127.0.0.1:8200").rstrip("/")
ENGINES_URL = os.environ.get("CRAFTLY_ENGINES_URL", "http://127.0.0.1:8010").rstrip("/")

SEED_DIR = Path(os.environ.get("CRAFTLY_SEED_DIR", PROJECT_DIR / "seed"))
MEDIA_DIR = Path(os.environ.get("CRAFTLY_MEDIA_DIR", SEED_DIR / "media"))
#: Copies of photos fetched from B2's media store (see app/media.py).
MEDIA_CACHE_DIR = Path(os.environ.get("CRAFTLY_MEDIA_CACHE_DIR", PROJECT_DIR / "media_cache"))
ORDERS_PATH = Path(os.environ.get("CRAFTLY_ORDERS_PATH", PROJECT_DIR / "orders.jsonl"))
REELS_DIR = Path(os.environ.get("CRAFTLY_REELS_DIR", PROJECT_DIR / "reels"))
QR_DIR = Path(os.environ.get("CRAFTLY_QR_DIR", PROJECT_DIR / "qr"))

FFMPEG = os.environ.get("CRAFTLY_FFMPEG", "ffmpeg")

#: Reel voiceover: "gtts" (Google's TTS, needs internet while a reel is
#: built) or "off" for silent reels.
REEL_VOICE = os.environ.get("CRAFTLY_REEL_VOICE", "gtts").strip().lower()

#: Baked into QR codes. Must be reachable from a phone on the same
#: network — a QR pointing at localhost scans to nothing.
PUBLIC_URL = os.environ.get("CRAFTLY_PUBLIC_URL", "http://localhost:8100").rstrip("/")

#: Languages the buyer surfaces render in. The artisan side speaks many
#: more; a buyer page only has text A2 actually generates, which is
#: English and Hindi.
LANGUAGES = ("en", "hi")
DEFAULT_LANGUAGE = "en"


#: Prices and bulk splits can be switched separately, so the shop can run
#: off B2's real catalogue while B1 is not up. Defaults to ADAPTERS.
PRICE_ADAPTERS = os.environ.get("CRAFTLY_PRICE_ADAPTERS", ADAPTERS).strip().lower()


def using_stubs() -> bool:
    return ADAPTERS != "http"


def using_stub_prices() -> bool:
    return PRICE_ADAPTERS != "http"
