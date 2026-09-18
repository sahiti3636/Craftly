"""The QR landing page, the QR image itself, and reels.

`/p/{code}` is the only surface in this system that a buyer meets while
holding the object. Someone at a mela picks up a printed hang-tag,
scans it, and this page has about four seconds to answer "who made this
and is that claim real" on a phone with bad signal. That is why it is a
single page with no chrome, no basket, no navigation, and why the reorder
link is the last thing rather than the first — the page earns the sale by
answering the question it was opened to answer.

The reorder link is what turns a one-off mela sale into repeat business:
a shopkeeper who sold twenty of these can scan the tag on the last one
and order twenty more from the same woman, without knowing her name, her
number, or which co-operative she belongs to.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app import config, deps
from app.adapters import registry
from app.adapters.stub_passport import render_qr_png, verification_code
from app.contracts import Channel
from app.reel import build as build_reel
from app.templating import page

router = APIRouter()


@router.get("/p/{code}")
def passport_page(request: Request, code: str):
    passport = registry.passports().by_code(code)
    if passport is None:
        return page(request, "passport_missing.html", code=code, status_code=404)

    card = deps.card(passport.listing_id, Channel.OWN_STORE)
    return page(request, "passport.html", passport=passport, card=card)


@router.get("/qr/{code}.png")
def qr_image(code: str):
    """The QR image for a passport code.

    B2 mints these in the finished system. This endpoint exists so the QR
    journey can be demonstrated — and so a tag sheet can be printed —
    before B2 ships. It is also genuinely useful: an artisan who runs out
    of printed tags at a mela can pull one up on a phone.
    """
    path = render_qr_png(code.strip().upper())
    return FileResponse(path, media_type="image/png")


@router.get("/tag/{listing_id}")
def printable_tag(request: Request, listing_id: str):
    """A hang-tag sized for printing: QR, code, maker, one line of proof.

    Deliberately a web page rather than a PDF. An artisan or a cluster
    co-ordinator prints these from a phone in a browser at a cyber cafe;
    a PDF generator is one more dependency between her and a tag.
    """
    passport = registry.passports().by_listing(listing_id)
    if passport is None:
        return page(request, "not_found.html", what="passport", status_code=404)
    card = deps.card(listing_id, Channel.MELA_QR)
    return page(request, "tag.html", passport=passport, card=card)


@router.get("/reel/{listing_id}")
def reel_page(request: Request, listing_id: str):
    """Build (or reuse) the reel and show it with its caption script.

    Rendering happens on the request rather than in a queue because at
    this scale it takes a couple of seconds and a queue is one more thing
    to run on a laptop. If reels move to bulk generation, this is the
    handler that becomes a job submission.
    """
    card = deps.card(listing_id, Channel.OWN_STORE)
    if card is None:
        return page(request, "not_found.html", what="piece", status_code=404)

    lang = deps.language(request)
    passport = registry.passports().by_listing(listing_id)
    result = build_reel(
        card,
        lang=lang,
        passport=passport,
        force=request.query_params.get("rebuild") == "1",
    )
    return page(request, "reel.html", card=card, reel=result, passport=passport)


@router.get("/reels/{filename}")
def reel_file(filename: str):
    """Serve a built reel or storyboard frame.

    Not a StaticFiles mount because the directory is created at build time
    and may not exist at startup, and because the name is constrained here
    to files this module actually produces.
    """
    path = (config.REELS_DIR / filename).resolve()
    root = config.REELS_DIR.resolve()
    # `filename` comes off the URL, so a name containing `..` must not be
    # able to walk out of the reels directory and serve, say, a .env.
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="No such reel")
    media_type = "video/mp4" if path.suffix == ".mp4" else "image/png"
    return FileResponse(path, media_type=media_type)


@router.get("/scan")
def scan_help(request: Request):
    """A list of every passport code in the catalogue, with its QR.

    This is a demo affordance, not a product surface: judges and testers
    need a way to scan a real code off a screen without a printed tag in
    their hand. It is listed in the README as such.
    """
    cards = deps.cards(Channel.MELA_QR)
    rows = [(card, verification_code(card.listing_id)) for card in cards]
    return page(request, "scan.html", rows=rows)
