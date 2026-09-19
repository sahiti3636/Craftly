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

C1_URL = os.environ.get("CRAFTLY_C1_URL", "http://localhost:8100").rstrip("/")
SEED_DIR = Path(os.environ.get("CRAFTLY_SEED_DIR", MARKET_DIR / "seed"))
ORDERS_PATH = Path(os.environ.get("CRAFTLY_ORDERS_PATH", MARKET_DIR / "orders.jsonl"))
LOG_DIR = Path(os.environ.get("CRAFTLY_C2_LOG_DIR", PROJECT_DIR / "logs"))

#: The date the simulation treats as today. Pin it for a repeatable demo.
TODAY_OVERRIDE = os.environ.get("CRAFTLY_C2_TODAY")

#: Languages C2 has call scripts for. Artisans speak more; see voice.py.
CALL_LANGUAGES = ("hi", "en")
