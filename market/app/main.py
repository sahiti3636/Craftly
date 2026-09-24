"""The buyer-side service: storefront, B2B portal, QR passport, reels.

Slice C1. Runs on its own with `CRAFTLY_ADAPTERS=stub`; points at the real
B1 and B2 with `CRAFTLY_ADAPTERS=http`. See `app/ports.py` for the seams
and the "C1 — Buyer surfaces" section of the repo's README.md for what
each surface is for.

    uv run uvicorn app.main:app --reload --port 8100
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import config, deps
from app.adapters.http_platform import PlatformUnavailable
from app.routes import api, b2b, buyer, passport
from app.templating import page

app = FastAPI(
    title="craftly — market",
    description="Buyer surfaces: storefront, B2B portal, craft passport, reels.",
    version="0.1.0",
)

config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(config.PACKAGE_DIR / "static")), name="static")
app.mount("/media", StaticFiles(directory=str(config.MEDIA_DIR)), name="media")

app.include_router(buyer.router)
app.include_router(b2b.router)
app.include_router(passport.router)
app.include_router(api.router)


@app.on_event("startup")
def prewarm_reels() -> None:
    """Build the first few reels in the background so the app's first tap
    on play is instant (a build is a few seconds; every later request reuses it)."""
    import threading

    def work() -> None:
        try:
            from app.adapters import registry
            from app.contracts import Channel
            from app.reel import build as build_reel

            for card in [c for c in deps.cards(Channel.OWN_STORE) if c.image_url]:
                build_reel(card, lang="en", passport=registry.passports().by_listing(card.listing_id))
        except Exception:  # never let a warm-up problem affect serving
            pass

    threading.Thread(target=work, daemon=True).start()


@app.exception_handler(PlatformUnavailable)
def platform_down(request: Request, exc: PlatformUnavailable):
    """B1 or B2 is unreachable.

    An empty shop is indistinguishable from a shop with nothing in it,
    and the second one is a lie. Say the platform is down.
    """
    return page(request, "unavailable.html", detail=str(exc), status_code=503)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "adapters": config.ADAPTERS}


@app.get("/", response_class=HTMLResponse)
@app.get("/app", response_class=HTMLResponse)
def buyer_app() -> str:
    """The buyer home: the phone-style app. Same catalogue, same /cart and
    /checkout, driven through the JSON API (see routes/api.py). The older
    server-rendered storefront is still at /classic."""
    return (config.PACKAGE_DIR / "static" / "buyer_app.html").read_text(encoding="utf-8")


@app.get("/lang/{code}")
def set_language(request: Request, code: str):
    """Switch display language and return to where the buyer was."""
    target = request.headers.get("referer") or "/"
    response = RedirectResponse(target, status_code=303)
    if code in config.LANGUAGES:
        response.set_cookie(
            deps.LANG_COOKIE, code, max_age=60 * 60 * 24 * 365, samesite="lax"
        )
    return response
