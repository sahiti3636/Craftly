"""Engine, session and schema creation.

SQLite by default, and that is a considered choice rather than a shortcut.
This repo has six people on six laptops and a demo that has to run on a
seventh; a slice whose first setup step is "install Postgres" is a slice
that is down on demo day. ``CRAFTLY_DATABASE_URL`` takes a
``postgresql+psycopg://`` URL unchanged, and nothing above this module
knows which one is behind it.

Two SQLite-specific things are switched on here and both matter:

* **Foreign keys.** SQLite ignores ``REFERENCES`` unless you ask per
  connection. Without this an order line can point at a listing that does
  not exist, which is exactly the corruption a foreign key is for.
* **WAL.** A reader (C1 rendering a shop) and a writer (an artisan
  publishing a listing) otherwise take turns, and the reader gets
  "database is locked" during the one minute of the demo when both happen.

Schema creation is ``create_all`` rather than Alembic. There is one
deployed instance of this and its data is re-seedable; a migration tool
earns its place when neither of those is true any more.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app import config


class Base(DeclarativeBase):
    pass


def _engine_for(url: str) -> Engine:
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(
        url,
        echo=False,
        future=True,
        # A FastAPI request can be served on a different thread than the one
        # that opened the connection; the session is still used by exactly
        # one request at a time, which is the condition this flag needs.
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


engine: Engine = _engine_for(config.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def create_all() -> None:
    from app import models  # noqa: F401 - registers the mappers before create_all

    Base.metadata.create_all(engine)


def session() -> Iterator[Session]:
    """FastAPI dependency. One session per request, committed by the route."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts and startup work."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def bind(url: str) -> None:
    """Point the module at a different database. Tests and scripts only."""
    global engine, SessionLocal
    engine = _engine_for(url)
    SessionLocal.configure(bind=engine)
