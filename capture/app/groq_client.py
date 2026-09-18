"""Shared Groq client setup, used by app/extract.py and app/describe.py.

Kept in one place so there's a single source of truth for the API key
check and default model — two LLM call sites duplicating that logic is
exactly the kind of drift that bites the next developer who changes one
and forgets the other.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

DEFAULT_MODEL = os.environ.get("CRAFTLY_GROQ_MODEL", "openai/gpt-oss-120b")

_client: Groq | None = None


def get_client() -> Groq:
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Add it to .env or the environment."
            )
        _client = Groq(api_key=api_key)
    return _client
