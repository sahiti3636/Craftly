"""Environment-driven settings. Every value has a working default so the
demo runs with no `.env`, same rule as C1."""

from __future__ import annotations

import os
from pathlib import Path

try:  # optional convenience
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
REPO_DIR = PROJECT_DIR.parent
MARKET_DIR = REPO_DIR / "market"

# 127.0.0.1, not localhost: on Windows "localhost" tries IPv6 first and every
# service-to-service call waits ~2s for it to fail.
C1_URL = os.environ.get("CRAFTLY_C1_URL", "http://127.0.0.1:8100").rstrip("/")

#: B2 (the platform). With SERVICE_TOKEN set to the same value as B2's,
#: orders come from B2 and delivery settles payouts there; see b2_client.py.
PLATFORM_URL = os.environ.get("CRAFTLY_PLATFORM_URL", "http://127.0.0.1:8200").rstrip("/")
SERVICE_TOKEN = os.environ.get("CRAFTLY_SERVICE_TOKEN", "")
SEED_DIR = Path(os.environ.get("CRAFTLY_SEED_DIR", MARKET_DIR / "seed"))
ORDERS_PATH = Path(os.environ.get("CRAFTLY_ORDERS_PATH", MARKET_DIR / "orders.jsonl"))
LOG_DIR = Path(os.environ.get("CRAFTLY_C2_LOG_DIR", PROJECT_DIR / "logs"))

#: The date the simulation treats as today. Pin it for a repeatable demo.
TODAY_OVERRIDE = os.environ.get("CRAFTLY_C2_TODAY")

#: Languages C2 has call scripts for. Artisans speak more; see voice.py.
CALL_LANGUAGES = ("hi", "en")
