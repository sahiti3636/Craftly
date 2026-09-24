"""The bridge to B2 (`platform/`): where a confirmed listing goes live.

Until this existed a confirmed listing lived only in `app/store.py`'s
in-memory dict and never reached the shop. Now, once the artisan has
heard the readback and confirmed it, `publish()` hands it to B2:

1. the photos go to `POST /media` (content-addressed, so a retry from
   A1's offline queue re-sends nothing new),
2. the listing goes to `POST /listings` with `publish: true`, which is
   the moment B2 mints its craft passport and QR code.

B2 owns who the artisan is. Studio signs her in with a one-time code
(`request_code` / `verify_code`, proxied through this service so the app
stays same-origin), and the token it gets back is what `publish()` sends.
B2 files the listing under that token's artisan, whatever id the draft
carries.

`CRAFTLY_PLATFORM_URL` says where B2 is. B2 runs on 8200; this service
is on 8000.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.schema import Listing

#: 127.0.0.1 rather than localhost: on Windows "localhost" tries IPv6 first
#: and each call waits ~2s for that to fail.
PLATFORM_URL = os.environ.get("CRAFTLY_PLATFORM_URL", "http://127.0.0.1:8200").rstrip("/")
#: C1 (the shop). It serves every listing photo — the seed catalogue's and
#: the ones artisans published — at /media/<name>, so Studio shows her
#: listings with the photos buyers see.
SHOP_URL = os.environ.get("CRAFTLY_SHOP_URL", "http://127.0.0.1:8100").rstrip("/")
TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class PlatformError(RuntimeError):
    """B2 refused or could not be reached. `status` is B2's HTTP status,
    or 503 when B2 did not answer at all."""

    def __init__(self, message: str, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


def _call(method: str, path: str, *, token: str | None = None, **kwargs) -> dict | list:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.request(method, f"{PLATFORM_URL}{path}", headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise PlatformError(f"The Craftly platform is not reachable ({exc}).") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise PlatformError(str(detail or f"The platform answered {response.status_code}."), response.status_code)
    return response.json()


def request_code(phone: str, name: str | None, language: str) -> dict:
    return _call("POST", "/auth/artisan/request-otp", json={"phone": phone, "name": name, "language": language})


def verify_code(challenge_id: str, code: str) -> dict:
    return _call("POST", "/auth/artisan/verify", json={"challenge_id": challenge_id, "code": code})


def me(token: str) -> dict:
    """Who is signed in: her name as B2 has it, language and consent."""
    return _call("GET", "/auth/me", token=token)


def update_me(token: str, fields: dict) -> dict:
    """Her language and AI-call consent, saved on her B2 account (C2 reads
    the consent before placing any call)."""
    return _call("PATCH", "/auth/me", token=token, json=fields)


def my_listings(token: str) -> list:
    """Her listings as B2 holds them, drafts included, with passport codes."""
    return _call("GET", "/listings/mine", token=token)


def shop_media(name: str) -> tuple[bytes, str] | None:
    try:
        response = httpx.get(f"{SHOP_URL}/media/{name}", timeout=TIMEOUT)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    return response.content, response.headers.get("content-type", "application/octet-stream")


def artisan_orders(token: str) -> list:
    return _call("GET", "/orders/mine/artisan", token=token)


def set_order_status(token: str, order_id: str, status: str) -> dict:
    return _call("POST", f"/orders/{order_id}/status", token=token, json={"status": status})


def _local_file(url: str | None, served: dict[str, Path]) -> Path | None:
    """The file on disk behind one of this service's own image URLs.

    Only `/images/...` and `/processed/...` are ours; anything else is
    left alone rather than fetched.
    """
    if not url:
        return None
    parts = Path(urlparse(url).path).parts  # ("/", "processed", "abc.png")
    if len(parts) != 3 or parts[1] not in served:
        return None
    path = served[parts[1]] / parts[2]
    return path if path.is_file() else None


def _upload(path: Path, token: str) -> str:
    with path.open("rb") as fh:
        stored = _call("POST", "/media", token=token, files={"file": (path.name, fh)})
    return stored["url"]


def publish(listing: Listing, token: str, served: dict[str, Path]) -> dict:
    """Send a confirmed listing to B2 and put it live.

    `served` maps this service's static mounts to their directories
    ({"images": ..., "processed": ...}) so the photos can be uploaded from
    disk. A photo that cannot be found locally is dropped from what B2
    stores rather than published as a link to this service, which a buyer
    on the shop could not load.
    """
    payload = listing.model_dump(mode="json")
    for field in ("image_clean_url", "image_original_url"):
        local = _local_file(payload.get(field), served)
        payload[field] = _upload(local, token) if local else None
    return _call("POST", "/listings", token=token, json={"listing": payload, "publish": True})
