# Craftly

One photo, one voice note, a year-round market.

An AI pipeline that lets a marginalized artisan list, price, prove and sell
a handmade product without typing, reading English, or understanding
e-commerce. She photographs the product and describes it out loud in her
own language. Everything after that is automated: photo cleanup, catalogue
copy, a wage-floor-backed price, an authenticity passport, publication to
multiple sales channels, buyer matching, order splitting across a cluster,
courier booking, and a phone call in her language telling her she's been
paid.

## Pipeline

```
Artisan view: capture (photo + voice: what it is, material cost, hours)
        │
        ├──▶ Photo studio ──┐
        ├──▶ Voice cataloguer ──┤
        └──▶ Price engine ──┘
                            │
                            ▼
            Live listing with craft passport
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
    Own app          Marketplace sync      Reel + QR code
 (direct sales)   (Amazon/Flipkart/eBay)  (melas, WhatsApp)
        │                   │                   │
        └─────────┬─────────┴───────────────────┘
                  ▼
      Buyer view  /  B2B buyer view
                  │
                  ▼
            Order engine (matches + splits across artisans)
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
   AI call /  Artisan   Pickup ticket
   message    view      (courier or
              (accept,   marketplace)
              pack)
        └─────────┼─────────┘
                  ▼
          Delivered and paid
     (split to each artisan by share)

    ↻ Sales data returns as demand alerts and QR reorders
```

## Repo layout

| Directory | Owner | Status |
|---|---|---|
| `capture/` | Pair A | Working |
| `platform/` | Pair B (B2) | Working |
| `market/` | Pair C | C1 working, C2 TODO |
| `app/` (mobile) | Pair A | TODO — native app deferred to the final project; see A1 below for the working demo |
| `schema/` | Shared contract | Working |

## The shared contract

Everything in this repo is built around one object: the **Listing**,
defined in [`capture/app/schema.py`](capture/app/schema.py) with a filled
example in [`schema/listing.example.json`](capture/schema/listing.example.json).

**Core rule: every extracted field is nullable, and a missing value is
`null` — never a guessed default.** `material_cost_inr` and `hours_worked`
must never default to `0`, because those two numbers feed a minimum-wage
floor. `0` means the artisan said there was no cost. `null` means we don't
know and must ask her.

Any change to this schema is a team-wide announcement, not a commit.

---

## Pair A — Artisan side

*Everything the artisan touches, plus the AI that processes what she
captures.*

### A2 — Capture AI  ✅ built

FastAPI service in [`capture/`](capture/). Speech-to-text, deterministic
money/duration parsing, LLM semantic extraction, bilingual description
generation, spoken readback, and product photo cleanup — wired into one
`POST /listing/create` call.

Full documentation: [`capture/README.md`](capture/README.md)

```bash
cd capture && uv sync && uv run uvicorn app.main:app --reload
```

`GET /demo` is a one-screen manual test page.

**Known gaps:** `app/lang/te.py`, `kn.py`, `ta.py` are empty stubs — only
Hindi and English resolve numbers end to end. TTS is a dev-only gTTS
backend. The draft store is in-memory. Blur and ASR thresholds are tuned
against synthetic fixtures, not real phone media.

### A1 — Artisan mobile app  ✅ demo

> **Owner:** Tejas Kollipara
>
> Capture screen, two-button home, offline queue and sync, fulfilment
> view, language switching, onboarding and consent — delivered for now
> as a phone-shaped browser demo at
> [`capture/static/mobile_demo.html`](capture/static/mobile_demo.html)
> (served same-origin by A2's backend at `GET /mobile-demo`, so it calls
> the real `/listing/create` and `/listing/confirm` endpoints, no mocks).
> A native app is deferred to the final project.
>
> - [x] Camera + mic capture, calls `POST /listing/create`
> - [x] Confirmation loop UI (plays `summary_spoken`, one tap to confirm)
> - [x] Offline queue — capture works with no network, syncs later
> - [x] Fulfilment view: accept order, packing slip, dispatch (local mock
>       data — no order-engine backend to call yet, see B1/C2 below)
> - [x] Language selection at onboarding + AI-call consent toggle
>
> ```bash
> cd capture && uv sync && uv run uvicorn app.main:app --reload
> ```
> then open `http://localhost:8000/mobile-demo`.

---

## Pair B — Intelligence and backend

### B1 — Price engine and order engine  🔲 TODO

> **Owner:** _TBD_
>
> Wage floor from material cost + (hours × state minimum wage) + wastage.
> Market band from comparable listings. Passport premium. Per-channel
> price adjustment so artisan take-home is constant across channels.
> Capacity pooling that splits a bulk order across a cluster.
>
> - [ ] `price(listing) -> {floor, band, recommended, per_channel}`
> - [ ] Hard rule: nothing publishes below floor
> - [ ] `split(order, artisans) -> allocation`
> - [ ] Honest failure path when floor exceeds market band
>
> _Setup and run instructions go here._

### B2 — Platform  ✅ built

FastAPI + SQLAlchemy service in [`platform/`](platform/): the database
every other slice has been faking, artisan and buyer auth, media storage,
craft-passport assembly with QR minting, the payment split on delivery,
and a deployment.

Full documentation: [`platform/README.md`](platform/README.md)

```bash
cd platform
uv sync
uv run python -m app.seed                                # fill the database
uv run uvicorn app.main:app --reload --port 8200
uv run pytest                                            # 134 tests, no network
```

That is the whole setup — SQLite by default, so there is no database
server to install and no credentials to hand round.
`CRAFTLY_DATABASE_URL=postgresql+psycopg://...` switches it and nothing in
`app/` changes.

- [x] Persistent store — 15 tables, replaces `capture/app/store.py` and C1's seed JSON
- [x] Artisan auth (phone + one-time code) and buyer auth (email + password)
- [x] Every endpoint C1's `http_platform.py` was written against, in the shapes it already parses
- [x] Content-addressed media storage
- [x] Passport object + QR, with the codes C1 has been printing all along
- [x] Payment split on delivery, idempotent, with blocked payouts recorded rather than dropped
- [x] Dockerfile + compose, non-root, healthcheck

| URL | What it is |
|---|---|
| <http://localhost:8200/health> | Status, plus counts of what is actually in the database |
| <http://localhost:8200/docs> | Every endpoint, generated from the code |
| <http://localhost:8200/catalog/entries> | The catalogue C1 renders |
| <http://localhost:8200/passports/by-code/CR-R8CR-9SG8> | A passport, resolved from a tag |
| <http://localhost:8200/qr/CR-R8CR-9SG8.png> | The QR for that tag |

#### The integration is real, not proposed

C1's [`market/app/adapters/http_platform.py`](market/app/adapters/http_platform.py)
was written before this service existed — half implementation, half
request, stating in code exactly what C1 needs and in what shape. **Every
endpoint in that file is implemented, at those paths, in those shapes.**

So C1 runs off the real database today:

```bash
cd platform && uv run python -m app.seed && uv run uvicorn app.main:app --port 8200
cd market   && CRAFTLY_ADAPTERS=http CRAFTLY_PLATFORM_URL=http://localhost:8200 \
               uv run uvicorn app.main:app --port 8100
```

Storefront, product pages, B2B portal, `/scan` and every QR passport page
render off the database instead of off seed JSON — same twelve products,
same photos, and **the same verification codes**. `CR-R8CR-9SG8` is still
`CR-R8CR-9SG8`.

That is a constraint, not a coincidence: every hang-tag printed off C1's
stub is already in the world, so `platform/app/ids.py` derives codes with
the identical algorithm, and a test loads that algorithm out of C1's own
source file and compares the two. `platform/tests/test_seed_parity.py`
holds the rest of the line — every field of every seeded listing, artisan
and inventory row survives the round trip unchanged.

> **One gap, and it is B1's seam rather than B2's.** With
> `CRAFTLY_ADAPTERS=http`, C1 also routes *prices* to `CRAFTLY_ENGINES_URL`,
> where it expects `POST /price` and `POST /split`. B1's service exposes
> `POST /api/predict` and `POST /api/bulk-order` instead. Until those two
> are reconciled, a fully-`http` C1 returns 503 on any page needing a price
> — correctly, because there is no price. The fix is a few lines in
> `http_platform.py` or a pair of aliases in B1; it does not touch B2.

#### What B2 owns, and what it refuses to own

It stores what the other slices produce and does not second-guess any of
it. `POST /orders` never calls B1 to re-check a price: the buyer saw a
number and agreed to it, and a platform that recalculates at write time
can disagree with its own receipt.

The one place B2 does arithmetic is the payment split, and it is governed
by five rules written down in
[`platform/app/payments.py`](platform/app/payments.py):

1. **The artisan's take-home is not a residual.** It is B1's number,
   quoted on the page, stored on the line, paid out unchanged. The
   marketplace's cut is what is left *after* it.
2. **The fee comes out of the margin above the floor, and the margin can
   be zero.** The fee applies to `price − take_home`, never to `price`.
   On a craft whose market price sits under its own wage floor — the
   terracotta diya in the seed data — the fee is zero and the marketplace
   absorbs the loss, recorded as `platform_absorbed_inr` rather than
   quietly netted off.
3. **A payout that cannot be sent is `blocked`, never dropped.** No UPI id
   on file still writes the row, with the full amount and a reason. The
   money is owed either way, and a system that skips the row loses the debt.
4. **Settlement is idempotent.** Courier webhooks retry; nobody gets paid
   twice and the second attempt does not error.
5. **An incomplete line blocks the split rather than guessing it.** A null
   take-home means B1 never resolved a wage floor. Paying a
   plausible-looking guess is the one outcome worse than paying late.

A three-unit order for an Ajrakh runner at ₹2,400, take-home ₹1,800:

```json
{ "gross_inr": 7200, "artisan_total_inr": 5400,
  "platform_fee_inr": 180, "platform_absorbed_inr": 0,
  "artisan_share_pct": 75.0, "complete": true,
  "payouts": [{ "artisan_name": "Hansaben Vankar", "amount_inr": 5400,
                "status": "pending" }] }
```

₹180 is 10% of the ₹600 margin, not 10% of ₹7,200. The share reaching the
maker is computed from the payout rows, not asserted on a banner.

#### Two insecure defaults, on purpose and out loud

Both are logged at startup and listed in `GET /health`.

| Setting | Default | Why | Fix |
|---|---|---|---|
| `CRAFTLY_REQUIRE_ORDER_AUTH` | `false` | C1's order adapter sends no `Authorization` header. Requiring one on day one breaks the only integration this endpoint has. | `true`, once C1 sends a token |
| `CRAFTLY_OTP_ECHO` | `true` | No SMS gateway exists in this repo — C2 owns delivery — and a login you cannot complete is not a login. | `false`, once C2's channel is wired in |

A default that is wrong for production is fine. One that is wrong for
production *and silent* is how it ships.

#### Known gaps

- **The container image has never been built.** No Docker on the machine
  this was written on, so `Dockerfile` and `docker-compose.yml` are
  reviewed but unverified; everything else here was run.
- **No real payment rail.** Payout rows are computed and recorded;
  `POST /payouts/{id}/paid` is a manual acknowledgement. The shape a UPI
  or bank-file integration plugs into is already there.
- **Deleting a media row does not delete the file.** Content addressing
  means two listings can share bytes; garbage collection is written down
  rather than half-implemented.
- **No rate limit on OTP requests.** Codes are attempt-capped and expire,
  but nothing caps requests. That belongs at a reverse proxy.
- **No pagination on `/catalog/entries`.** Honest at a few thousand items,
  wrong at a million — and the seam is placed so the query pushes down
  here without C1's screens noticing.
- **`create_all`, not Alembic.** One deployed instance, a re-seedable
  catalogue. A migration tool earns its place when neither is true.

---

## Pair C — Buyer side and the outside world

### C1 — Buyer surfaces  ✅ built

FastAPI + Jinja service in [`market/`](market/): the storefront, the B2B
portal, the craft-passport page a QR code resolves to, the fifteen-second
reel generator, and a JSON API over all of it for A1's app and C2's
adapters.

- [x] Browse / search / buy one piece — facets, price range, sort, basket, checkout
- [x] B2B bulk order with quantity and deadline — and a refusal with a number when the cluster cannot meet it
- [x] QR scan → passport page + reorder link, plus printable hang-tags
- [x] 15s vertical reel from photo + making-clip + captions, with a storyboard fallback when ffmpeg is absent

Python 3.11+, FastAPI + Pydantic v2, Jinja2 templates and hand-written
CSS with no build step, Pillow for reel frames, `qrcode` for passport QRs.
`ffmpeg` is **optional** — it stitches reels into video, and everything
works without it.

```bash
cd market
uv sync
uv run uvicorn app.main:app --reload --port 8100
uv run pytest                       # 110 tests, no network, no ffmpeg
```

That is the whole setup — `market/seed/media/` is committed, so the shop
has pictures the moment you clone it.

| URL | What it is |
|---|---|
| <http://localhost:8100/> | Storefront — browse, filter, buy one piece |
| <http://localhost:8100/b2b> | B2B portal — bulk quantity, deadline, split across a cluster |
| <http://localhost:8100/scan> | Every passport QR in the catalogue, scannable off the screen |
| <http://localhost:8100/reel/lst_51ea90cb7d24> | Reel generator |
| <http://localhost:8100/api/products> | The same catalogue as JSON |
| <http://localhost:8100/health> | `{"status": "ok", "adapters": "stub"}` |

#### What C1 owns, and what it only borrows

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

Those five seams are `Protocol` classes in
[`market/app/ports.py`](market/app/ports.py). B1 and B2 did not exist when
these screens were built, so each seam has a stub in
[`market/app/adapters/`](market/app/adapters/) and one environment
variable chooses between them:

```bash
CRAFTLY_ADAPTERS=stub   # seed JSON + placeholder price engine (default)
CRAFTLY_ADAPTERS=http   # the real B1 and B2 services
```

Two things follow, and they are the reason the design is shaped this way:

1. **C1 is demoable alone.** No database, no other service, no API key.
2. **Integration is a config change, not a rewrite.** When B1 and B2 ship,
   only `market/app/adapters/` changes. No route, template, search or
   quote calculation knows which implementation is behind it.

[`market/app/adapters/http_platform.py`](market/app/adapters/http_platform.py)
is the real adapter, written against endpoints that do not exist yet. It
is half implementation and half request: it states in code exactly what C1
needs from B1 and B2 and in what shape, so that conversation can be about
a diff instead of a whiteboard.

The stubs are stubs:

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

#### The shared contract, mirrored

[`market/app/contracts.py`](market/app/contracts.py) contains a **copy**
of A2's `Listing`. It is duplicated rather than imported because both
packages are called `app` and neither is installable, so
`from app.schema import Listing` inside `market/` would resolve to the
wrong module.
[`market/tests/test_schema_parity.py`](market/tests/test_schema_parity.py)
loads A2's file directly and fails if the two drift.

> **If that test goes red, do not edit the mirror to make it pass.** A
> schema change is a team-wide announcement. A red parity test means the
> announcement happened somewhere C1 did not hear it.

The same file also holds C1's proposal for the types B1 and B2 should
return — `PriceQuote`, `SplitPlan`, `Passport`, `Inventory`, `Artisan` —
and the `Order` that C1 produces and hands on.

#### The rule that shapes every surface

**A listing whose wage floor is incomplete cannot be sold.**

If `material_cost_inr` or `hours_worked` is null, the floor underneath the
price is a guess. Selling at a guessed floor is how an artisan ends up
working below minimum wage with a receipt proving everyone agreed. So
`PriceQuote.floor_incomplete` is set by the price engine,
[`market/app/view.py`](market/app/view.py) turns that into a refusal
naming the outstanding question ("we are still confirming hours of work"),
the product page hides the button, `POST /cart/add` refuses a
hand-written request, `cart.build()` drops the line if it got in anyway,
and the B2B quote returns an error instead of a number.

Four checks, because they happen at four different times and a listing can
lose its floor between them. `lst_77b0c4d8e913` in the seed data is
permanently in this state so the path is always on screen.

**The other honest failure:** some crafts sell on the open market for
*less than their own wage floor*. A hand-made terracotta diya is three
hours of work plus fifty rupees of clay; it fetches sixty to a hundred and
fifty rupees online. The buyer surfaces say so, in those words, and list
the piece at its floor rather than matching the market.
`lst_9f8a2e1c4b3d` is that case.

#### The surfaces

**Storefront — `/`.** Search across craft, material, colour, artisan,
village and state, with facet counts, price range, sort and paging.
[`market/app/search.py`](market/app/search.py) is pure functions over
already-priced cards, which is why the price filter works and why the
interesting parts are testable without a server. Ranking is explainable
rather than clever: field weights and prefix matching, so "pott" finds
pottery. Facet counts are computed **before** the structured filters, so
ticking "textiles" does not make every other category vanish and strand
the buyer with no way back.

**Product page — `/product/{listing_id}`.** Shows the price, then shows
how the price was built: materials, hours at the named state's minimum
wage, wastage, floor, comparable market band, passport premium, the
channel's cut, and what the artisan actually keeps. The take-home share is
on the tile in the grid too — it is the number that distinguishes this
marketplace from every other one, and a claim you only make after someone
clicks is a claim you are hiding. It also prints the same piece's Amazon
and Flipkart price beside its price here, with the artisan's take-home
held constant across all three: "buy direct" is a comparison the buyer can
check, not a slogan.

**B2B portal — `/b2b`.** Quantity, deadline, and an answer that is allowed
to be no. [`market/app/bulk.py`](market/app/bulk.py) returns a quote that
can be infeasible, and the page renders that refusal with the two
follow-ups a real procurement conversation produces —

> Not 900 by 14 October. They can commit **2 units** by that date.
> All 900 would need about **136 days**.

— each a one-click link to re-quote at that number. A spinner that
eventually produces an order the cluster cannot fill is how an artisan
ends up eating a penalty clause. **Volume discounts come out of the margin
above the floor, never out of the floor.** A bulk buyer can negotiate the
marketplace's share; they cannot negotiate the artisan's wage.

**Capacity pooling.** `stub_engine.py` spreads an order *proportionally
across the cluster* rather than pouring it into the first artisan until
she overflows. Greedy filling passes a naive feasibility test and is wrong
twice over: 500 diyas given to one woman who makes 900 a month is
seventeen days in which she can make nothing else, and the order takes far
longer than the same 500 spread across the village. The listing's own
artisan gets a weighted share, nobody is added for fewer units than is
worth packing, and anyone whose lead time exceeds the deadline is left out
rather than given work they cannot finish. What the stub does *not* do,
and B1 must: balance earnings across a cluster over time, know who is
mid-order on something else, or know that two artisans share one kiln.

**QR passport — `/p/{code}`.** The only surface in this system a buyer
meets while holding the object. Someone at a mela picks up a printed
hang-tag, scans it, and the page has about four seconds to answer "who
made this, and is that claim real" on a phone with one bar of signal. So
it does not extend the site chrome — no navigation, no basket, no search.
Maker, place, the making clip if there is one, the chain of how the object
came to exist, what the maker was paid, the verification code, and only
then the reorder link. That reorder link is what turns a one-off mela sale
into repeat business: a shopkeeper who sold twenty can scan the tag on the
last one and order twenty more from the same woman without knowing her
name, her number, or which co-operative she belongs to.

The code is short, spoken and unambiguous — `CR-R8CR-9SG8`. The alphabet
drops `0/O` and `1/I/L`, because the failure mode is not a cryptographic
attack, it is a buyer squinting at a smudged tag and reading it down a
phone line. A code that resolves to nothing gets a page saying "we cannot
verify this" and a box to retype it, not a generic 404 — being checkable
is the entire value of the passport. `/tag/{listing_id}` renders printable
hang-tags; `/scan` lists every code with its QR so a judge can walk the
mela journey without a printed tag in hand.

**Reels — `/reel/{listing_id}`.** Fifteen seconds, 1080×1920, built from
one photo and the listing. A reel is an argument in four beats: what it is
and where a human made it; how it was made and how long that took; what it
costs and how much reaches her; where to get it and the code that proves
the claim. [`market/app/captions.py`](market/app/captions.py) builds that
as data, separately from the rendering, which is what makes the wording
testable. Every number comes from the listing and the price quote, and **a
fact that is missing is dropped rather than invented** — a reel that says
"16 hours of work" when `hours_worked` is null is a lie with a soundtrack,
and this is a system whose entire pitch is that the provenance is real.

Pillow composes each beat into a full frame; ffmpeg then gives each still
a slow push and splices the making clip into beat two if one exists.
**The storyboard fallback is not a consolation prize, it is the
contract** — with no ffmpeg on the machine, the same frames come back as
PNGs with a warning saying why. Degrade, don't crash, the same rule A2's
pipeline holds itself to. No music: an unlicensed track is a takedown
waiting to happen on exactly the marketplaces this project is trying to
reach. Hindi captions need a Devanagari font (Nirmala UI on Windows, Noto
Sans Devanagari elsewhere); with none, the reel is captioned in English
and says so, because tofu boxes are worse than English.

**JSON API — `/api/...`**

| Endpoint | For |
|---|---|
| `GET /api/products` | Search and filter, all the same parameters as the storefront |
| `GET /api/products/{id}?channel=amazon` | **C2**: the priced, channel-aware listing to push to a marketplace |
| `GET /api/passports/{code}` | **A1**: resolve a scanned code |
| `GET /api/bulk/quote?listing_id=&quantity=&deadline=` | A bulk quote, including infeasibility |

#### Why server-rendered

No React, no build step, no `npm install` between a teammate and a running
storefront. Every page is Jinja plus one hand-written stylesheet, because
it has to open on a cheap phone on mela wifi, it has to still run the week
after the demo on someone else's laptop, and the QR landing page in
particular must render on the first paint — a passport page that boots a
JavaScript bundle before it can say who made the object has missed the
point of being scanned. The JSON API exists so that this is a choice
rather than a trap: a single-page app can replace the templates later
without touching `search.py`, `bulk.py` or `view.py`.

#### Details worth knowing

**Indian digit grouping.** `₹12,34,567`, not `₹1,234,567`. Python's
`{:,}` gets this wrong, and a marketplace for Indian craft printing Indian
prices in American grouping looks, to the people it is for, like it was
built for someone else. See [`market/app/money.py`](market/app/money.py).

**A missing price is a dash, not a zero.** `rupees(None)` is `—`. Zero is
a claim; missing is a question.

**Bilingual with honest fallback.** Buyer pages render in English or
Hindi, because those are the two languages A2 actually generates.
`title_hi` is null when A2 could not produce it, and `card.title('hi')`
falls back rather than showing a blank.

**The basket is a signed cookie holding listing ids and quantities — and
no prices.** Prices are re-fetched from B1 on every render, so a basket
left open for three days cannot check out at a stale price and nobody can
edit a cookie into a discount. Signed, so a tampered cookie is discarded
as if the basket were empty.

**Seed media are generated placeholders.** The seed catalogue describes
real crafts and this repo has no photographs of them, so
`market/scripts/generate_seed_media.py` draws a swatch per listing in the
colours the artisan named. It is deterministic; re-run it after editing
`seed/listings.json`. Drop a real photo in over any file of the same name
and every surface picks it up.

#### Layout of `market/`

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

#### Known gaps

- **`http_platform.py` has never talked to a real server.** The endpoint
  shapes are C1's proposal; B1 and B2 have not agreed to them yet.
- **Prices are fetched one listing at a time.** Twelve tiles on a browse
  page is twelve calls to B1 over HTTP. The fix is a batch price endpoint
  in B1's interface, not a cache in C1 — a stale price in a basket is a
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

### C2 — Integrations  🔲 TODO

> **Owner:** _TBD_
>
> Marketplace adapters, courier booking, AI voice call and WhatsApp.
>
> - [ ] Channel adapter interface + one real live integration
> - [ ] Courier API → waybill + pickup slot
> - [ ] AI call: order confirmed, pickup scheduled, payment credited
> - [ ] Weekly demand alert (real data only, never generic marketing)
>
> Two things C1 already provides to build against:
> `GET /api/products/{id}?channel=amazon` returns the listing priced for
> that channel with the artisan's take-home held constant — that is the
> payload a marketplace adapter pushes. And every placed order is appended
> to `market/orders.jsonl` in the `Order` shape from
> [`market/app/contracts.py`](market/app/contracts.py), which is what the
> courier booking and the voice call read.
>
> _Setup and run instructions go here._

---

## Running the whole thing

Three services, three ports, nothing shared but HTTP.

```bash
cd platform && uv run python -m app.seed                          # once
cd platform && uv run uvicorn app.main:app --reload --port 8200   # B2
cd capture  && uv run uvicorn app.main:app --reload --port 8000   # A2
cd market   && uv run uvicorn app.main:app --reload --port 8100   # C1
```

By default C1 still runs off its own seed catalogue, which keeps it
demoable with nothing else up. To point it at the real database instead:

```bash
cd market
CRAFTLY_ADAPTERS=http CRAFTLY_PLATFORM_URL=http://localhost:8200 \
  uv run uvicorn app.main:app --reload --port 8100
```

Everything C1 shows — catalogue, artisans, inventory, passports, QR codes,
placed orders — then comes from B2. The verification codes do not change,
so any hang-tag already printed still resolves.

> **Ports.** B2 runs on **8200**, not the `8000` that
> `market/.env.example` suggests as `CRAFTLY_PLATFORM_URL` — A2 is already
> on 8000, and pointing C1 there gives it the capture service and a 404
> shop. Set the variable explicitly, as above.
>
> **Prices still need B1's seam closing.** C1 assumes `POST /price` and
> `POST /split` on `CRAFTLY_ENGINES_URL`; B1's service exposes
> `POST /api/predict` and `POST /api/bulk-order`. Until those agree, a
> fully-`http` C1 returns 503 on any page that needs a price. Catalogue,
> passports, QR and orders are unaffected — that is B2, and it is wired.

### The end-to-end path, today

1. A2 turns a photo and a voice note into a `Listing`.
2. `POST /listings` on B2 persists it; publishing mints its passport and QR.
3. C1 reads `GET /catalog/entries` and renders the shop.
4. A buyer scans a tag at a mela; `GET /passports/by-code/{code}` answers.
5. Checkout `POST /orders` to B2.
6. The artisan accepts, dispatches and delivers from her phone.
7. Delivery settles the order and writes a payout row per artisan, with
   her take-home paid exactly as quoted and the marketplace's fee taken
   only from the margin above the wage floor.

## Team

| Role | Name |
|---|---|
| A1 — Artisan app | Tejas Kollipara |
| A2 — Capture AI | _TBD_ |
| B1 — Engines | _TBD_ |
| B2 — Platform | _TBD_ |
| C1 — Buyer surfaces | _TBD_ |
| C2 — Integrations | _TBD_ |

## Licence

> 🔲 **TODO** — add a `LICENSE` file. MIT or Apache 2.0.

Third-party model licences are documented per-component; see
[`capture/README.md`](capture/README.md) for the background-removal model
licence analysis.
