# craftly

Artisan product cataloguing pipeline: FastAPI service, the `Listing`
schema contract, speech-to-text (ASR), deterministic money/duration
parsing, LLM-based semantic field extraction, description generation,
text-to-speech, product photo cleanup, and the end-to-end draft/confirm
listing flow.

## Stack

- Python 3.11
- [uv](https://docs.astral.sh/uv/) for dependency management
- FastAPI + Pydantic v2
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for ASR
- [Groq](https://groq.com) for semantic field extraction (category,
  material, colours, etc.) and description generation
- [gTTS](https://gtts.readthedocs.io/) for text-to-speech (dev backend —
  see "Text-to-speech" below)
- [rembg](https://github.com/danielgatis/rembg) (MIT) for background
  removal, running the **u2net** model (Apache 2.0) — see "Image cleanup"
  below for why the model choice matters and how it was verified
- `ffmpeg` (system dependency — install via `brew install ffmpeg` or your
  package manager; not managed by `uv`)

## Setup

```bash
uv sync
```

Copy `.env.example` to `.env` and set `GROQ_API_KEY` (get one at
[console.groq.com](https://console.groq.com)). `.env` is gitignored —
never commit it.

## Run

```bash
uv run uvicorn app.main:app --reload
```

Health check: `GET /health` → `{"status": "ok"}`

## Test

```bash
uv run pytest
```

## The Listing schema

Defined in [`app/schema.py`](app/schema.py). An example instance is in
[`schema/listing.example.json`](schema/listing.example.json).

### The core rule

**Every extracted field is nullable, and a missing value is `null` —
never a guessed default.** In particular `material_cost_inr` and
`hours_worked` must never be defaulted to `0`. A real `0` and a missing
value are different facts: `0` means the artisan told us there was no
material cost; `null` means we don't know and should ask. Any code that
writes a `Listing` must preserve that distinction, and any code that
reads one must treat `null` on any field below as "unknown, needs a
question to the artisan," not as a falsy/empty value to paper over.

### Field reference

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `listing_id` | `str` | No | Assigned when the record is created. Primary key. |
| `artisan_id` | `str` | No | Owner of the listing. |
| `source_language` | `str \| None` | Yes | BCP-47 code of the artisan's spoken input (e.g. `hi`, `ta`). Null if language detection hasn't run or failed. |
| `transcript_raw` | `str \| None` | Yes | Verbatim speech-to-text transcript, in `source_language`. Null until transcription has run. |
| `title_en` | `str \| None` | Yes | Generated title, English. |
| `title_hi` | `str \| None` | Yes | Generated title, Hindi. |
| `description_en` | `str \| None` | Yes | Generated description, English. |
| `description_hi` | `str \| None` | Yes | Generated description, Hindi. |
| `summary_spoken` | `str \| None` | Yes | Short plain-text readback of the listing, in `source_language`, meant to be read/played back to the artisan for confirmation. |
| `category` | `str \| None` | Yes | Top-level category, e.g. `"textiles"`, `"pottery"`. |
| `craft_type` | `str \| None` | Yes | Specific technique, e.g. `"block printing"`, `"wheel-thrown"`. |
| `material` | `str \| None` | Yes | Primary material, e.g. `"terracotta"`, `"cotton"`. |
| `colours` | `list[str] \| None` | Yes | Null = not extracted yet. `[]` is a valid *extracted* result meaning "no distinct colours identified" — the two are different states. |
| `dimensions` | `str \| None` | Yes | Free-text physical dimensions, e.g. `"12cm x 8cm x 5cm"`. |
| `material_cost_inr` | `int \| None` | Yes | Cost of materials in INR, as stated by the artisan. **Never 0 by default.** |
| `hours_worked` | `float \| None` | Yes | Hours spent making the item. **Never 0 by default.** |
| `confidence` | `dict[str, float]` | No (defaults to `{}`) | Per-field extraction confidence in `[0, 1]`, keyed by field name (e.g. `{"title_en": 0.94}`). A field absent from this map means extraction was never attempted for it, as distinct from an attempt that produced `null`. |
| `needs_confirmation` | `list[str]` | No (defaults to `[]`) | Names of `Listing` fields to ask the artisan to confirm before publishing — e.g. low confidence, or the field came back null. |
| `image_original_url` | `str \| None` | Yes | Artisan's original, unedited photo. Null until uploaded. |
| `image_clean_url` | `str \| None` | Yes | Cleaned-up (background-removed/enhanced) photo. Null until cleanup has run. |
| `created_at` | `datetime` | No (defaults to now, UTC) | When the record was created. |

### Field groups, for intuition

- **Identity/system** (never null): `listing_id`, `artisan_id`, `created_at`.
- **Raw input** (nullable — capture can fail or not have run yet):
  `source_language`, `transcript_raw`, `image_original_url`,
  `image_clean_url`.
- **Extracted** (nullable — extraction can fail, be skipped, or be
  uncertain): `title_en` through `hours_worked`.
- **Extraction metadata** (never null, but can be empty): `confidence`,
  `needs_confirmation`.

## ASR (speech-to-text)

[`app/asr.py`](app/asr.py) turns an audio file into a transcript. It does
**not** do any field extraction (title, category, cost, etc.) — it only
produces `Listing.transcript_raw` and related metadata.

`transcribe(audio_path, language_hint=None)` returns:

```json
{
  "text": "...",
  "detected_language": "hi",
  "confidence": 0.97,
  "duration_sec": 4.2
}
```

- Accepts `.m4a`, `.mp3`, `.wav`, `.ogg`; converts to 16kHz mono WAV via
  `ffmpeg` before decoding, so `ffmpeg` must be on `PATH`.
- Rejects clips under 1.5s or over 90s by raising `AudioDurationError`.
- Auto-detects language by default; pass `language_hint` (an ISO code,
  e.g. `"hi"`) to skip detection and force that language — in that case
  `confidence` is `1.0` since detection was skipped in favor of the
  caller's certainty.
- Model is [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
  size `"small"` by default, configurable via env vars:
  - `CRAFTLY_ASR_MODEL_SIZE` (default `small`)
  - `CRAFTLY_ASR_DEVICE` (default `cpu`)
  - `CRAFTLY_ASR_COMPUTE_TYPE` (default `int8`)

`POST /transcribe` wraps this as a multipart upload: form field `audio`
(the file) and optional form field `language_hint`. Returns 415 for an
unsupported format, 422 for a duration violation or ffmpeg failure.

### Trying it on a real recording

```bash
uv run python scripts/try_asr.py path/to/recording.m4a
uv run python scripts/try_asr.py path/to/recording.m4a --language hi
```

## Number parsing (money & duration)

[`app/numbers.py`](app/numbers.py) is a **deterministic, rule-based**
parser (no LLM, no network) that pulls money and duration mentions out of
a transcript. It feeds a minimum-wage floor calculation, so it is
deliberately conservative: **a number is only extracted when a
currency/duration marker is adjacent to it** — a bare number (a year, a
phone number, "hundred percent") is left alone rather than guessed at.

```python
from app.numbers import extract_money, extract_duration

extract_money("sava sau rupaye", "hi")
# [{"value_inr": 125, "span": (0, 15), "raw": "sava sau rupaye"}]

extract_duration("dedh ghante lage", "hi")
# [{"hours": 1.5, "unit": "hours", "assumed_hours_per_day": None,
#   "span": (0, 11), "raw": "dedh ghante"}]
```

- Handles Latin and Devanagari digits, spoken English numbers ("one
  hundred twenty"), the Indian-English digit-group elision pattern ("one
  twenty" → 120), Hindi scale words (sau/hazaar/lakh), and Hindi fraction
  words (sava, dedh, adhai, paune, saadhe).
- Days are converted to hours at 8 working hours/day; when that
  conversion happens, the result carries `"assumed_hours_per_day": 8` so
  callers can surface the assumption rather than treat it as measured.
- Code-switching is handled by always merging English word tables with
  the requested language's tables (see `app/lang/__init__.py`), since
  artisan speech mixes languages routinely.
- Per-language word tables live in `app/lang/<code>.py` as plain data
  dicts (`NUMBER_WORDS`, `SCALE_WORDS`, `FRACTION_PREFIXES`,
  `STANDALONE_FRACTIONS`, `CURRENCY_MARKERS`, `DURATION_UNITS`). Hindi and
  English are populated; `te`/`kn`/`ta` are empty stubs — adding a
  language is a data change, not a code change. Hindi's 21-99 word list is
  best-effort transliteration and should be spot-checked by a
  fluent/native speaker before relying on it beyond this prototype.

### A marker-reuse bug found while building the end-to-end path

Found via `create_listing`'s integration testing (see "The end-to-end
path" below), not by inspection — worth documenting since it's exactly
the class of silent-wrong-value bug this module exists to prevent.

`"paanch sau rupaye, teen ghante"` (500 rupees, 3 hours) was producing
**two** money matches: `500` (correct) and a spurious `3` from
`"rupaye, teen"`. Cause: `"rupaye"` legitimately qualifies `"paanch sau"`
as its marker-*after*; the scan then resumes right at `"rupaye"` itself,
and since `"rupaye"` also sits immediately before `"teen"`, it wrongly
qualified `"teen"` as a *second* marker-*before* match too — even though
`"teen"` is actually the start of an unrelated duration phrase (`"teen
ghante"`), not a second cost. A marker token was never marked "used," so
it could anchor two matches at once. Fixed by tracking consumed marker
indices in `_scan` (`app/numbers.py`) — a marker can now only ever
qualify the one match it was actually adjacent to. See
`test_money_marker_is_not_reused_for_an_adjacent_unrelated_number` in
`tests/test_numbers.py`.

## Semantic field extraction (LLM)

[`app/extract.py`](app/extract.py) extracts `category`, `craft_type`,
`material`, `colours`, `dimensions`, and `product_description_facts` from
a transcript via [Groq](https://groq.com). It deliberately does **not**
extract money or hours — that's `app/numbers.py`'s job exclusively.

```python
from app.extract import extract_fields

extract_fields("yeh mitti ka diya hai, laal rang", "hi")
# ExtractedFields(category=None, craft_type=None, material="clay",
#   colours=["red"], dimensions=None, product_description_facts=[],
#   material_cost_inr=None, hours_worked=None,
#   confidence={"material": 0.9, "colours": 0.85}, needs_confirmation=[])
```

- The system prompt lives in [`prompts/extract.txt`](prompts/extract.txt)
  (editable without touching code) and explicitly forbids inventing or
  inferring facts — e.g. never guess a colour from the material or
  category. That's backed by a validation guardrail, not just the prompt:
  the LLM's JSON is parsed into a schema that rejects unknown keys (so a
  hallucinated `material_cost_inr` fails validation) and rejects
  confidence values outside `[0, 1]`.
- Uses Groq's JSON mode (`response_format={"type": "json_object"}`).
  Model is configurable via `CRAFTLY_GROQ_MODEL` (default
  `openai/gpt-oss-120b`).
- Retries once on malformed/schema-invalid JSON; raises `ExtractionError`
  if it's still bad after the retry, or if the Groq request itself fails.
- Every attempt (system prompt, raw response, error if any) is logged as
  one JSON line to `logs/extract.jsonl` (configurable via
  `CRAFTLY_EXTRACT_LOG_PATH`) for debugging. This file is gitignored.
- Requires `GROQ_API_KEY` in the environment/`.env`; an empty transcript
  short-circuits to an all-null result without calling Groq at all.

## Merging parser + LLM output

[`app/merge.py`](app/merge.py) combines `app/numbers.py`'s money/duration
matches with `app/extract.py`'s LLM output into one `ExtractedFields`.
Rule: an *exact single* match for money (or duration) is trusted and used
directly; zero matches (nothing stated) or more than one (ambiguous —
which mention is the real cost?) both null the field and add it to
`needs_confirmation`, rather than guessing.

```python
from app.extract import extract_fields
from app.numbers import extract_money, extract_duration
from app.merge import merge_extraction

llm_result = extract_fields(transcript, language)
merged = merge_extraction(
    llm_result,
    extract_money(transcript, language),
    extract_duration(transcript, language),
)
```

## Generating titles, descriptions, and a spoken summary

[`app/describe.py`](app/describe.py) generates `title_en`, `title_hi`,
`description_en`, `description_hi`, and `summary_spoken` — from the
**structured `ExtractedFields` only, never the raw transcript**. Grounding
generation in fields that already passed extraction/merge means this step
can't reintroduce a detail that didn't survive those steps.

```python
from app.describe import generate_descriptions

result = generate_descriptions(merged, source_language)
# GeneratedDescriptions(title_en=..., title_hi=..., description_en=...,
#   description_hi=..., summary_spoken=...)
```

- `title_en`/`title_hi`: always under 70 characters.
- `description_en`/`description_hi`: always 60-90 words, e-commerce
  style, naturally mention material/craft_type for SEO. Never mention
  `material_cost_inr` or `hours_worked` — that's for the artisan, not the
  customer.
- `summary_spoken`: a short (~15 word) plain-text readback covering only
  product + material cost + hours, meant to be read aloud to the artisan
  for confirmation — no marketing language, no lists, minimal
  punctuation. For English/Hindi source language it's built with a
  **deterministic template** (see `_build_summary_en`/`_build_summary_hi`
  in `app/describe.py`) rather than an LLM call, so it cannot hallucinate;
  any other source language falls back to an LLM call using
  `prompts/describe_summary.txt`.
- Two runtime guardrails back the "no invented facts" prompt instructions
  in `prompts/describe_en.txt`/`describe_hi.txt`: a closed colour
  vocabulary check (a colour word in the output that isn't in
  `extracted_fields.colours` fails validation) and a cost/hours leak
  check (the literal cost or hours number appearing in customer-facing
  copy fails validation). Both trigger the same retry-once-then-fail
  pattern as `app/extract.py`, raising `DescriptionError` if still
  invalid after the retry.
- If `extracted_fields` has no usable facts at all, returns an all-null
  result without calling Groq.
- Every attempt logs to `logs/describe.jsonl` (gitignored), same
  convention as `app/extract.py`.
- Groq client setup (API key, default model) is shared between
  `app/extract.py` and `app/describe.py` via `app/groq_client.py`.

## Text-to-speech

[`app/tts.py`](app/tts.py) reads `summary_spoken` back to the artisan.
`speak(text, language) -> Path` returns a local audio file path.

```python
from app.tts import speak

audio_path = speak("aapka material cost 500 rupaye hai", "hi")
```

- Backend is **pluggable**: `speak()` always goes through whatever
  `TTSBackend` is configured (`CRAFTLY_TTS_BACKEND`, default `gtts`), so
  swapping in an on-device Android TTS engine or an AI4Bharat Indic-TTS
  model is a matter of implementing one `TTSBackend` subclass and
  flipping the env var — no caller of `speak()` changes.
- `GTTSBackend` (the only working implementation today) uses
  [gTTS](https://gtts.readthedocs.io/) — a dev-only backend, since it
  calls Google Translate's TTS over the network. `AndroidTTSBackend` and
  `AI4BharatTTSBackend` are placeholders (raise `NotImplementedError`)
  that make the intended shape of a real implementation visible, same
  pattern as the empty `app/lang/te.py`-style stubs elsewhere.
- Audio files are written to `CRAFTLY_AUDIO_DIR` (default `audio_files/`,
  gitignored).

## Image cleanup

[`app/image.py`](app/image.py) turns a raw product photo into a clean,
consistent listing image. Runs entirely offline — no paid APIs — and
only one step (background removal) is ML; everything else is plain
OpenCV/Pillow.

```python
from app.image import clean_product_image

result = clean_product_image("raw_photo.jpg")
# {"clean_path": "processed_images/raw_photo_clean.jpg",
#  "alpha_path": "processed_images/raw_photo_alpha.png",
#  "warnings": []}
```

Pipeline, in this exact order:

1. **Blur check** — variance of Laplacian on a histogram-equalized copy
   of the image (not the raw image — see below). Below
   `CRAFTLY_BLUR_THRESHOLD` (default `500.0`), returns
   `{"clean_path": None, "alpha_path": None, "warnings": ["retake"]}`
   and stops — no background removal, no files written.
2. **Background removal** via [rembg](https://github.com/danielgatis/rembg)
   — see "Model licence" below.
3. **Composite onto pure white**, alpha-blended so soft/antialiased edges
   survive instead of getting hard-cut.
4. **Crop to the alpha bounding box + 8% padding, square.** If the
   source photo has little or no margin around the subject (a tight
   crop), the canvas is extended with white rather than losing padding —
   the output is always properly padded, never just a raw slice of the
   original frame.
5. **Gray-world white balance** (foreground-masked, damped — see below),
   **then subtle CLAHE** on the L channel in LAB (chroma untouched, so it
   can't shift hue/saturation — only local contrast, and mildly, via a
   conservative default `CRAFTLY_CLAHE_CLIP_LIMIT`). These are textiles
   and handicrafts: colour accuracy matters more than punch.
6. **Output**: a 2000×2000 JPEG at quality 90 (`clean_path`), plus a
   600×600 thumbnail next to it (find it via
   `thumbnail_path_for(clean_path)` — not a separate return key, since
   the contract is exactly `{clean_path, alpha_path, warnings}`).

### Two bugs this module's own testing caught

Both are documented in code where they were fixed, but are worth
surfacing here since they'd otherwise look like reasonable
implementations that happen to be subtly wrong:

- **Naive blur detection flags dark-but-sharp photos as blurry.**
  Variance of Laplacian scales with pixel amplitude (Laplacian is a
  linear filter), so a photo that's simply underexposed — not out of
  focus — scores far lower than the same shot at normal exposure, purely
  from reduced contrast. A synthetic sharp-but-dark test image scored 20
  on the raw metric (below any sane threshold — a false "retake") vs.
  2713 after histogram-equalizing first; a genuinely blurred image
  scored 282 equalized, still clearly separated. `_blur_score` equalizes
  before measuring for exactly this reason.
- **Naive gray-world white balance tints a pure-white background.**
  Gray-world assumes the average colour of what it measures is neutral
  gray — true for a whole natural scene, false for a product crop
  dominated by one saturated colour (e.g. a red pot filling most of the
  frame). Measuring the whole padded/cropped image let a strongly
  coloured product drag the correction so far off that the manufactured
  white padding visibly tinted cyan. Fixed by (a) measuring gray-world
  statistics from only the alpha-masked foreground, (b) damping the
  correction to `CRAFTLY_WHITE_BALANCE_STRENGTH` (default `0.6`, i.e.
  60% of the full correction — a strongly-coloured product shouldn't get
  partially desaturated trying to drag its own average toward gray
  either), and (c) re-compositing onto pure white afterwards regardless,
  so the background is guaranteed white no matter what the correction
  computed. See `_gray_world_white_balance`'s docstring.

Both were found by actually running the pipeline on test images and
looking at the output — see "Batch testing" below.

### Model licence — read before changing `CRAFTLY_REMBG_MODEL`

This matters here specifically because this pipeline is meant for a
government deployment, so an accidental paid-license dependency isn't
just a cost surprise — it can be a procurement/compliance problem.

**rembg's own default model is not permissively licensed.** Inspecting
`rembg.new_session`'s actual signature (not just its docs) shows its
default `model_name` is `"bria-rmbg"` (RMBG-2.0), which the rembg README
states plainly requires a **paid commercial agreement**:

> "RMBG-2.0 is released under a BRIA license that requires a paid
> agreement for commercial use." / "Model weights carry their own
> licenses, independent of rembg's MIT license — check the linked source
> before using any model commercially."

`app/image.py` never relies on that default. `CRAFTLY_REMBG_MODEL`
defaults to **`u2net`** instead: its source repo,
[xuebinqin/U-2-Net](https://github.com/xuebinqin/U-2-Net), is licensed
[Apache License 2.0](https://github.com/xuebinqin/U-2-Net/blob/master/LICENSE)
— permissive, no payment or separate commercial agreement required.
`rembg` itself (the library, separate from any model) is MIT licensed.

**One residual caveat, worth a legal read before an actual government
deployment**: Apache 2.0's definition of "Work" covers "Source or Object
form," and "Object form" is defined to include things like "compiled
object code" and "conversions to other media types" — language that
plausibly extends to distributed trained-weight files, but whose
explicit examples are conventional software artifacts, not ML models
specifically. There's an open, unanswered upstream GitHub issue
([#208](https://github.com/xuebinqin/U-2-Net/issues/208)) asking the
maintainer to confirm the weights carry the same license as the code —
it has sat without a maintainer response. No other model in rembg's
lineup was found to carry a comparable licensing flag to bria-rmbg's
(i.e. nothing else identified was flagged as requiring payment), which
is one reason u2net was chosen over a newer/higher-quality option
(`birefnet-*`, `isnet-general-use`, etc.) without doing the same
diligence on each. If you change `CRAFTLY_REMBG_MODEL`, check that
model's license the same way before deploying.

- `CRAFTLY_REMBG_MODEL` — background-removal model (default `u2net`).
- `CRAFTLY_BLUR_THRESHOLD` — retake cutoff on the equalized blur score
  (default `500.0`; tune against real camera photos, this is a starting
  point, not a calibrated final value).
- `CRAFTLY_WHITE_BALANCE_STRENGTH` — 0–1 blend toward full gray-world
  correction (default `0.6`).
- `CRAFTLY_CLAHE_CLIP_LIMIT` — CLAHE aggressiveness (default `1.5`, kept
  low on purpose).
- `CRAFTLY_PROCESSED_IMAGES_DIR` — output directory (default
  `processed_images/`, gitignored).

### Batch testing (contact sheet)

[`scripts/try_image.py`](scripts/try_image.py) runs the pipeline over a
folder of photos and writes a before/after contact sheet PNG, so quality
can be eyeballed quickly instead of opening every output file by hand:

```bash
uv run python scripts/try_image.py tests/fixtures/bad_photos
```

[`tests/fixtures/bad_photos/`](tests/fixtures/bad_photos) has 5
deliberately bad synthetic test photos (generated by
[`scripts/generate_bad_test_images.py`](scripts/generate_bad_test_images.py),
re-run it to regenerate them): `dark.jpg` (underexposed), `cluttered.jpg`
(busy multi-object background), `blurry.jpg` (should trigger "retake"),
`off_white.jpg` (cream/beige background, not true white), and
`tight_crop.jpg` (subject fills nearly the whole frame, stress-testing
the padding logic).

## The draft/confirm listing flow

[`app/pipeline.py`](app/pipeline.py) orchestrates the full pipeline —
ASR → number parsing → LLM extraction → merge → description generation
— and applies artisan corrections. [`app/store.py`](app/store.py) is an
in-memory (dev-only) store keyed by `listing_id`, holding both the public
`Listing` and the underlying `ExtractedFields` (which carries a bit more
detail, e.g. `product_description_facts`, that regeneration after a
correction needs but the `Listing` contract has no field for).

### `POST /listing/draft`

Multipart form: `audio` (file), `image` (file), `artisan_id` (string),
optional `language_hint`. Runs the full pipeline and returns:

```json
{
  "listing": { "...": "a full Listing, see schema/listing.example.json" },
  "summary_audio_url": "http://.../audio/<id>.mp3"
}
```

Any field the numbers parser couldn't resolve to exactly one value (zero
matches, or more than one ambiguous match) comes back `null` and is
listed in `listing.needs_confirmation` — see "Merging parser + LLM
output" above. **Any such field is spoken back in `summary_spoken` as a
question, never stated as fact** (e.g. `"What was the material cost?"`,
not a guessed number) — see `generate_spoken_summary` in
`app/describe.py`.

### `POST /listing/confirm`

JSON body:

```json
{
  "listing_id": "lst_...",
  "confirmed": true,
  "corrections": { "material_cost_inr": 500 }
}
```

`corrections` may only target `category`, `craft_type`, `material`,
`colours`, `dimensions`, `material_cost_inr`, `hours_worked` (400 for any
other key, or for a wrong-typed value). Regeneration rule:

- Correcting a **semantic** field (category/craft_type/material/colours/
  dimensions) re-runs full description generation — `title_en/hi`,
  `description_en/hi`, and `summary_spoken`.
- Correcting only a **numeric** field (material_cost_inr/hours_worked)
  re-runs nothing but `summary_spoken` (the only generated text that ever
  mentions cost/hours — title/description never do, so there's nothing
  else to regenerate). This is cheap: `generate_spoken_summary` for
  English/Hindi is a deterministic template, no LLM call.
- No corrections at all regenerates nothing.
- `confirmed: true` clears every remaining `needs_confirmation` entry,
  corrected or not (the artisan has signed off on the listing as it now
  stands); `confirmed: false` leaves whatever wasn't corrected still
  flagged, for a later round.

Returns 404 for an unknown `listing_id`.

## The end-to-end path

`POST /listing/create` is the single call that runs the whole pipeline
on one image + one audio recording and hands back a complete `Listing`
draft. [`app/pipeline.py`](app/pipeline.py)'s `create_listing` is the
orchestration; `app/main.py`'s endpoint just does request/response
plumbing around it.

```
image cleanup ─┐
                ├─▶ merge ─▶ description generation ─▶ TTS of summary_spoken
transcription ─┘        ▲
numbers parsing ─┐       │
                 ├───────┘
LLM extraction ──┘
```

Stage order:

1. **Image cleanup** (`app/image.py`) and **transcription** (`app/asr.py`)
   run **concurrently** — neither depends on the other. Implemented with
   `asyncio.gather` over `asyncio.to_thread`: both are actually
   CPU/subprocess-bound (OpenCV+ONNX, ffmpeg+ctranslate2), and that work
   happens in native code that releases the GIL, so this is genuine
   parallelism, not just async bookkeeping. Confirmed in
   `tests/test_create_listing.py` by timing two artificially-slowed
   mocked stages and asserting the wall clock is close to the slower
   one, not their sum — and empirically in a real (unmocked) run: image
   cleanup took 2.17s and transcription 4.55s, total request time 5.3s,
   not 6.72s.
2. **Numbers parsing** (`app/numbers.py`) and **LLM field extraction**
   (`app/extract.py`) — also concurrent; both only need the transcript
   from stage 1.
3. **Merge** (`app/merge.py`).
4. **Description generation** (`app/describe.py`).
5. **TTS** of `summary_spoken` (`app/tts.py`) — skipped (not an error)
   if there's no summary to read.

### Degrade, don't crash

Every stage is individually timed and individually fault-tolerant: if a
stage's function raises, that stage's contribution degrades to its
safest empty/null value, a short stable `<stage>_failed` code is added
to the response's `errors` list, and the exception's detail goes to
`debug` — but every *other* stage still runs normally. For example, if
transcription fails: the image is still cleaned, numbers parsing and LLM
extraction still run on an empty transcript (which itself degrades to
all-null fields, per `extract_fields`'s own documented behaviour — see
"Semantic field extraction" above), and description generation still
produces whatever `summary_spoken` that implies (e.g. still asking
pending-confirmation questions if `merge.py` flagged any) — the whole
request never becomes a 4xx/5xx just because one stage had a bad day.

`app/image.py`'s own `"retake"` warning (a blurry photo — not an
exception, `clean_product_image`'s own designed stop-early case) is
surfaced the same way, reusing its existing vocabulary rather than
inventing a parallel code for the same thing.

Response shape:

```json
{
  "listing": { "...": "a full Listing" },
  "summary_audio_url": "http://.../audio/<id>.mp3",
  "clean_thumb_url": "http://.../processed/<id>_clean_thumb.jpg",
  "errors": ["transcription_failed"],
  "debug": {
    "image_cleanup_sec": 2.17, "transcription_sec": 4.55,
    "numbers_parsing_sec": 0.0002, "llm_extraction_sec": 0.5,
    "merge_sec": 0.0001, "description_generation_sec": 1.2,
    "tts_sec": 0.75, "total_sec": 5.3,
    "transcription_error": "RuntimeError: ffmpeg exploded"
  }
}
```

`errors` carries short stable codes an app can key off (build a friendly
message per code); `debug` carries per-stage timing plus a
`<stage>_error` detail string for whichever stages failed — meant for
developers, not end users.

## `/demo` — one-screen manual test page

`GET /demo` serves [`static/demo.html`](static/demo.html): upload a
photo and an audio file, submit, and see the cleaned photo, transcript,
extracted fields (with `needs_confirmation` ones visibly flagged),
generated titles/descriptions, a native `<audio controls>` player for
the spoken summary, the `errors` list, and the full `debug` timing
breakdown — all on one screen, calling `POST /listing/create` directly
via `fetch()`.

Deliberately plain (no build step, no framework, inline CSS/JS in one
file) — this is a tool for the team to eyeball real pipeline output
quickly during development, not a customer-facing page, so obvious and
fast mattered more than polished.

## Project layout

```
app/
  main.py       FastAPI app: health, /transcribe, /listing/draft, /listing/confirm,
                /listing/create, /demo
  schema.py     The Listing contract (Pydantic model)
  asr.py        Speech-to-text (transcript only, no field extraction)
  numbers.py    Deterministic money/duration parsing from transcript text
  lang/         Per-language word tables (en, hi, te, kn, ta) + merge logic
  extract.py    LLM semantic field extraction via Groq (category/material/etc.)
  merge.py      Combines numbers.py + extract.py output into one ExtractedFields
  describe.py   Generates title/description (en+hi) + spoken summary, fields only
  groq_client.py  Shared Groq client/model config for extract.py + describe.py
  tts.py        Pluggable text-to-speech (gTTS dev backend; Android/AI4Bharat stubs)
  image.py      Product photo cleanup: blur check, background removal, crop, colour
  pipeline.py   Orchestrates ASR->numbers->extract->merge->describe; applies corrections;
                create_listing() is the parallel, degrade-not-crash end-to-end path
  store.py      In-memory (dev-only) draft-listing store, keyed by listing_id
static/
  demo.html     GET /demo — one-screen manual test page for POST /listing/create
prompts/
  extract.txt           System prompt for app/extract.py
  describe_en.txt       System prompt for English title/description
  describe_hi.txt       System prompt for Hindi title/description
  describe_summary.txt  System prompt for spoken summary in non-en/hi languages
  (all editable without touching code)
scripts/
  try_asr.py                   CLI to run asr.transcribe() against a real audio file
  try_image.py                 Batch-runs image cleanup over a folder, writes a contact sheet
  generate_bad_test_images.py  (Re)generates tests/fixtures/bad_photos/
tests/
  test_main.py                   health + /transcribe endpoint tests
  test_schema.py                 schema nullability + contract tests
  test_asr.py                    ASR tests against synthetic audio clips
  test_numbers.py                money/duration parsing tests, incl. adversarial cases
  test_extract.py                extract_fields tests (mocked Groq calls, retry/logging)
  test_merge.py                  merge rule tests, driven by tests/fixtures/transcripts.json
  test_describe.py               generate_descriptions tests, incl. no-invented-facts guardrails
  test_tts.py                    speak() tests, incl. backend pluggability
  test_pipeline.py               build_draft/apply_corrections unit tests
  test_listing_flow.py           integration test: draft w/ ambiguous number -> confirm -> complete
  test_image.py                  image cleanup tests, real rembg pipeline + mocked-boundary tests
  test_create_listing.py         create_listing() unit tests: concurrency timing + degrade-per-stage
  test_create_listing_endpoint.py  POST /listing/create + GET /demo HTTP-level tests
  fixtures/
    transcripts.json   15 realistic transcripts incl. code-switched/missing-info
    bad_photos/        5 deliberately-bad synthetic product photos (see "Image cleanup")
schema/
  listing.example.json   one filled example Listing
```
