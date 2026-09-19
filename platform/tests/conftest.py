"""Fixtures for the platform tests.

Every test gets its own SQLite file in a tmp directory, its own media
directory and its own QR directory. Nothing here touches the working
tree, and no test can see another test's rows — which matters more than
usual in this slice, because half of what is being tested is persistence
and a leaked row looks exactly like a passing test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, db as db_module, media


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "QR_DIR", tmp_path / "qr")
    monkeypatch.setattr(config, "OTP_ECHO", True)
    monkeypatch.setattr(config, "REQUIRE_ORDER_AUTH", False)
    media.reset(tmp_path / "media")

    db_module.bind(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    db_module.create_all()
    yield
    db_module.engine.dispose()


@pytest.fixture
def session():
    with db_module.session_scope() as db:
        yield db


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture
def seeded():
    """The full seed catalogue: 12 listings, 14 artisans, 9 clusters."""
    from app import seed

    return seed.load()


@pytest.fixture
def artisan_headers(client, seeded):
    """Signed in as the demo artisan, via the real OTP flow."""
    from app.seed import DEMO_ARTISAN_PHONE

    issued = client.post(
        "/auth/artisan/request-otp", json={"phone": DEMO_ARTISAN_PHONE}
    ).json()
    token = client.post(
        "/auth/artisan/verify",
        json={"challenge_id": issued["challenge_id"], "code": issued["dev_code"]},
    ).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


@pytest.fixture
def artisan_id(client, artisan_headers):
    return client.get("/auth/me", headers=artisan_headers).json()["artisan_id"]


@pytest.fixture
def a_listing(client, artisan_headers):
    """A published listing with a complete wage floor, owned by the demo artisan."""
    payload = {
        "listing": {
            "listing_id": "lst_test_complete",
            "artisan_id": "ignored-by-design",
            "title_en": "Test terracotta bowl",
            "source_language": "hi",
            "material_cost_inr": 60,
            "hours_worked": 4.0,
        },
        "inventory": {"in_stock": 10, "monthly_capacity": 100, "lead_time_days": 5},
        "publish": True,
    }
    return client.post("/listings", json=payload, headers=artisan_headers).json()


def order_payload(listing_id: str = "lst_test_complete", **overrides) -> dict:
    """A well-formed retail order. Overrides are shallow-merged."""
    payload = {
        "order_id": "ord_test_0001",
        "kind": "retail",
        "buyer": {"name": "A Buyer", "phone": "9876543210"},
        "lines": [
            {
                "listing_id": listing_id,
                "title": "Test terracotta bowl",
                "quantity": 2,
                "unit_price_inr": 500,
                "artisan_take_home_inr": 380,
            }
        ],
        "source": "storefront",
    }
    payload.update(overrides)
    return payload
