# craftly — market (slice C1)

Buyer surfaces: the storefront, the B2B portal, the craft-passport landing
page a QR code resolves to, and the fifteen-second reel generator. Plus a
JSON API over all of it for A1's app and C2's marketplace adapters.

Everything here runs with no other slice of Craftly up.

## Stack

- Python 3.11+
- FastAPI + Pydantic v2
- Jinja2 templates, hand-written CSS, no build step and no JavaScript
  framework — see "Why server-rendered" below
- Pillow for reel frames, `qrcode` for passport QRs
- `ffmpeg` (**optional**) for stitching reels into video. Everything works
  without it; reels fall back to a storyboard

## Setup

```bash
cd market
uv sync            # or: python -m venv .venv && pip install -e .
```

That is all — `seed/media/` is committed, so the shop has pictures the
moment you clone it.

Those pictures are generated placeholders. The seed catalogue describes
real crafts and this repo has no photographs of them, so
`scripts/generate_seed_media.py` draws a swatch per listing in the colours
the artisan named. Re-run it after editing `seed/listings.json`; it is
deterministic, so unchanged listings produce unchanged files. Drop a real
photo in over any file of the same name and every surface picks it up.

Copy `.env.example` to `.env` if you want to change anything. You do not
have to — every setting has a working default.

## Run

```bash
uv run uvicorn app.main:app --reload --port 8100
```

Then:

| URL | What it is |
|---|---|
| <http://localhost:8100/> | Storefront — browse, filter, buy one piece |
| <http://localhost:8100/b2b> | B2B portal — bulk quantity, deadline, split across a cluster |
| <http://localhost:8100/scan> | Every passport QR in the catalogue, scannable off the screen |
| <http://localhost:8100/reel/lst_51ea90cb7d24> | Reel generator |
| <http://localhost:8100/api/products> | The same catalogue as JSON |
| <http://localhost:8100/health> | `{"status": "ok", "adapters": "stub"}` |

## Test

```bash
uv run pytest
```

110 tests, no network, no ffmpeg required.

---

## What C1 owns, and what it only borrows

C1 owns screens. It does not own a price, a database, a courier or a
passport. Everything a buyer surface displays is produced somewhere else:

```
              ┌──────────────── C1: market/ ────────────────┐
              │  storefront   B2B portal   /p/{code}   reels │
              └───┬──────────────┬─────────────┬────────────┘
                  │              │             │
      CatalogSource│   PriceEngine│  PassportSource
      OrderSink     │  OrderEngine │
                  │              │             │
              ┌───▼──────────────▼─────────────▼───┐
              │      B2 platform    B1 engines      │
              └─────────────────────────────────────┘
```

Those five seams are `Protocol` classes in [`app/ports.py`](app/ports.py).
B1 and B2 did not exist when these screens were built, so each seam has a
stub in [`app/adapters/`](app/adapters/) and one environment variable
chooses between them:

```bash
CRAFTLY_ADAPTERS=stub   # seed JSON + placeholder price engine (default)
CRAFTLY_ADAPTERS=http   # the real B1 and B2 services
```

Two things follow, and they are the reason the design is shaped this way:

1. **C1 is demoable alone.** No database, no other service, no API key.
2. **Integration is a config change, not a rewrite.** When B1 and B2 ship,
   only `app/adapters/` changes. No route, template, search or quote
   calculation knows which implementation is behind it.

[`app/adapters/http_platform.py`](app/adapters/http_platform.py) is the
real adapter, written against endpoints that do not exist yet. It is half
implementation and half request: it states in code exactly what C1 needs
from B1 and B2 and in what shape, so that conversation can be about a diff
instead of a whiteboard.

### The stubs are stubs

| Stub | Stands in for | What it fakes |
|---|---|---|
| `stub_catalog.py` | B2's database | 12 listings, 14 artisans, 6 clusters from `seed/*.json` |
| `stub_price.py` | B1's price engine | Wage floor from a placeholder state-wage table |
| `stub_engine.py` | B1's order engine | Proportional capacity pooling across a cluster |
| `stub_passport.py` | B2's passport + QR | Derives a passport from the catalogue join, mints QRs |
| `stub_orders.py` | B2's order store | In-memory, plus every order appended to `orders.jsonl` |

That JSONL file is the useful part during integration: it is exactly the
payload C1 will POST to B2, written down, so B2 can build against real
traffic before the two are wired together.

---

## The shared contract, mirrored

[`app/contracts.py`](app/contracts.py) contains a **copy** of A2's
`Listing` from `capture/app/schema.py`. It is duplicated rather than
imported because both packages are called `app` and neither is
installable, so `from app.schema import Listing` inside `market/` would
resolve to the wrong module.

[`tests/test_schema_parity.py`](tests/test_schema_parity.py) loads A2's
file directly and fails if the two drift.

> **If that test goes red, do not edit the mirror to make it pass.** A
> schema change is a team-wide announcement. A red parity test means the
> announcement happened somewhere C1 did not hear it.

The same file also holds C1's proposal for the types B1 and B2 should
return — `PriceQuote`, `SplitPlan`, `Passport`, `Inventory`, `Artisan` —
and the `Order` that C1 produces and hands on.

---

## The rule that shapes every surface

**A listing whose wage floor is incomplete cannot be sold.**

If `material_cost_inr` or `hours_worked` is null, the floor underneath the
price is a guess. Selling at a guessed floor is how an artisan ends up
working below minimum wage with a receipt proving everyone agreed. So:

- `PriceQuote.floor_incomplete` is set by the price engine,
- [`app/view.py`](app/view.py) turns that into a refusal with the specific
  question that is outstanding ("we are still confirming hours of work"),
- the product page hides the button, `POST /cart/add` refuses a
  hand-written request, `cart.build()` drops the line if it got in
  anyway, and the B2B quote returns an error instead of a number.

Four checks, because they happen at four different times and a listing can
lose its floor between them. `lst_77b0c4d8e913` in the seed data is
permanently in this state so the path is always on screen.

### The other honest failure

Some crafts sell on the open market for **less than their own wage
floor**. A hand-made terracotta diya is three hours of work plus fifty
rupees of clay; it fetches sixty to a hundred and fifty rupees online. The
buyer surfaces say so, in those words, and list the piece at its floor
rather than matching the market. `lst_9f8a2e1c4b3d` is that case.

---

## The surfaces

### Storefront — `/`

Search across craft, material, colour, artisan, village and state, with
facet counts, price range, sort, and paging.
[`app/search.py`](app/search.py) is pure functions over already-priced
cards, which is why the price filter works and why the interesting parts
are testable without a server.

Ranking is explainable rather than clever: field weights, prefix matching,
so "pott" finds pottery and someone who stops typing halfway still gets
results. When the catalogue outgrows this, the replacement is a real
search engine behind `CatalogSource`, not a smarter loop.

Facet counts are computed **before** the structured filters are applied,
so ticking "textiles" does not make every other category vanish and strand
the buyer with no way back.

### Product page — `/product/{listing_id}`

Shows the price, and then shows how the price was built: materials, hours
at the named state's minimum wage, wastage, floor, the comparable market
band, the passport premium, the channel's cut, and what the artisan
actually keeps. The take-home share is on the tile in the grid too — it is
the number that distinguishes this marketplace from every other one, and a
claim you only make after someone clicks is a claim you are hiding.

It also prints the same piece's price on Amazon and Flipkart beside its
price here, with the artisan's take-home held constant across all three.
"Buy direct" is a comparison the buyer can check, not a slogan.

### B2B portal — `/b2b`

Quantity, deadline, and an answer that is allowed to be no.

The portal's whole value is being honest where honesty costs something.
[`app/bulk.py`](app/bulk.py) returns a quote that can be infeasible, and
the page renders that refusal with the two follow-ups a real procurement
conversation produces:

> Not 900 by 14 October. They can commit **2 units** by that date.
> All 900 would need about **136 days**.

with a one-click link to re-quote at either number. A spinner that
eventually produces an order the cluster cannot fill is how an artisan
ends up eating a penalty clause.

**Volume discounts come out of the margin above the floor, never out of
the floor.** A bulk buyer can negotiate the marketplace's share; they
cannot negotiate the artisan's wage.

### Capacity pooling

`stub_engine.py` spreads an order **proportionally across the cluster**
rather than pouring it into the first artisan until she overflows. Greedy
filling passes a naive feasibility test and is wrong twice over: 500 diyas
given to one woman who makes 900 a month is seventeen days in which she
can make nothing else, and the order takes far longer than the same 500
spread across the village. The listing's own artisan gets a weighted
share (she put it up), nobody is added for fewer units than is worth
packing, and anyone whose lead time exceeds the deadline is left out
rather than given work they cannot finish.

What the stub does *not* do, and B1 must: balance earnings across a
cluster over time, know who is mid-order on something else, or know that
two artisans share one kiln and cannot both fire at once.

### QR passport — `/p/{code}`

The only surface in this system a buyer meets while holding the object.
Someone at a mela picks up a printed hang-tag, scans it, and the page has
about four seconds to answer "who made this, and is that claim real" on a
phone with one bar of signal.

So it does not extend the site chrome. No navigation, no basket, no
search. Maker, place, the making clip if there is one, the chain of how
the object came to exist, what the maker was paid, the verification code,
and only then the reorder link — the page earns the sale by answering the
question it was opened to answer.

The reorder link is what turns a one-off mela sale into repeat business: a
shopkeeper who sold twenty can scan the tag on the last one and order
twenty more from the same woman without knowing her name, her number, or
which co-operative she belongs to.

**The code is short, spoken and unambiguous** — `CR-R8CR-9SG8`. The
alphabet drops `0/O` and `1/I/L`, because the failure mode is not a
cryptographic attack, it is a buyer squinting at a smudged tag and reading
it down a phone line. A code that resolves to nothing gets a page that
says "we cannot verify this" and a box to retype it, not a generic 404 —
being checkable is the entire value of the passport.

`/tag/{listing_id}` renders printable hang-tags. `/scan` lists every code
in the catalogue with its QR, so a judge or tester can walk the mela
journey without a printed tag in hand; that page is a demo affordance, not
a product surface.

### Reels — `/reel/{listing_id}`

Fifteen seconds, 1080×1920, built from one photo and the listing.

A reel is an argument in four beats: what it is and where a human made it;
how it was made and how long that took; what it costs and how much reaches
her; where to get it and the code that proves the claim.
[`app/captions.py`](app/captions.py) builds that as data, separately from
the rendering, which is what makes the wording testable.

Every number comes from the listing and the price quote. **A fact that is
missing is dropped rather than invented** — a reel that says "16 hours of
work" when `hours_worked` is null is a lie with a soundtrack, and this is
a system whose entire pitch is that the provenance is real.

Pillow composes each beat into a full frame — the photo blurred into a
background, the photo itself clean on top, a gradient scrim, the caption
under it. ffmpeg then gives each still a slow push and splices the making
clip into beat two if one exists.

**The storyboard fallback is not a consolation prize, it is the
contract.** With no ffmpeg on the machine, `build()` returns the same
frames as PNGs and the page shows the reel as a storyboard, with a warning
saying why. Degrade, don't crash — the same rule A2's pipeline holds
itself to.

No music: an unlicensed track is a takedown waiting to happen on exactly
the marketplaces this project is trying to reach, and picking a licensed
bed is a distribution decision, not a default buried in a renderer.

Hindi captions need a Devanagari font (Nirmala UI on Windows, Noto Sans
Devanagari elsewhere). If there is none, the reel is captioned in English
and says so — tofu boxes are worse than English.

### JSON API — `/api/...`

| Endpoint | For |
|---|---|
| `GET /api/products` | Search and filter, all the same parameters as the storefront |
| `GET /api/products/{id}?channel=amazon` | **C2**: the priced, channel-aware listing to push to a marketplace |
| `GET /api/passports/{code}` | **A1**: resolve a scanned code |
| `GET /api/bulk/quote?listing_id=&quantity=&deadline=` | A bulk quote, including infeasibility |

Anyone who later replaces these server-rendered pages with a single-page
app can do it against this API without touching `search.py`, `bulk.py` or
`view.py`. That is the point of the pages being thin.

---

## Why server-rendered

No React, no build step, no `npm install` between a teammate and a running
storefront. Every page is Jinja plus one hand-written stylesheet.

- It has to open on a cheap phone on mela wifi.
- It has to still run the week after the demo, on someone else's laptop,
  without a node_modules directory that has since rotted.
- The QR landing page in particular must render on the first paint. A
  passport page that needs to boot a JavaScript bundle before it can say
  who made the object has missed the point of being scanned.

The JSON API exists so that this is a choice rather than a trap.

---

## Money, language and other details worth knowing

**Indian digit grouping.** `₹12,34,567`, not `₹1,234,567`. Python's
`{:,}` gets this wrong. A marketplace for Indian craft printing Indian
prices in American grouping looks, to the people it is for, like it was
built for someone else. See [`app/money.py`](app/money.py).

**A missing price is a dash, not a zero.** `rupees(None)` is `—`.
Zero is a claim; missing is a question.

**Bilingual with honest fallback.** Buyer pages render in English or
Hindi, because English and Hindi are the two languages A2 actually
generates. `title_hi` is null when A2 could not produce it, and
`card.title('hi')` falls back rather than showing a blank. The artisan
side speaks many more languages; a buyer page can only show text that
exists.

**The basket is a signed cookie holding listing ids and quantities — and
no prices.** Prices are re-fetched from B1 on every render, so a basket
left open for three days cannot check out at a stale price and nobody can
edit a cookie into a discount. Signed so a tampered cookie is discarded as
if the basket were empty, which is a safe way to fail.

---

## Project layout

```
market/
  app/
    main.py            FastAPI app, mounts, error handlers
    contracts.py       mirrored Listing + the cross-slice types
    ports.py           the five seams (Protocols)
    config.py          environment settings
    deps.py            pricing a catalogue, resolving a language
    view.py            view models — where "can this be sold" is decided
    search.py          browse, filter, sort (pure)
    bulk.py            B2B quote: volume, deadline, feasibility
    cart.py            signed-cookie basket
    money.py           Indian rupee formatting
    captions.py        the reel script, as data
    reel.py            frame composition + ffmpeg, with storyboard fallback
    templating.py      Jinja setup and shared page context
    adapters/
      registry.py      picks stub vs http from one env var
      stub_*.py        the fakes
      http_platform.py the real B1/B2 adapter (endpoints proposed, not built)
    routes/
      buyer.py  b2b.py  passport.py  api.py
    templates/  static/
  seed/                catalogue, artisans, inventory, market bands
  scripts/             one-off media generation
  tests/
```

## Known gaps

- **`http_platform.py` has never talked to a real server.** The endpoint
  shapes are C1's proposal; B1 and B2 have not agreed to them yet.
- **Prices are fetched one listing at a time.** Twelve tiles on a browse
  page is twelve calls to B1 over HTTP. The fix is a batch price endpoint
  in B1's interface, not a cache here — a stale price in a basket is a
  worse bug than a slow page.
- **No payment.** Checkout ends at a placed order. Payment and the split
  to each artisan on delivery are B2's.
- **No auth.** Anyone can place an order with any phone number. B2 owns
  buyer accounts; the B2B portal in particular needs a real one before it
  takes a rupee.
- **The state minimum-wage table in `stub_price.py` is placeholder data.**
  Real rates vary by district, skill classification and notification date.
  B1 needs the actual gazette numbers.
- **Reels are rendered on the request.** Fine at this scale, wrong for
  bulk generation; `routes/passport.py:reel_page` is the handler that
  becomes a job submission.
- **Seed media are generated placeholders**, not photographs of the
  crafts described.
