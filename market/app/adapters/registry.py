"""Picks which implementation sits behind each port.

One environment variable, `CRAFTLY_ADAPTERS`:

    stub  (default)  seed JSON, placeholder price engine, JSONL order log
    http             the real B1 and B2 services

`CRAFTLY_PRICE_ADAPTERS` overrides it for prices and bulk splits alone, so
the shop can run off B2 with the placeholder price engine while B1 is down.

Everything is a lazily-built module-level singleton. `reset()` drops them,
which is how tests swap in a fake without touching the routes.
"""

from __future__ import annotations

from app import config
from app.ports import CatalogSource, OrderEngine, OrderSink, PassportSource, PriceEngine

_catalog: CatalogSource | None = None
_prices: PriceEngine | None = None
_engine: OrderEngine | None = None
_passports: PassportSource | None = None
_orders: OrderSink | None = None


def catalog() -> CatalogSource:
    global _catalog
    if _catalog is None:
        if config.using_stubs():
            from app.adapters.stub_catalog import catalog as seed_catalog

            _catalog = seed_catalog()
        else:
            from app.adapters.http_platform import HttpCatalog

            _catalog = HttpCatalog(config.PLATFORM_URL)
    return _catalog


def prices() -> PriceEngine:
    global _prices
    if _prices is None:
        if config.using_stub_prices():
            from app.adapters.stub_price import StubPriceEngine

            _prices = StubPriceEngine()
        else:
            from app.adapters.http_platform import HttpPriceEngine

            _prices = HttpPriceEngine(config.ENGINES_URL)
    return _prices


def order_engine() -> OrderEngine:
    global _engine
    if _engine is None:
        if config.using_stub_prices():
            from app.adapters.stub_engine import StubOrderEngine

            _engine = StubOrderEngine(catalog(), prices())
        else:
            from app.adapters.http_platform import HttpOrderEngine

            _engine = HttpOrderEngine(config.ENGINES_URL)
    return _engine


def passports() -> PassportSource:
    global _passports
    if _passports is None:
        if config.using_stubs():
            from app.adapters.stub_passport import StubPassportSource

            _passports = StubPassportSource(catalog())
        else:
            from app.adapters.http_platform import HttpPassportSource

            _passports = HttpPassportSource(config.PLATFORM_URL)
    return _passports


def orders() -> OrderSink:
    global _orders
    if _orders is None:
        if config.using_stubs():
            from app.adapters.stub_orders import StubOrderSink

            _orders = StubOrderSink()
        else:
            from app.adapters.http_platform import HttpOrderSink

            _orders = HttpOrderSink(config.PLATFORM_URL)
    return _orders


def reset() -> None:
    """Forget every cached adapter. Used by tests and by seed reloads."""
    global _catalog, _prices, _engine, _passports, _orders
    _catalog = _prices = _engine = _passports = _orders = None


def override(
    *,
    catalog_impl: CatalogSource | None = None,
    price_impl: PriceEngine | None = None,
    engine_impl: OrderEngine | None = None,
    passport_impl: PassportSource | None = None,
    order_impl: OrderSink | None = None,
) -> None:
    """Install specific implementations. Tests only."""
    global _catalog, _prices, _engine, _passports, _orders
    if catalog_impl is not None:
        _catalog = catalog_impl
    if price_impl is not None:
        _prices = price_impl
    if engine_impl is not None:
        _engine = engine_impl
    if passport_impl is not None:
        _passports = passport_impl
    if order_impl is not None:
        _orders = order_impl
