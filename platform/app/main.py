"""The platform service: FastAPI app, startup, health.

Run it:

    uv run uvicorn app.main:app --reload --port 8200

Everything has a default, so that command works on a fresh clone with no
`.env`, no database server and no credentials. The database file is
created on startup and `python -m app.seed` fills it from C1's committed
catalogue.

Startup is deliberately noisy about insecure defaults. There are two — an
unauthenticated order endpoint and an OTP echoed in the response — and
both exist so the other slices work today. A default that is wrong for
production is fine; a default that is wrong for production and *silent* is
how it ships.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app import config
from app.db import SessionLocal, create_all
from app.routes import auth as auth_routes
from app.routes import catalog as catalog_routes
from app.routes import listings as listing_routes
from app.routes import media as media_routes
from app.routes import orders as order_routes
from app.routes import passports as passport_routes
from app.schemas import HealthOut

log = logging.getLogger("craftly.platform")


def startup_warnings() -> list[str]:
    """Insecure-by-design settings, named. Shown at startup and on /health."""
    warnings = []
    if not config.REQUIRE_ORDER_AUTH:
        warnings.append(
            "Orders can be placed without a token (CRAFTLY_REQUIRE_ORDER_AUTH=false). "
            "C1's adapter sends no Authorization header yet; turn this on once it does."
        )
    if config.OTP_ECHO:
        warnings.append(
            "Login codes are returned in the response body (CRAFTLY_OTP_ECHO=true). "
            "There is no SMS gateway in this repo — C2 owns delivery. Turn this off "
            "before this service is reachable from the internet."
        )
    if not config.SECRET_KEY:
        warnings.append(
            "CRAFTLY_SECRET_KEY is unset. Tokens are still random and safe, but "
            "set it before running more than one worker so the whole deployment "
            "shares one configuration."
        )
    if config.PUBLIC_URL.startswith("http://localhost"):
        warnings.append(
            f"QR codes are being minted against {config.PUBLIC_URL}, which will not "
            "resolve on a phone. Set CRAFTLY_PUBLIC_URL to an address a device on "
            "the same network can reach before printing any hang-tags."
        )
    return warnings


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_all()
    for warning in startup_warnings():
        log.warning("%s", warning)
    yield


app = FastAPI(
    title="Craftly platform",
    version="0.1.0",
    summary="Database, auth, APIs, media, craft passports and payment splits (slice B2).",
    lifespan=lifespan,
)

app.include_router(catalog_routes.router)
app.include_router(passport_routes.router)
app.include_router(order_routes.router)
app.include_router(listing_routes.router)
app.include_router(media_routes.router)
app.include_router(auth_routes.router)


@app.exception_handler(SQLAlchemyError)
async def _database_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
    """Never leak a query into a response.

    A SQL error rendered to a buyer surface is both a confusing page and a
    free schema dump. The caller gets a sentence; the detail goes to the log.
    """
    log.exception("database error", exc_info=exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "The catalogue is temporarily unavailable."},
    )


@app.get("/health", response_model=HealthOut, tags=["ops"])
def health() -> HealthOut:
    """Liveness plus a count of what is actually in the database.

    The counts are the useful part. A service that answers `{"status":
    "ok"}` while holding an empty database has told you nothing, and an
    empty catalogue is exactly what a missed seed step looks like.
    """
    from app.models import Artisan, Listing, Order

    try:
        with SessionLocal() as db:
            listings = db.scalar(select(func.count()).select_from(Listing)) or 0
            published = (
                db.scalar(
                    select(func.count()).select_from(Listing).where(Listing.published.is_(True))
                )
                or 0
            )
            artisans = db.scalar(select(func.count()).select_from(Artisan)) or 0
            order_count = db.scalar(select(func.count()).select_from(Order)) or 0
    except SQLAlchemyError:
        log.exception("health check could not reach the database")
        return HealthOut(status="degraded", database="unreachable", warnings=startup_warnings())

    return HealthOut(
        status="ok",
        database="ok",
        listings=listings,
        published=published,
        artisans=artisans,
        orders=order_count,
        warnings=startup_warnings(),
    )
