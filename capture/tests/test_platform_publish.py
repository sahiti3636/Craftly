"""Publishing a confirmed listing to B2, and Studio's other B2 routes.

B2 is faked at `platform_client._call`, the one place this service talks
to it; `platform/tests` covers B2's side.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main, platform_client
from app.extract import ExtractedFields
from app.main import app
from app.schema import Listing
from app.store import save as save_draft

client = TestClient(app)
AUTH = {"Authorization": "Bearer tok_artisan"}


@pytest.fixture
def b2(monkeypatch, tmp_path):
    """Records every call; answers like B2 would."""
    calls = []

    def fake_call(method, path, *, token=None, **kwargs):
        calls.append((method, path, token, kwargs))
        if path == "/media":
            return {"url": f"/media/{len(calls):064x}.jpg"}
        if path == "/listings":
            body = kwargs["json"]
            return {"listing": body["listing"], "published": body["publish"], "verification_code": "CR-TEST-CODE",
                    "passport_url": "http://shop/p/CR-TEST-CODE"}
        if path == "/auth/artisan/request-otp":
            return {"challenge_id": "otp_1", "dev_code": "123456", "delivery": "echo"}
        raise platform_client.PlatformError("That code did not match.", 401)

    monkeypatch.setattr(platform_client, "_call", fake_call)
    monkeypatch.setattr(main, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(main, "IMAGES_DIR", tmp_path / "images")
    (tmp_path / "processed").mkdir()
    (tmp_path / "images").mkdir()
    (tmp_path / "processed" / "clean.png").write_bytes(b"\x89PNG fake")
    (tmp_path / "images" / "orig.jpg").write_bytes(b"\xff\xd8 fake")
    return calls


def _draft(listing_id="lst_pub_1", needs=()):
    listing = Listing(
        listing_id=listing_id,
        artisan_id="artisan_phone_local",
        title_en="Terracotta Vase",
        material_cost_inr=200,
        hours_worked=5,
        image_clean_url="http://testserver/processed/clean.png",
        image_original_url="http://testserver/images/orig.jpg",
        needs_confirmation=list(needs),
    )
    save_draft(listing, ExtractedFields(material_cost_inr=200, hours_worked=5))
    return listing


def test_publishing_needs_a_signed_in_artisan(b2):
    _draft()
    assert client.post("/listing/publish", json={"listing_id": "lst_pub_1"}).status_code == 401
    assert b2 == []


def test_an_unknown_draft_is_a_404(b2):
    assert client.post("/listing/publish", json={"listing_id": "lst_nope"}, headers=AUTH).status_code == 404


def test_an_unconfirmed_listing_does_not_go_live(b2):
    _draft("lst_pub_2", needs=["material_cost_inr"])
    response = client.post("/listing/publish", json={"listing_id": "lst_pub_2"}, headers=AUTH)
    assert response.status_code == 409
    assert "material_cost_inr" in response.json()["detail"]
    assert b2 == []


def test_photos_go_to_b2_then_the_listing_goes_live(b2):
    _draft()
    response = client.post("/listing/publish", json={"listing_id": "lst_pub_1"}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["verification_code"] == "CR-TEST-CODE"

    paths = [c[1] for c in b2]
    assert paths == ["/media", "/media", "/listings"]
    assert all(c[2] == "tok_artisan" for c in b2)
    sent = b2[-1][3]["json"]
    assert sent["publish"] is True
    # The shop gets B2's media URLs, never a link back to this service.
    assert sent["listing"]["image_clean_url"].startswith("/media/")
    assert sent["listing"]["image_original_url"].startswith("/media/")
    # The null rule survives the trip: numbers go through untouched.
    assert sent["listing"]["material_cost_inr"] == 200


def test_a_photo_that_is_not_ours_is_dropped_not_linked(b2):
    listing = _draft("lst_pub_3")
    listing.image_original_url = "https://elsewhere.example/photo.jpg"
    client.post("/listing/publish", json={"listing_id": "lst_pub_3"}, headers=AUTH)
    sent = b2[-1][3]["json"]["listing"]
    assert sent["image_original_url"] is None
    assert sent["image_clean_url"].startswith("/media/")


def test_sign_in_is_passed_through_and_b2_refusals_keep_their_status(b2):
    code = client.post("/artisan/login", json={"phone": "9000000001"})
    assert code.status_code == 200 and code.json()["dev_code"] == "123456"
    wrong = client.post("/artisan/verify", json={"challenge_id": "otp_1", "code": "000000"})
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "That code did not match."


def test_b2_down_is_a_503_with_a_sentence(monkeypatch):
    def down(*args, **kwargs):
        raise platform_client.PlatformError("The Craftly platform is not reachable.")

    monkeypatch.setattr(platform_client, "_call", down)
    response = client.get("/artisan/orders", headers=AUTH)
    assert response.status_code == 503
    assert "not reachable" in response.json()["detail"]
