# Craftly

**One photo, one voice note, a year-round market.**

Craftly lets a marginalized artisan list, price, prove, and sell a handmade
product without typing, reading English, or understanding e-commerce. She
photographs the product and describes it out loud in her own language.
Everything after that is automated: photo cleanup, catalogue copy, a
wage-floor-backed price, an authenticity "craft passport," publication to
multiple sales channels, buyer matching, order splitting across a cluster of
artisans, courier booking, and a phone call in her language confirming she's
been paid.

## How it works

```
Artisan: one photo + one voice note (what it is, material cost, hours worked)
                              │
                              ▼
              Capture AI cleans the photo, transcribes and
              extracts fields, writes bilingual copy, reads
                      the listing back to her
                              │
                              ▼
        Pricing engine sets a wage-floor-backed price per
         channel and mints an authenticity "craft passport"
                              │
                              ▼
              Live listing, stored in the platform database
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   Storefront /          Marketplace push       Mela QR code /
   B2B portal          (Amazon/Flipkart/eBay)   15s vertical reel
        │                     │                     │
        └──────────┬──────────┴─────────────────────┘
                    ▼
      Buyer orders — bulk orders are matched and split
          across a cluster of artisans by capacity
                    │
                    ▼
      Courier booked, artisan notified, order delivered
                    │
                    ▼
      Payment settles: each artisan is paid exactly her
       quoted take-home, split proportionally by share
                    │
                    ▼
     A phone call in her own language confirms she's paid
                    ↻
      Sales data feeds back as weekly demand alerts
```

## Why the wage floor matters

Every price Craftly quotes is built on top of a **sustainable wage floor**:
`(material cost + hours worked × fair hourly wage) × overhead`. A listing is
only sellable once that floor is known — an artisan who says "I don't know"
about her hours or costs gets asked, rather than silently priced at zero.
This rule is enforced everywhere: at the pricing engine, on the product
page (the buy button hides), in the cart (adding is refused), and in B2B
quoting (the quote errors out). Some products intentionally sell **below**
the going market rate rather than under their own wage floor — the honest
answer is "artisans keep making it at cost," never a race to the bottom.

## Architecture

Craftly is five independent services that talk to each other over plain
HTTP, built by three pairs of teammates:

| Service | Role | Port | What it owns |
|---|---|---|---|
| [`capture/`](capture/) | **Capture AI** | `:8000` | Turns a photo + voice note into a structured listing: speech-to-text, deterministic money/duration parsing, LLM field extraction, bilingual description generation, spoken readback, photo cleanup |
| [`engines/`](engines/) | **Pricing & order engine** | `:8010` | The market-price ML model, comparable-listing search, the wage floor, per-channel pricing, bulk-order capacity allocation across a cluster, the craft passport + QR object |
| [`platform/`](platform/) | **Platform** | `:8200` | The persistent database, artisan/buyer auth, media storage, passport issuance, and the payment-split logic that turns an order into money in a named artisan's account |
| [`market/`](market/) | **Buyer surfaces** | `:8100` | Everything a buyer sees: storefront, B2B bulk-order portal, the QR passport landing page scanned at a mela, and the 15-second reel generator |
| [`integrations/`](integrations/) | **Channel integrations** | `:8300` | Marketplace push (Amazon/Flipkart/eBay), courier booking, an AI voice call to the artisan, and a weekly demand alert |

Each service is documented in depth in its own README — this file is the
map, not a duplicate.

### The shared contract

Everything is built around one object: the **Listing**, defined in
[`capture/app/schema.py`](capture/app/schema.py) with a filled example at
[`capture/schema/listing.example.json`](capture/schema/listing.example.json).

**Every extracted field is nullable, and a missing value is `null` — never
a guessed default.** In particular, `material_cost_inr` and `hours_worked`
must never default to `0`: `0` means the artisan said there was no cost or
time; `null` means it's unknown and she must be asked. Those two numbers
feed the wage floor everywhere downstream, so a wrong default there would
silently underpay someone. Any change to this schema is treated as a
team-wide announcement, not just a commit — `market/` and `platform/` both
run parity tests that diff their own copies of the schema against
`capture`'s source of truth.

### How the services connect

1. **Capture** (`:8000`) turns a photo + voice note into a `Listing`.
2. **Platform** (`:8200`) persists listings (`POST /listings`, idempotent),
   and mints a passport + QR code once a listing is published. It's the
   single source of truth that `market` and `integrations` read from.
3. **Engines** (`:8010`) prices listings and allocates bulk orders. `market`
   calls it through two clean seams (`PriceEngine`, `OrderEngine`) that can
   point at either an in-memory stub (for a self-contained demo) or the real
   HTTP service.
4. **Market** (`:8100`) is the buyer-facing aggregator — it reads the
   catalogue from platform, prices through engines, and writes orders back
   to platform (or to a local file in stub mode).
5. **Integrations** (`:8300`) reads placed orders, then pushes them to
   marketplaces, books a courier, and places an AI call — all built against
   real priced order data, never recomputing a price itself.

## Running it end to end

There's no single launch command yet — each service is its own Python
environment, started in its own terminal. Most use [`uv`](https://docs.astral.sh/uv/);
`engines/` uses a plain venv because of its heavier ML dependencies
(PyTorch, LightGBM, XGBoost).

```bash
# 1. Platform — the database everything else reads from
cd platform && uv sync
uv run python -m app.seed          # loads market/seed/ into the DB
uv run uvicorn app.main:app --reload --port 8200

# 2. Engines — pricing + order allocation
cd engines
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python scripts/service.py serve --port 8010

# 3. Capture — photo + voice → listing
cd capture && uv sync
uv run uvicorn app.main:app --reload --port 8000

# 4. Market — storefront, B2B portal, QR passports, reels
cd market && uv sync
uv run uvicorn app.main:app --reload --port 8100

# 5. Integrations — marketplace push, courier, AI calls, demand alerts
cd integrations
uv run uvicorn c2.app:app --reload --port 8300
```

`market/` can also run entirely on its own, off seed data and stub
engines, with no other service running — set `CRAFTLY_ADAPTERS=stub` (the
default) instead of `http`. That's the fastest way to see the buyer
experience without standing up the whole stack.

Each service needs its own `.env` (see each folder's `.env.example`).
Nothing here reads secrets from the repo — the only real external
credential needed anywhere is a Groq API key for `capture/`.

## What each service does

### Capture AI (`capture/`)

FastAPI service. Speech-to-text (faster-whisper), a deterministic
money/duration parser, LLM-based semantic field extraction (Groq), bilingual
(English/Hindi) description generation, spoken readback (text-to-speech),
and product photo cleanup — all wired into one call, `POST /listing/create`.
`GET /demo` is a one-screen manual test page, and `GET /mobile-demo` serves
a phone-shaped browser demo (camera + mic capture, confirmation playback,
offline queue with sync, fulfilment view, language switch) that stands in
for the artisan mobile app for this prototype — it calls the real capture
API, nothing in it is faked. Number resolution is fully wired for Hindi and
English; Tamil, Telugu, and Kannada are scaffolded but not yet completed.

Full docs: [`capture/README.md`](capture/README.md)

### Pricing & order engine (`engines/`)

FastAPI service backed by a real trained model, not a lookup table. Product
price is estimated from a 913-dimension feature vector (CLIP image
embeddings + MiniLM text embeddings + structured category/material features
+ nearest-neighbour comparable stats) fed into gradient-boosted regressors,
trained on ~12k cleaned product listings with a conformal-calibrated price
band. On top of that sits the business logic: the wage floor, a passport
premium, per-channel pricing (direct/marketplace/B2B) with an explicit
artisan take-home on every channel, and a deterministic bulk-order
allocation algorithm that splits a large order across a cluster of artisans
by capacity. Also owns creating the craft passport + QR object.

Full docs: [`engines/README.md`](engines/README.md)

### Platform (`platform/`)

The persistent backbone. SQLite by default (swappable to Postgres with a
single environment variable), holding listings, artisans, orders, media,
passports, and payouts. Artisan/buyer auth uses opaque rotating tokens
(not JWTs) so a lost or shared phone can be revoked instantly. Media is
stored content-addressed and sniffed by file signature rather than trusted
client headers.

The payment split logic is the core of this service: an artisan is always
paid exactly the take-home number she was quoted, a platform fee is taken
only from the margin above the wage floor (never from the floor itself),
payouts that can't be sent are recorded as `blocked` with a reason rather
than silently dropped, and settlement is idempotent against retries.

Full docs: [`platform/README.md`](platform/README.md)

### Buyer surfaces (`market/`)

FastAPI + server-rendered templates (no JS build step, by design — this has
to load fast on patchy mela wifi). Storefront with browse/search/filter,
a B2B bulk-order portal with capacity-aware feasibility checks, a QR
passport landing page for buyers scanning a hang-tag in person, printable
tags, and a 15-second vertical reel generator for social sharing. The
product page shows a full price breakdown — materials, hours × wage, the
floor, market comparables, the passport premium, channel cut, and artisan
take-home — including a side-by-side comparison to what the same piece
would cost on a big marketplace with the artisan's take-home held constant.

Documented in this repo's top-level docs alongside the other services; see
the routes and adapters under [`market/app/`](market/app/) for detail.

### Channel integrations (`integrations/`)

Pushes priced listings out to marketplaces (Amazon/Flipkart/eBay), books a
courier per order (including splitting pickups across artisans for a bulk
order), places an AI voice call to the artisan in her language when an
order is confirmed and again when payment is credited, and computes a
weekly demand alert from real order history. Run `python demo.py` for a
terminal walkthrough of the whole flow, or `uvicorn c2.app:app` for a small
web console.

Full docs: [`integrations/README.md`](integrations/README.md)

## Tests

Every service ships its own test suite (run with `pytest` inside that
service's environment): capture covers the full audio → listing pipeline
including bad-photo fixtures for blur detection; engines covers pricing
business rules, allocation determinism, and a full model-integrity/API
check; market covers routes, cart/pricing, the reel generator, and schema
parity against capture's contract; platform covers auth, orders, passports,
payments, and schema/seed parity; integrations covers the whole push →
courier → call → demand-alert flow hermetically, with a parity check
against the real order shape.

## Repo layout

```
capture/        Capture AI — photo + voice → structured listing
engines/        Pricing model, wage floor, bulk-order allocation, passport/QR
platform/       Database, auth, media, passport issuance, payment splits
market/         Storefront, B2B portal, QR passport pages, reels
integrations/   Marketplace push, courier, AI calls, demand alerts
```

## License

No license has been chosen yet for this prototype.
