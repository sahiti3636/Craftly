import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from app.asr import (
    AudioConversionError,
    AudioDurationError,
    UnsupportedAudioFormatError,
    transcribe,
)
from app.image import OUTPUT_DIR as PROCESSED_DIR
from app.image import thumbnail_path_for
from app.pipeline import (
    UnknownCorrectionFieldError,
    apply_corrections,
    build_draft,
    create_listing,
)
from app.schema import Listing
from app.store import get as get_draft
from app.store import save as save_draft
from app.tts import AUDIO_DIR, speak

IMAGES_DIR = Path(os.environ.get("CRAFTLY_IMAGES_DIR", "images"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="craftly",
    description="Artisan product cataloguing pipeline.",
    version="0.1.0",
)
app.mount("/audio", StaticFiles(directory=str(AUDIO_DIR)), name="audio")
app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")
app.mount("/processed", StaticFiles(directory=str(PROCESSED_DIR)), name="processed")


class ListingResponse(BaseModel):
    listing: Listing
    summary_audio_url: str | None = None


class ListingCreateResponse(BaseModel):
    listing: Listing
    summary_audio_url: str | None = None
    clean_thumb_url: str | None = None
    errors: list[str] = Field(default_factory=list)
    debug: dict[str, float | str] = Field(default_factory=dict)


class ConfirmRequest(BaseModel):
    listing_id: str
    confirmed: bool
    corrections: dict[str, object] = Field(default_factory=dict)


def _save_upload(upload: UploadFile, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "").suffix.lower()
    out_path = directory / f"{uuid4().hex}{suffix}"
    with out_path.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return out_path


def _synthesize_summary_audio(request: Request, text: str | None, language: str) -> str | None:
    if not text or not text.strip():
        return None
    audio_path = speak(text, language)
    return str(request.url_for("audio", path=audio_path.name))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/transcribe")
async def transcribe_endpoint(
    audio: UploadFile = File(...),
    language_hint: str | None = Form(None),
) -> dict:
    suffix = Path(audio.filename or "").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(audio.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        return transcribe(tmp_path, language_hint=language_hint)
    except UnsupportedAudioFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except AudioDurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AudioConversionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/listing/draft", response_model=ListingResponse)
async def draft_listing_endpoint(
    request: Request,
    audio: UploadFile = File(...),
    image: UploadFile = File(...),
    artisan_id: str = Form(...),
    language_hint: str | None = Form(None),
) -> ListingResponse:
    audio_suffix = Path(audio.filename or "").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=audio_suffix, delete=False) as tmp:
        shutil.copyfileobj(audio.file, tmp)
        audio_tmp_path = Path(tmp.name)

    try:
        image_path = _save_upload(image, IMAGES_DIR)
        image_url = str(request.url_for("images", path=image_path.name))

        listing, extracted_fields = build_draft(
            artisan_id=artisan_id,
            audio_path=audio_tmp_path,
            language_hint=language_hint,
            image_original_url=image_url,
        )
    except UnsupportedAudioFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except AudioDurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AudioConversionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        audio_tmp_path.unlink(missing_ok=True)

    audio_url = _synthesize_summary_audio(request, listing.summary_spoken, listing.source_language)
    save_draft(listing, extracted_fields, audio_url)

    return ListingResponse(listing=listing, summary_audio_url=audio_url)


@app.post("/listing/confirm", response_model=ListingResponse)
async def confirm_listing_endpoint(request: Request, body: ConfirmRequest) -> ListingResponse:
    record = get_draft(body.listing_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No draft listing found for id {body.listing_id!r}")

    try:
        updated_listing, updated_fields = apply_corrections(
            record.listing, record.extracted_fields, body.corrections, body.confirmed
        )
    except UnknownCorrectionFieldError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid correction value: {exc}") from exc

    if updated_listing.summary_spoken == record.listing.summary_spoken:
        # Nothing changed in the text that gets read aloud — reuse the
        # existing audio instead of paying for a redundant TTS call.
        audio_url = record.summary_audio_url
    else:
        audio_url = _synthesize_summary_audio(
            request, updated_listing.summary_spoken, updated_listing.source_language
        )

    save_draft(updated_listing, updated_fields, audio_url)

    return ListingResponse(listing=updated_listing, summary_audio_url=audio_url)


@app.post("/listing/create", response_model=ListingCreateResponse)
async def create_listing_endpoint(
    request: Request,
    image: UploadFile = File(...),
    audio: UploadFile = File(...),
    artisan_id: str = Form(...),
    language_hint: str | None = Form(None),
) -> ListingCreateResponse:
    """The end-to-end path: image cleanup + transcription in parallel,
    then numbers parsing + LLM extraction, then merge, then description
    generation, then TTS. See app.pipeline.create_listing for the stage
    order and the degrade-not-crash contract.

    Unlike /listing/draft, a single stage failing (ASR, image cleanup,
    LLM extraction, description generation, TTS) never turns into an
    HTTP error here — it's absorbed into `errors` on a 200 response, with
    per-stage timing under `debug`, so the caller always gets back
    whatever of the listing *did* succeed.
    """
    audio_suffix = Path(audio.filename or "").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=audio_suffix, delete=False) as tmp:
        shutil.copyfileobj(audio.file, tmp)
        audio_tmp_path = Path(tmp.name)

    try:
        image_path = _save_upload(image, IMAGES_DIR)
        image_url = str(request.url_for("images", path=image_path.name))

        result = await create_listing(
            artisan_id=artisan_id,
            image_path=image_path,
            audio_path=audio_tmp_path,
            image_original_url=image_url,
            language_hint=language_hint,
        )
    finally:
        audio_tmp_path.unlink(missing_ok=True)

    listing = result["listing"]

    clean_thumb_url = None
    if result["clean_path"]:
        clean_path = Path(result["clean_path"])
        listing.image_clean_url = str(request.url_for("processed", path=clean_path.name))
        thumb_path = thumbnail_path_for(clean_path)
        if thumb_path.exists():
            clean_thumb_url = str(request.url_for("processed", path=thumb_path.name))

    summary_audio_url = None
    if result["summary_audio_path"]:
        summary_audio_url = str(
            request.url_for("audio", path=Path(result["summary_audio_path"]).name)
        )

    save_draft(listing, result["extracted_fields"], summary_audio_url)

    return ListingCreateResponse(
        listing=listing,
        summary_audio_url=summary_audio_url,
        clean_thumb_url=clean_thumb_url,
        errors=result["errors"],
        debug=result["debug"],
    )


@app.get("/demo", response_class=HTMLResponse)
async def demo_page() -> str:
    return (STATIC_DIR / "demo.html").read_text(encoding="utf-8")
