# B2 — Platform

*Database, auth, APIs, media storage, craft passports and QR, payment
split, deployment.*

This is the slice nobody sees. A2 turns a photo and a voice note into a
`Listing`; C1 renders a `Listing` as a shop. Until this service existed
there was nothing in between: A2 wrote listings into an in-memory dict
that died with the process, and C1 read three JSON files off disk. This is
the join, and the place an order becomes money in a named woman's account.

```bash
cd platform
uv sync
uv run python -m app.seed                                  # fill the database
uv run uvicorn app.main:app --reload --port 8200
uv run pytest                                              # 134 tests, no network
```

That is the whole setup. No database server, no credentials, no API key —
SQLite by default, because six people on six laptops and a demo on a
seventh is not a situation where "first install Postgres" is a reasonable
first step. `CRAFTLY_DATABASE_URL=postgresql+psycopg://...` switches it and
nothing in `app/` changes.

| URL | What it is |
|---|---|
| <http://localhost:8200/health> | Status, plus counts of what is actually in the database |
| <http://localhost:8200/docs> | Every endpoint, generated from the code |
| <http://localhost:8200/catalog/entries> | The catalogue C1 renders |
| <http://localhost:8200/passports/by-code/CR-R8CR-9SG8> | A passport, resolved from a tag |
| <http://localhost:8200/qr/CR-R8CR-9SG8.png> | The QR for that tag |

---

## What this slice is responsible for

```
   A2 capture ────POST /listings────┐
   A1 app     ────POST /media───────┤
                                    ▼
                          ┌──────────────────┐
                          │  B2: platform/   │
                          │                  │
   C1 buyer   ◀──GET /catalog/entries────────┤  listings   artisans
   surfaces   ◀──GET /passports/by-code/─────┤  inventory  passports
              ────POST /orders───────────────┤  orders     payouts
                                             │  accounts   media
   B1 engines ────(reads entries, returns ───┤
                   prices and split plans)   └──────────────────┘
                                    │
                          delivered ▼ split to each artisan
```

It does **not** own: a price, a split plan, a courier, a screen, or a
photograph's contents. Those belong to B1, C2, C1 and A2 respectively.
This service stores what they produce and refuses to second-guess it —
see "The price on the page is the price stored" below.

---

## The integration, proved rather than promised

C1's `market/app/adapters/http_platform.py` was written before this
service existed. It is half implementation and half request: it states in
code exactly which endpoints C1 needs and in what shape. **Every endpoint
in that file is implemented here, with those paths and those shapes.**

So this works, today:

```bash
# terminal 1
cd platform && uv run python -m app.seed && uv run uvicorn app.main:app --port 8200

# terminal 2
cd market
CRAFTLY_ADAPTERS=http CRAFTLY_PLATFORM_URL=http://localhost:8200 \
  uv run uvicorn app.main:app --port 8100
```

The storefront, the product pages, the B2B portal, the `/scan` sheet and
every QR passport page now render off this database instead of off C1's
seed JSON — same twelve products, same photos, and **the same verification
codes**, down to `CR-R8CR-9SG8` being the same tag it always was.

That last part is not a coincidence, it is a constraint. `app/ids.py`
derives codes with the identical algorithm C1's stub used, and
`tests/test_passports.py::test_codes_match_the_ones_c1_has_been_printing`
loads that algorithm out of C1's own source file and compares. Every
hang-tag printed off the stub is already in the world; a service that
minted different codes would silently orphan all of them.

`tests/test_seed_parity.py` holds the rest of the line: every field of
every seeded listing, artisan and inventory row survives the round trip
through the database unchanged.

> **One gap, and it is not B2's.** With `CRAFTLY_ADAPTERS=http`, C1 routes
> *prices* to `CRAFTLY_ENGINES_URL` too, and B1's service exposes
> `POST /api/predict`, not the `POST /price` C1 assumes. Until that is
> reconciled, running C1 fully on `http` gives a 503 on every page that
> needs a price — correctly, because there is no price. B2 is not in that
> loop. The two-line fix belongs in `http_platform.py` or in B1.

---

## Layout

```
platform/
  app/
    main.py          FastAPI app, startup warnings, /health
    config.py        environment settings, all with working defaults
    contracts.py     the negotiated wire types (mirror of A2's Listing)
    schemas.py       B2's own request/response bodies
    db.py            engine, session, SQLite pragmas
    models.py        the schema — 15 tables
    ids.py           id minting, and the spoken verification code
    security.py      scrypt hashing, token minting, phone normalisation
    auth.py          accounts, OTP, tokens, the route dependencies
    catalog.py       the listing+artisan+inventory join C1 reads
    passports.py     passport assembly, code resolution, QR minting
    orders.py        placing, the status machine, the wire shape
    payments.py      the split  ← the file to read first
    media.py         content-addressed file storage
    seed.py          import C1's catalogue into the database
    routes/
      catalog.py  passports.py  orders.py  listings.py  media.py  auth.py
  tests/             134 tests
  Dockerfile  docker-compose.yml  .env.example
```

---

## The five rules in `app/payments.py`

This is the part worth reviewing properly. Everything else here is
plumbing; this is the file that decides what a woman is paid.

**1. The artisan's take-home is not a residual.** It is B1's number,
quoted to the buyer on the page, stored on the line, and paid out
unchanged. The marketplace's cut is what is left *after* it. Systems that
compute the platform fee first and hand the artisan the remainder are how
someone ends up below minimum wage with a receipt proving everyone agreed.

**2. The fee comes out of the margin above the floor, and the margin can
be zero.** `CRAFTLY_PLATFORM_FEE_PCT` is applied to `price − take_home`,
never to `price`. When the margin is zero or negative — which happens on
exactly the crafts whose market price sits *under* their own wage floor,
like the terracotta diya in the seed data — the fee is zero and the
marketplace absorbs the difference. That loss is recorded as
`platform_absorbed_inr` rather than quietly netted off, because a
marketplace losing money on a category should have to look at the number.

**3. A payout we cannot send is `blocked`, never dropped.** No UPI id on
file? The row is still written, with the full amount and a reason. The
money is owed whether or not we know where to send it, and a system that
skips the row loses the debt. `GET /payouts/mine` shows blocked rows to
the artisan, because a woman owed ₹4,200 that cannot be sent needs to see
the ₹4,200 and the reason — a list that quietly omits it tells her she
earned nothing.

**4. Settlement is idempotent.** A unique constraint on
`(order, line, artisan)`, plus a check before every write. Courier
webhooks retry and deliveries get confirmed twice; neither may pay twice,
and neither may error on the second attempt and leave an order
half-settled.

**5. An incomplete line blocks the split, it does not guess it.** A line
whose `artisan_take_home_inr` is null reached us without B1 having
resolved a wage floor. There is no honest number to pay, so the settlement
returns `blocked` rows naming the problem and a human answers it. Paying a
plausible-looking guess is the one outcome worse than paying late.

A worked example, from the integration run above — C1 places a three-unit
order for an Ajrakh runner at ₹2,400 with a quoted take-home of ₹1,800:

```json
{ "gross_inr": 7200, "artisan_total_inr": 5400,
  "platform_fee_inr": 180, "platform_absorbed_inr": 0,
  "artisan_share_pct": 75.0, "complete": true,
  "payouts": [{ "artisan_name": "Hansaben Vankar", "amount_inr": 5400,
                "status": "pending" }] }
```

₹180 is 10% of the ₹600 margin, not 10% of ₹7,200. `artisan_share_pct` is
computed from the rows rather than asserted on a banner.

---

## Other decisions worth knowing

**The price on the page is the price stored.** `POST /orders` does not
call B1 to re-check anything. The buyer saw a number and agreed to it; a
platform that recalculates at write time can disagree with its own
receipt. If B1's price moved between the page render and the submit, the
buyer's number wins and the difference is the marketplace's problem.

**Nullable means unknown, in the columns as well as the contract.**
`material_cost_inr` and `hours_worked` are nullable with no server
default. A `DEFAULT 0` on either turns "we have not asked her yet" into
"it cost nothing and took no time", which is how a wage floor collapses to
zero with nobody noticing. `tests/test_schema_parity.py` asserts it at
three levels: A2's schema, the three mirrors, and the table itself.

**Artisans log in with a phone and a one-time code; buyers with an email
and a password.** The target user of this system may not read English and
may be using a smartphone for the first time. "Choose a password with a
number and a symbol, and remember it" is a barrier invented for a
different user; a six-digit code read out by a voice she understands is
not. A buyer placing a ₹90,000 B2B order is a procurement officer at a
desk, and a magic link to a shared purchasing inbox is worse for them than
a password.

**Tokens are rows, not JWTs.** A JWT would save a database lookup and cost
the ability to revoke before expiry. These tokens go on phones that get
sold, lost and shared in a village; instant revocation is worth the
lookup. Only a SHA-256 of each token is stored, so a database leak yields
nothing usable.

**Passwords and OTPs go through `hashlib.scrypt`.** Standard library,
memory-hard, no wheel that has to compile on a teammate's laptop at 2am.
Tokens deliberately do *not* — they are already 256 bits from `secrets`,
so there is nothing to brute-force and a slow hash on every request would
cost more than it buys.

**Media is content-addressed.** A file is stored under the SHA-256 of its
own bytes. A1's offline queue retries an upload it is not sure completed,
and addressing by content means the retry writes the same path — no
duplicate, and her data is not spent twice. It also means a URL on a
passport cannot be made to point at different bytes later, which matters
on an object whose entire purpose is proof.

**Upload types are sniffed, not trusted.** A client can declare anything;
the magic number at the head of the file is harder to lie about. This is
not a sandbox, but it does stop a broken client posting an HTML error page
as a JPEG and a storefront rendering a broken image for the rest of a demo.

**The QR encodes C1's `/p/{code}`, not this service's own URL.** A buyer
scanning a tag should land on a page designed for a phone with one bar of
signal, and that page is C1's. `CRAFTLY_PUBLIC_URL` must therefore be
C1's address *and* reachable from a device — a QR baked with `localhost`
scans to nothing, which is the single most likely way this feature fails
at a demo. `/health` warns about it.

**A revoked or unpublished listing's passport still resolves.** The
question a buyer asks by scanning is "is this real", and silence is a
worse answer than "this tag was withdrawn". "No longer for sale" and
"never real" are different claims, and only one of them is true.

**The status machine only moves forwards, and cancellation stops at
dispatch.** Once a courier has the box the question is a return, which is
a different process with different money in it. Re-delivering an already
delivered order is a no-op rather than an error, because a webhook that
retries is normal and a 500 on it is how an order gets stuck.

**Every status change is an append-only event row.** The current status is
derivable from the history, and a dispute is arguable from it.

---

## The two insecure defaults, on purpose and out loud

Both are logged at startup and listed in `GET /health`.

| Setting | Default | Why | Fix |
|---|---|---|---|
| `CRAFTLY_REQUIRE_ORDER_AUTH` | `false` | C1's `HttpOrderSink` sends no `Authorization` header — it was written before this service existed. Requiring one on day one breaks the only integration this endpoint has. | Set `true` once C1's adapter sends a token. |
| `CRAFTLY_OTP_ECHO` | `true` | There is no SMS gateway in this repo; C2 owns delivery. A login you cannot complete is not a login. | Set `false` and wire C2's channel into `POST /auth/artisan/request-otp`. |

A default that is wrong for production is fine. A default that is wrong
for production *and silent* is how it ships.

---

## Endpoints

**For C1** (no auth — a shop window that needs a token is a shop nobody
walks past):

| Endpoint | Returns |
|---|---|
| `GET /catalog/entries` | Every published listing + artisan + inventory |
| `GET /catalog/entries/{listing_id}` | One, or 404 |
| `GET /clusters/{cluster_id}/members` | The pool a bulk order splits across |
| `GET /clusters/{cluster_id}/entries` | That cluster's listings, `?craft_type=` |
| `GET /passports/by-listing/{listing_id}` | The proof object |
| `GET /passports/by-code/{code}` | The same, from a scanned or typed tag |
| `GET /qr/{code}.png` | The QR image, minted once and cached |
| `POST /orders` · `GET /orders/{order_id}` | Place and read back |

**For A1 and A2** (artisan token):

| Endpoint | For |
|---|---|
| `POST /auth/artisan/request-otp` → `/verify` | Login |
| `GET`/`PATCH /auth/me` | Profile, language, AI-call consent, payout details |
| `POST /listings` | Persist a capture. Idempotent on `listing_id` |
| `PATCH /listings/{id}` | Corrections from the confirmation loop; publish |
| `GET /listings/mine` | Her listings, drafts included |
| `POST /media` | Upload a photo, clip or voice note |
| `GET /orders/mine/artisan` | The fulfilment view |
| `POST /orders/{id}/status` | Accept, dispatch, deliver |
| `GET /payouts/mine` | Paid, pending and blocked |

**For anyone:** `GET /health`, `GET /media/{filename}`, `GET /docs`.

---

## Deployment

```bash
docker compose up --build
docker compose run --rm platform python -m app.seed
```

Non-root user, two declared volumes, a healthcheck that counts rows rather
than returning a constant. `CRAFTLY_PUBLIC_URL` is the setting most likely
to be wrong — on a laptop at a demo it is the laptop's LAN IP:

```bash
CRAFTLY_PUBLIC_URL=http://192.168.1.5:8100 docker compose up
```

Schema creation is `create_all`, not Alembic. There is one deployed
instance and its catalogue is re-seedable from `market/seed/`; a migration
tool earns its place when neither of those is true.

---

## Known gaps

- **The container image has never been built.** There is no Docker on the
  machine this was written on, so the `Dockerfile` and `docker-compose.yml`
  are reviewed but unverified. Everything above them — the service, the
  seed importer, the tests, the live integration with C1 — was run.
  Whoever has Docker should do a `docker compose up --build` first.
- **No real payment rail.** `settle` computes what is owed and writes
  payout rows; `POST /payouts/{id}/paid` is a manual acknowledgement that
  a transfer happened. Wiring UPI or a bank file is the next piece, and
  the shape it plugs into is already there.
- **`CRAFTLY_PLATFORM_FEE_PCT` is one flat number.** Real marketplace
  economics vary by channel and category. The rule it enforces — fee off
  the margin, never off the floor — is the part that matters and is
  already correct.
- **Deleting a `Media` row does not delete the file.** Content addressing
  means two listings can share bytes, so removal is a garbage-collection
  pass nobody needs yet. Written down rather than half-implemented.
- **No rate limit on `POST /auth/artisan/request-otp`.** Codes are
  attempt-capped and expire, but nothing stops someone requesting many. A
  limiter belongs at the reverse proxy, not in this process.
- **Orders are open by default.** See the table above. One setting.
- **No pagination on `GET /catalog/entries`.** Honest at a few thousand
  items, wrong at a million; C1 filters in-process and the seam is placed
  so the query pushes down here without the screens noticing.
- **B1's HTTP shape and C1's assumption about it disagree.** Not B2's
  loop, but it is what stops `CRAFTLY_ADAPTERS=http` being a complete
  end-to-end run today. Noted above.
