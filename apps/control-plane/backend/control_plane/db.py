"""Database engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from control_plane.config import get_settings
from control_plane.models import Base

__all__ = ["create_schema", "get_engine", "get_session", "reset_engine_cache", "session_scope"]


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide engine."""
    settings = get_settings()
    url = settings.database_url
    connect_args: dict[str, Any] = {}

    if url.startswith("sqlite"):
        # SQLite is the default so the demo runs with no external service. Two adjustments
        # make it behave enough like a real database for this workload.
        connect_args["check_same_thread"] = False
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, future=True, connect_args=connect_args, pool_pre_ping=True)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(connection: Any, _: Any) -> None:
            cursor = connection.cursor()
            # Foreign keys are off by default in SQLite, which would silently disable the
            # cascade rules the model relies on.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def create_schema() -> None:
    """Create tables directly from the model metadata.

    Used by tests and by the first local run. Alembic owns schema changes for anything that
    persists; this is the shortcut for a database that starts empty every time.
    """
    Base.metadata.create_all(get_engine())


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Run a unit of work in a transaction that commits or rolls back as a whole."""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    with session_scope() as session:
        yield session


def reset_engine_cache() -> None:
    """Dispose the engine and clear the caches. Used by tests between databases."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    _session_factory.cache_clear()
