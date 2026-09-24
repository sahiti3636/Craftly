"""Environment-driven settings for the platform.

Read once at import, and every value has a working default so that

    uv run uvicorn app.main:app

comes up on a fresh clone with no ``.env`` at all. A service that needs
configuration before it will answer `/health` is a service that fails on
a teammate's laptop twenty minutes before a demo.

The one setting with a deliberately *insecure* default is
``REQUIRE_ORDER_AUTH``. See the note on it below: it is false so that C1
works the day this ships, and `app/main.py` says so out loud at startup.
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
REPO_DIR = PROJECT_DIR.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


#: SQLAlchemy URL. SQLite by default because the whole point of this slice
#: is that the other five can clone the repo and have a database without
#: installing one. `postgresql+psycopg://...` works unchanged.
DATABASE_URL = os.environ.get(
    "CRAFTLY_DATABASE_URL", f"sqlite:///{(PROJECT_DIR / 'craftly.db').as_posix()}"
)

#: Where uploaded media lands. Content-addressed — see app/media.py.
MEDIA_DIR = Path(os.environ.get("CRAFTLY_MEDIA_DIR", PROJECT_DIR / "media"))
QR_DIR = Path(os.environ.get("CRAFTLY_QR_DIR", PROJECT_DIR / "qr"))

MAX_UPLOAD_BYTES = int(os.environ.get("CRAFTLY_MAX_UPLOAD_BYTES", 12 * 1024 * 1024))

#: Baked into QR codes, so it must be reachable from a phone at a mela.
#: A QR pointing at localhost scans to nothing on anyone else's device.
#: It points at C1's storefront, not at this service: the passport page a
#: buyer lands on is C1's `/p/{code}`, and this service only mints the code.
PUBLIC_URL = os.environ.get("CRAFTLY_PUBLIC_URL", "http://localhost:8100").rstrip("/")

#: Where this service itself is reachable, used to build absolute media URLs
#: when a client asks for them.
SELF_URL = os.environ.get("CRAFTLY_PLATFORM_SELF_URL", "http://localhost:8200").rstrip("/")

#: Signing secret for access tokens. Generated per-process when unset, which
#: means tokens do not survive a restart in dev — correct for dev, and
#: `app/main.py` warns when it happens with more than one worker in play.
SECRET_KEY = os.environ.get("CRAFTLY_SECRET_KEY", "")

TOKEN_TTL_SECONDS = int(os.environ.get("CRAFTLY_TOKEN_TTL_SECONDS", 30 * 24 * 3600))
OTP_TTL_SECONDS = int(os.environ.get("CRAFTLY_OTP_TTL_SECONDS", 10 * 60))
OTP_MAX_ATTEMPTS = int(os.environ.get("CRAFTLY_OTP_MAX_ATTEMPTS", 5))

#: In dev the OTP is returned in the response body, because there is no SMS
#: gateway wired up and an artisan login you cannot complete is not a login.
#: C2 owns the real delivery channel. Turn this off before this is public.
OTP_ECHO = _flag("CRAFTLY_OTP_ECHO", True)

#: C1's HttpOrderSink posts an order with no Authorization header, because
#: when it was written B2 did not exist and there was nobody to get a token
#: from. Leaving this false keeps that integration working today; turning it
#: true is a one-line change here and a header in C1's adapter.
REQUIRE_ORDER_AUTH = _flag("CRAFTLY_REQUIRE_ORDER_AUTH", False)

#: Percentage of the margin *above the wage floor* the marketplace keeps.
#: It is applied to the margin, never to the floor — see app/payments.py.
PLATFORM_FEE_PCT = float(os.environ.get("CRAFTLY_PLATFORM_FEE_PCT", 0.10))

#: Seed directory used by `python -m app.seed`. Points at C1's committed
#: catalogue so that flipping CRAFTLY_ADAPTERS=http gives the same shop.
SEED_DIR = Path(os.environ.get("CRAFTLY_SEED_DIR", REPO_DIR / "market" / "seed"))

#: Shared with C2 (integrations). A request bearing it acts as the
#: `service` account: it can list every order, move an order along as the
#: courier picks it up and delivers it, and read an artisan's contact
#: details and call consent. Unset, nothing can — C2 then works without B2.
SERVICE_TOKEN = os.environ.get("CRAFTLY_SERVICE_TOKEN", "")
