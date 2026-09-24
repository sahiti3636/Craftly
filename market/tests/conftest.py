"""Test fixtures for the buyer surfaces.

Two things every test needs: adapters reset so one test's cached
catalogue does not leak into the next, and the order log and reels
directory pointed at a tmp path so a test run does not scribble into the
working tree.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app.adapters import registry
from app.adapters.stub_catalog import catalog as seed_catalog
from app.adapters.stub_orders import StubOrderSink
from app.adapters.stub_price import StubPriceEngine


@pytest.fixture(autouse=True)
def isolated_adapters(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORDERS_PATH", tmp_path / "orders.jsonl")
    monkeypatch.setattr(config, "REELS_DIR", tmp_path / "reels")
    monkeypatch.setattr(config, "QR_DIR", tmp_path / "qr")
    # Reels stay silent in tests: the voiceover calls Google over the network.
    monkeypatch.setattr(config, "REEL_VOICE", "off")
    registry.reset()
    registry.override(order_impl=StubOrderSink(tmp_path / "orders.jsonl"))
    yield
    registry.reset()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture
def catalog():
    return seed_catalog()


@pytest.fixture
def prices():
    return StubPriceEngine()


@pytest.fixture
def entry(catalog):
    """A well-formed listing: both floor inputs present, market above floor."""
    return catalog.get_entry("lst_51ea90cb7d24")


@pytest.fixture
def entry_without_hours(catalog):
    """The listing whose `hours_worked` is null — no honest floor."""
    return catalog.get_entry("lst_77b0c4d8e913")


@pytest.fixture
def entry_below_market(catalog):
    """The listing the open market pays less for than minimum wage."""
    return catalog.get_entry("lst_9f8a2e1c4b3d")
