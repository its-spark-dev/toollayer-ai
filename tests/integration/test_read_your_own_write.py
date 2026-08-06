"""A successful write response means the transaction is committed.

FastAPI runs the exit half of a ``yield`` dependency *after* the response has gone to the
client. The Control Plane's session dependency committed there, so a ``201`` could be observed
before the row it described was durable.

On a reused keep-alive connection that is invisible — uvicorn finishes the whole ASGI cycle,
teardown included, before it reads the next request off that socket, so the two requests
serialize. Send the follow-up on a *different* connection and it is handled by an independent
task that can start while the first commit is still pending.

Measured on this codebase before the fix, over 3,000 create-then-read pairs: **zero** failures
reusing the connection, **seven** on fresh ones. It reached CI twice — once as
``no deployment exists with that key``, once as a spurious ``revision_conflict`` — and passed
on re-run both times, which is exactly how a race of this shape presents.

These tests run a real uvicorn server rather than ``TestClient``, because the defect lives in
connection and event-loop scheduling that ``TestClient`` does not model.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn

pytestmark = pytest.mark.integration

ADMIN = {"x-toollayer-admin-token": "test-admin-token-0123456789"}

#: No keep-alive: every request opens its own connection, so a follow-up can be handled while
#: the previous request's transaction is still open. This is the condition that exposed the
#: defect, and reusing a connection is what hid it.
FRESH_CONNECTION = httpx.Limits(max_keepalive_connections=0, max_connections=64)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def live_control_plane(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A real Control Plane on a real socket, backed by a real SQLite file."""
    from control_plane import config as cp_config
    from control_plane import db as cp_db

    monkeypatch.setenv("TOOLLAYER_CONTROL_PLANE_DATABASE_URL", f"sqlite:///{tmp_path}/ryow.db")
    monkeypatch.setenv("TOOLLAYER_ADMIN_TOKEN", "test-admin-token-0123456789")
    monkeypatch.setenv("TOOLLAYER_SERVICE_TOKEN", "test-service-token-0123456789")
    cp_config.reset_settings_cache()
    cp_db.reset_engine_cache()

    from control_plane.main import create_app

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover - only on a machine that cannot start a server at all
        pytest.fail("the control plane never became reachable")

    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        cp_db.reset_engine_cache()
        cp_config.reset_settings_cache()


class TestASuccessResponseMeansCommitted:
    def test_a_created_deployment_is_immediately_readable_on_a_fresh_connection(
        self, live_control_plane: str
    ) -> None:
        """The exact sequence that failed in CI, on the connection shape that exposes it."""
        violations = []
        with httpx.Client(
            base_url=live_control_plane, headers=ADMIN, timeout=30, limits=FRESH_CONNECTION
        ) as client:
            for index in range(150):
                key = f"ryow-{index}"
                created = client.post(
                    "/admin/v1/deployments",
                    json={"deployment_key": key, "display_name": "Read your own write"},
                )
                assert created.status_code == 201, created.text
                read = client.get(f"/admin/v1/deployments/{key}/snapshots")
                if read.status_code != 200:
                    violations.append((key, read.status_code, read.text[:80]))

        assert violations == [], (
            f"{len(violations)}/150 writes were not visible to the next request: {violations[:3]}"
        )

    def test_a_reviewed_draft_publishes_without_a_spurious_revision_conflict(
        self, live_control_plane: str, support_api_document: str
    ) -> None:
        """The other CI failure: PATCH returned a revision the next request did not agree with."""
        conflicts = []
        with httpx.Client(
            base_url=live_control_plane, headers=ADMIN, timeout=30, limits=FRESH_CONNECTION
        ) as client:
            for index in range(25):
                key = f"conn-{index}"
                registered = client.post(
                    "/admin/v1/connectors",
                    json={
                        "connector_key": key,
                        "document": support_api_document,
                        "document_filename": "s.yaml",
                        "base_url": "https://api.example.org",
                    },
                )
                assert registered.status_code == 201, registered.text
                reviewed = client.patch(
                    f"/admin/v1/connectors/{key}/draft",
                    json={
                        "expected_revision": registered.json()["revision"],
                        "operations": [{"operation_key": "get /v1/teams", "selection": "excluded"}],
                    },
                )
                assert reviewed.status_code == 200, reviewed.text
                published = client.post(
                    f"/admin/v1/connectors/{key}/publish",
                    json={"expected_revision": reviewed.json()["revision"], "version": "0.1.0"},
                )
                if published.status_code != 201:
                    conflicts.append(
                        (key, published.status_code, published.json()["error"]["code"])
                    )

        assert conflicts == [], f"{len(conflicts)}/25 publishes were refused: {conflicts[:3]}"

    def test_concurrent_clients_each_read_their_own_write(self, live_control_plane: str) -> None:
        """Independent clients, each on its own connection, interleaved by the event loop."""
        from concurrent.futures import ThreadPoolExecutor

        def create_then_read(index: int) -> tuple[int, int]:
            key = f"conc-{index}"
            with httpx.Client(
                base_url=live_control_plane, headers=ADMIN, timeout=30, limits=FRESH_CONNECTION
            ) as client:
                created = client.post(
                    "/admin/v1/deployments",
                    json={"deployment_key": key, "display_name": "Concurrent"},
                )
                read = client.get(f"/admin/v1/deployments/{key}/snapshots")
                return created.status_code, read.status_code

        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(create_then_read, range(150)))

        bad = [pair for pair in outcomes if pair != (201, 200)]
        assert bad == [], f"{len(bad)}/150 concurrent create-then-read pairs disagreed: {bad[:3]}"


class TestTheCommitHappensBeforeTheResponse:
    def test_the_session_is_already_committed_when_the_response_is_built(
        self, control_plane: Any
    ) -> None:
        """The mechanism itself, asserted rather than inferred from timing.

        ``TransactionalRoute`` commits after the endpoint returns and before the response
        leaves. By the time the dependency's own teardown runs there is nothing left to
        commit — which is what makes the window unobservable rather than merely narrow.
        """
        from control_plane import db as cp_db

        states: list[bool] = []
        original = cp_db.session_scope

        from contextlib import contextmanager

        @contextmanager
        def recording_scope() -> Iterator[Any]:
            with original() as session:
                yield session
                # Reached during dependency teardown, after the route class has committed.
                states.append(session.in_transaction())

        cp_db.session_scope = recording_scope
        try:
            response = control_plane.post(
                "/admin/v1/deployments",
                headers=ADMIN,
                json={"deployment_key": "committed", "display_name": "Committed"},
            )
        finally:
            cp_db.session_scope = original

        assert response.status_code == 201
        assert states, "the session dependency never ran"
        assert states[-1] is False, (
            "a transaction was still open when the response was already built; the commit is "
            "happening in dependency teardown again"
        )
