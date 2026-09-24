"""Photos from B2's media store, and prices switched apart from the catalogue."""

from __future__ import annotations

import httpx
import pytest

from app import config, media
from app.adapters import registry
from app.adapters.stub_price import StubPriceEngine


@pytest.fixture
def http_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ADAPTERS", "http")
    monkeypatch.setattr(config, "MEDIA_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "PLATFORM_URL", "http://b2.test")


def test_seed_photos_are_served_from_disk():
    assert media.file_for("lst_51ea90cb7d24.jpg") is not None


@pytest.mark.parametrize("name", ["../secrets.txt", "a/b.jpg", "..jpg", "", ".hidden"])
def test_unsafe_names_are_refused(name, http_mode):
    assert media.file_for(name) is None


def test_a_published_photo_is_fetched_from_b2_once(http_mode, monkeypatch):
    fetched = []

    def fake_get(url, timeout):
        fetched.append(url)
        return httpx.Response(200, content=b"jpeg-bytes")

    monkeypatch.setattr(media.httpx, "get", fake_get)
    name = "ab" * 32 + ".jpg"
    first = media.file_for(name)
    second = media.file_for(name)
    assert first == second and first.read_bytes() == b"jpeg-bytes"
    assert fetched == [f"http://b2.test/media/{name}"]  # cached after the first time


def test_a_photo_b2_does_not_have_is_a_404_not_an_error(http_mode, monkeypatch):
    monkeypatch.setattr(media.httpx, "get", lambda url, timeout: httpx.Response(404))
    assert media.file_for("cd" * 32 + ".jpg") is None


def test_stub_mode_never_reaches_for_b2(monkeypatch):
    monkeypatch.setattr(config, "ADAPTERS", "stub")
    monkeypatch.setattr(media.httpx, "get", lambda *a, **k: pytest.fail("called B2 in stub mode"))
    assert media.file_for("ef" * 32 + ".jpg") is None


def test_media_route_serves_seed_photos(client):
    response = client.get("/media/lst_51ea90cb7d24.jpg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert client.get("/media/nope.jpg").status_code == 404


def test_prices_can_stay_on_the_placeholder_while_the_catalogue_is_b2(monkeypatch):
    monkeypatch.setattr(config, "ADAPTERS", "http")
    monkeypatch.setattr(config, "PRICE_ADAPTERS", "stub")
    registry.reset()
    assert isinstance(registry.prices(), StubPriceEngine)
    assert type(registry.catalog()).__name__ == "HttpCatalog"
