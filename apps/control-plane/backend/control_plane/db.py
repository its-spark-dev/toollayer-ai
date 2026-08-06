"""Database engine and session management."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from control_plane.config import get_settings
from control_plane.models import Base

__all__ = [
    "TransactionalRoute",
    "create_schema",
    "get_engine",
    "get_session",
    "reset_engine_cache",
    "session_scope",
]


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


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    The session is published on ``request.state`` so :class:`TransactionalRoute` can commit it
    while the request is still in flight. The ``session_scope`` commit below still runs and is
    still correct — by then the transaction is normally already committed and it is a no-op —
    but it is a fallback, not the mechanism. See :class:`TransactionalRoute` for why.
    """
    with session_scope() as session:
        request.state.db_session = session
        yield session


class TransactionalRoute(APIRoute):
    """A route that commits its request's transaction *before* the response is sent.

    FastAPI runs the exit half of a ``yield`` dependency after the response has already gone
    to the client. Committing there means a ``201`` can be observed before the row it
    describes is durable, and a client that then reads gets a ``404``.

    On a reused keep-alive connection that is invisible: uvicorn finishes the whole ASGI
    cycle, teardown included, before it reads the next request off that socket, so the two
    requests serialize. Send the follow-up on a *different* connection and it is handled by an
    independent task that can start while the first commit is still pending. Measured on this
    codebase over 3,000 create-then-read pairs: zero failures reusing the connection, seven on
    fresh ones. It reached CI twice as ``no deployment exists with that key`` and as a spurious
    ``revision_conflict``, and both times passed on re-run.

    Committing here closes that window. It also means a commit that *fails* raises while the
    response is still being built, so it surfaces as a 500 instead of being logged after a
    ``201`` the client has already accepted.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def commit_before_responding(request: Request) -> Response:
            response = await handle(request)
            session: Session | None = getattr(request.state, "db_session", None)
            if session is not None and session.in_transaction():
                session.commit()
            return response

        return commit_before_responding


def reset_engine_cache() -> None:
    """Dispose the engine and clear the caches. Used by tests between databases."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    _session_factory.cache_clear()
