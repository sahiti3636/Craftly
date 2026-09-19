"""Append-only JSONL logs for the simulated shipments and calls.

A read-only or missing disk must not stop the demo, same rule as C1's
order sink: the in-memory record is already made, so a failed write is
swallowed.
"""

from __future__ import annotations

import json
from typing import Any

from c2 import config


def append_jsonl(name: str, record: dict[str, Any]) -> None:
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (config.LOG_DIR / name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
