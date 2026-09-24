"""The artisan-side write path: listings in, photos in.

This is what replaces `capture/app/store.py`, so the tests are mostly
about the two things an in-memory dict never had to survive: a retry from
a phone with bad signal, and a second artisan.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from app.models import Listing


def _png(colour: str = "red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), colour).save(buffer, format="PNG")
    return buffer.getvalue()


# -- listings ---------------------------------------------------------------


def test_a_listing_survives_the_process(client, artisan_headers, session):
    client.post(
        "/listings",
        json={
            "listing": {
                "listing_id": "lst_persist",
                "artisan_id": "x",
                "title_en": "A pot",
                "material_cost_inr": 50,
                "hours_worked": 3.0,
            },
            "publish": True,
        },
        headers=artisan_headers,
    )
    # Straight out of the database, not out of the response.
    row = session.get(Listing, "lst_persist")
    assert row.title_en == "A pot"
    assert row.material_cost_inr == 50
    assert row.hours_worked == 3.0


def test_the_owner_comes_from_the_token_not_the_payload(client, artisan_headers, artisan_id, session):
    """A client-supplied owner id is how one artisan's work gets filed
    under another's name."""
    client.post(
        "/listings",
        json={
            "listing": {
                "listing_id": "lst_owner",
                "artisan_id": "art_somebody_else",
                "title_en": "Mine",
            }
        },
        headers=artisan_headers,
    )
    assert session.get(Listing, "lst_owner").artisan_id == artisan_id


def test_a_retried_sync_does_not_create_a_second_product(client, artisan_headers, session):
    """A1's offline queue retries a sync it is not sure completed."""
    body = {"listing": {"listing_id": "lst_retry", "artisan_id": "x", "title_en": "Once"}}
    first = client.post("/listings", json=body, headers=artisan_headers)
    second = client.post("/listings", json=body, headers=artisan_headers)
    assert first.status_code == second.status_code == 201
    assert session.query(Listing).filter(Listing.listing_id == "lst_retry").count() == 1


def test_nulls_are_stored_as_nulls(client, artisan_headers, session):
    """The rule this whole repo turns on. A 0 here is a wage floor of zero."""
    client.post(
        "/listings",
        json={
            "listing": {
                "listing_id": "lst_nulls",
                "artisan_id": "x",
                "title_en": "Unknown cost",
            }
        },
        headers=artisan_headers,
    )
    row = session.get(Listing, "lst_nulls")
    assert row.material_cost_inr is None
    assert row.hours_worked is None


def test_an_explicit_null_correction_is_applied(client, artisan_headers, session):
    """She said three hours, then said she was not sure.

    `exclude_unset` rather than `exclude_none`, so sending an explicit null
    clears the value. Treating null as "no change" would make the one
    correction that protects the wage floor impossible to make.
    """
    client.post(
        "/listings",
        json={
            "listing": {
                "listing_id": "lst_fix",
                "artisan_id": "x",
                "title_en": "Fix me",
                "hours_worked": 3.0,
            }
        },
        headers=artisan_headers,
    )
    client.patch("/listings/lst_fix", json={"hours_worked": None}, headers=artisan_headers)
    session.expire_all()
    assert session.get(Listing, "lst_fix").hours_worked is None


def test_a_listing_starts_as_a_draft(client, artisan_headers):
    created = client.post(
        "/listings",
        json={"listing": {"listing_id": "lst_draft2", "artisan_id": "x", "title_en": "Draft"}},
        headers=artisan_headers,
    ).json()
    assert created["published"] is False


def test_an_artisan_cannot_edit_someone_elses_listing(client, artisan_headers, seeded):
    other = client.post("/auth/artisan/request-otp", json={"phone": "9111111111"}).json()
    token = client.post(
        "/auth/artisan/verify",
        json={"challenge_id": other["challenge_id"], "code": other["dev_code"]},
    ).json()["access_token"]

    client.post(
        "/listings",
        json={"listing": {"listing_id": "lst_hers", "artisan_id": "x", "title_en": "Hers"}},
        headers=artisan_headers,
    )
    response = client.patch(
        "/listings/lst_hers",
        json={"title_en": "Mine now"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_partial_inventory_update_leaves_the_rest_alone(client, artisan_headers, session):
    """Resetting monthly_capacity to null would make a cluster look
    incapable of a bulk order it can easily fill."""
    client.post(
        "/listings",
        json={
            "listing": {"listing_id": "lst_inv", "artisan_id": "x", "title_en": "Inv"},
            "inventory": {"in_stock": 5, "monthly_capacity": 300, "lead_time_days": 7},
            "publish": True,
        },
        headers=artisan_headers,
    )
    client.patch(
        "/listings/lst_inv", json={"inventory": {"in_stock": 2}}, headers=artisan_headers
    )
    inventory = client.get("/catalog/entries/lst_inv").json()["inventory"]
    assert inventory["in_stock"] == 2
    assert inventory["monthly_capacity"] == 300
    assert inventory["lead_time_days"] == 7


def test_mine_shows_drafts_and_nobody_elses(client, artisan_headers, artisan_id):
    client.post(
        "/listings",
        json={"listing": {"listing_id": "lst_m1", "artisan_id": "x", "title_en": "One"}},
        headers=artisan_headers,
    )
    mine = client.get("/listings/mine", headers=artisan_headers).json()
    # The demo artisan owns seeded pieces too; the draft is among them, and
    # nothing belongs to anyone else.
    assert "lst_m1" in [entry["listing"]["listing_id"] for entry in mine]
    assert {entry["listing"]["artisan_id"] for entry in mine} == {artisan_id}


def test_writing_a_listing_needs_a_token(client):
    response = client.post(
        "/listings", json={"listing": {"listing_id": "lst_x", "artisan_id": "x"}}
    )
    assert response.status_code == 401


# -- media ------------------------------------------------------------------


def test_an_upload_comes_back_with_a_url_that_serves(client, artisan_headers):
    uploaded = client.post(
        "/media", files={"file": ("photo.png", _png(), "image/png")}, headers=artisan_headers
    )
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["kind"] == "image"
    assert body["sha256"] == hashlib.sha256(_png()).hexdigest()

    served = client.get(body["url"])
    assert served.status_code == 200
    assert served.content == _png()


def test_the_same_photo_uploaded_twice_is_stored_once(client, artisan_headers):
    """A phone on a patchy connection retries. It must not cost her data
    twice or put two copies of one photograph on the disk."""
    first = client.post(
        "/media", files={"file": ("a.png", _png(), "image/png")}, headers=artisan_headers
    ).json()
    second = client.post(
        "/media", files={"file": ("b.png", _png(), "image/png")}, headers=artisan_headers
    ).json()
    assert first["url"] == second["url"]
    assert second["deduplicated"] is True


def test_the_declared_content_type_is_not_trusted(client, artisan_headers):
    """A broken client posting an HTML error page as a JPEG must not end up
    rendered as a product photo for the rest of the demo."""
    response = client.post(
        "/media",
        files={"file": ("photo.jpg", b"<html>error</html>", "image/jpeg")},
        headers=artisan_headers,
    )
    assert response.status_code == 415


def test_an_empty_file_is_refused(client, artisan_headers):
    response = client.post(
        "/media", files={"file": ("empty.png", b"", "image/png")}, headers=artisan_headers
    )
    assert response.status_code == 415


def test_an_oversized_file_is_refused(client, artisan_headers, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    response = client.post(
        "/media", files={"file": ("big.png", _png(), "image/png")}, headers=artisan_headers
    )
    assert response.status_code == 415


@pytest.mark.parametrize(
    "filename", ["../../secret", "not-a-hash.png", "..%2f..%2fetc", "a" * 64 + ".png"]
)
def test_a_filename_that_is_not_a_content_hash_serves_nothing(client, filename):
    """The served name *is* the digest, so traversal never becomes a path."""
    assert client.get(f"/media/{filename}").status_code == 404


def test_uploading_needs_a_token(client):
    assert client.post("/media", files={"file": ("a.png", _png(), "image/png")}).status_code == 401


def test_serving_does_not(client, artisan_headers):
    """A passport image is scanned by a stranger at a mela."""
    url = client.post(
        "/media", files={"file": ("a.png", _png(), "image/png")}, headers=artisan_headers
    ).json()["url"]
    assert client.get(url).status_code == 200


def test_uploading_against_someone_elses_listing_is_refused(
    client, artisan_headers, artisan_id, seeded, session
):
    not_hers = (
        session.query(Listing).filter(Listing.artisan_id != artisan_id).first()
    )
    assert not_hers is not None, "the seed catalogue should have more than one maker"

    response = client.post(
        "/media",
        files={"file": ("a.png", _png(), "image/png")},
        data={"listing_id": not_hers.listing_id},
        headers=artisan_headers,
    )
    assert response.status_code == 404
