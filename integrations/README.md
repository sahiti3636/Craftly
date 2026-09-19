# C2 — Integrations (simulated)

Marketplace push, courier booking, AI calls to artisans and the weekly demand
alert. For the prototype **nothing here calls an outside service**: each
integration is a simulation behind the interface a real one would implement.
Anything a simulation produces says `"simulated": true`.

## Run it

```bash
cd integrations
uv run python demo.py                              # whole flow, in the terminal
uv run uvicorn c2.app:app --reload --port 8300     # console: http://localhost:8300
uv run pytest
```

No `uv`? `pip install fastapi "uvicorn[standard]" pydantic httpx python-dotenv pytest`,
then `python demo.py` / `python -m uvicorn c2.app:app --port 8300` / `python -m pytest`.

For prices over HTTP, start C1 first (`cd market && uv run uvicorn app.main:app --port 8100`).
C2 does not need it: if C1 is not up, C2 loads C1's route code in-process (this
needs C1's dependencies installed). If that fails too, priced pushes are refused
rather than sent without a price.

To fulfil a real order: add something to the basket on C1 (`localhost:8100`), check
out, then open C2's console. It reads the order from `market/orders.jsonl`. With no
orders yet, C2 uses four **sample** orders built from C1's catalogue and says so.

## What is simulated, what is real

| Piece | Module | Real | Simulated |
|---|---|---|---|
| Marketplace push | `channels.py` | Payload built from C1's `/api/products/{id}?channel=`; price and take-home are C1's; refusal rules | The marketplace's reply (ids are deterministic hashes) |
| Courier | `courier.py` | Pickups per artisan, incl. bulk splits; lead-time and Sunday rules | The booking, waybill numbers, courier names, transit time |
| AI call | `voice.py` | Script text from real order, shipment and payout; language choice | The call itself, the artisan's phone number, the outcome |
| Demand alert | `demand.py` | **Everything**: units per listing over 7 days, stock check | — (labelled simulated only when it ran on sample orders) |

## Rules carried over from the rest of the repo

- **Missing is not zero.** A payout with no take-home figure from C1 is `None`;
  the call then says nothing about money instead of promising ₹0.
- **No price, no push.** A listing with no quote, an incomplete wage floor, or
  that C1 says cannot be bought is never sent to a marketplace.
- **No generic marketing.** With no orders in the window the demand alert says
  "nothing to report".
- **Take-home is constant across channels.** The settlement preview on a push
  shows buyer price, channel fee and artisan receipts straight from C1's quote.

## Layout

```
integrations/
  demo.py            terminal demo: push -> courier -> calls -> delivery -> alert
  c2/
    app.py           FastAPI on :8300 (JSON API + console.html)
    c1_client.py     the only module that knows where C1 is: HTTP -> in-process -> seed
    models.py        read-only mirror of C1's Order + C2's own records
    channels.py      Amazon / Flipkart / eBay adapters (simulated submit)
    courier.py       booking, waybills, pickup slot
    voice.py         hi/en call scripts, call log
    pipeline.py      payouts, confirm(order), deliver(order)
    demand.py        weekly alert
  tests/             hermetic (fake C1); one parity test against C1's real Order
  logs/              shipments.jsonl, calls.jsonl (gitignored)
```

## API

| Endpoint | |
|---|---|
| `GET /api/orders` | Orders C2 can see (`source`: `c1_orders_jsonl` or `c2_sample`) |
| `GET /api/listings` | Catalogue ids |
| `POST /api/channels/{amazon\|flipkart\|ebay}/push/{listing_id}` | Simulated marketplace push |
| `POST /api/channels/{channel}/push-all` | Push the whole catalogue |
| `POST /api/orders/{id}/confirm` | Book courier, compute payouts, place confirmation and pickup calls |
| `POST /api/orders/{id}/deliver` | Simulate delivery and the payment-credited call |
| `GET /api/demand-alert` | Weekly alert |
| `POST /api/demo/reset` | Clear in-memory shipments and calls |

## Known gaps

- **Calls are in Hindi or English only.** An artisan who speaks neither is called
  in Hindi and `language_gaps` says so. A2 already has Tamil, Telugu and Kannada
  text; the scripts would follow.
- **Artisan phone numbers are made up.** C1's seed has none; B2 owns that record.
- **Delivery is a button.** Nothing tracks a parcel, so the payout call fires on demand.
- **State is in memory** (plus JSONL logs). Restarting C2 forgets bookings.
- **Only C1's `orders.jsonl` is read.** Once C1 runs with `CRAFTLY_ADAPTERS=http`,
  orders live in B2's database and this reader needs a B2 endpoint instead.
- **`uv.lock` is not committed**; `uv run` creates it on first use.
