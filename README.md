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
| `platform/` | Pair B | TODO |
| `market/` | Pair C | TODO |
| `app/` (mobile) | Pair A | TODO |
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

### A1 — Artisan mobile app  🔲 TODO

> **Owner:** _TBD_
>
> Capture screen, two-button home, offline queue and sync, fulfilment
> view, language switching, onboarding and consent.
>
> - [ ] Camera + mic capture, calls `POST /listing/create`
> - [ ] Confirmation loop UI (plays `summary_spoken`, one tap to confirm)
> - [ ] Offline queue — capture works with no network, syncs later
> - [ ] Fulfilment view: accept order, packing slip, dispatch
> - [ ] Language selection at onboarding + AI-call consent toggle
>
> _Setup and run instructions go here._

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

### B2 — Platform  🔲 TODO

> **Owner:** _TBD_
>
> Database, auth, APIs, media storage, payment split, craft passport
> assembly + QR generation, deployment.
>
> - [ ] Persistent store (replaces `capture/app/store.py`)
> - [ ] Artisan + buyer auth
> - [ ] Passport object: artisan, village, craft, material, hours,
>       making-clip, QR
> - [ ] Payment split on delivery
> - [ ] Deploy
>
> _Setup and run instructions go here._

---

## Pair C — Buyer side and the outside world

### C1 — Buyer surfaces  🔲 TODO

> **Owner:** _TBD_
>
> Buyer view, B2B portal, QR landing page, reel generation.
>
> - [ ] Browse / search / buy one piece
> - [ ] B2B bulk order with quantity and deadline
> - [ ] QR scan → passport page + reorder link
> - [ ] 15s vertical reel from photo + making-clip + captions
>
> _Setup and run instructions go here._

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
> _Setup and run instructions go here._

---

## Running the whole thing

> 🔲 **TODO** — fill in once B2 has a deploy and the three slices are
> wired together.

## Team

| Role | Name |
|---|---|
| A1 — Artisan app | _TBD_ |
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
