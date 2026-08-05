"""Shared fixtures.

Two decisions shape everything here.

**Nothing touches the network.** The runtime's outbound transport is replaced with one that
dispatches into the demo API in-process, so an execution test exercises the real executor,
the real policy checks, and the real demo API — but no socket. A test that silently reached
the internet would be a test that passes or fails for reasons unrelated to the code.

**Every test gets its own database.** The Control Plane is configured through the
environment, so the fixtures set it, clear the cached settings, and dispose the engine. Tests
that share a database pass in isolation and fail in a suite.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SPEC = REPO_ROOT / "examples" / "support-api.openapi.yaml"

ADMIN_TOKEN = "test-admin-token-0123456789"
SERVICE_TOKEN = "test-service-token-0123456789"
ADMIN_HEADERS = {"x-toollayer-admin-token": ADMIN_TOKEN}
SERVICE_HEADERS = {"x-toollayer-service-token": SERVICE_TOKEN}

DEMO_ORIGIN = "http://demo-api.internal:8081"


@pytest.fixture(scope="session")
def support_api_document() -> str:
    """The hand-authored demo OpenAPI document, as text."""
    return EXAMPLE_SPEC.read_text(encoding="utf-8")


@pytest.fixture()
def control_plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A Control Plane backed by a fresh SQLite database."""
    from control_plane import config as cp_config
    from control_plane import db as cp_db

    database = tmp_path / "control-plane.db"
    monkeypatch.setenv("TOOLLAYER_CONTROL_PLANE_DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("TOOLLAYER_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("TOOLLAYER_SERVICE_TOKEN", SERVICE_TOKEN)
    cp_config.reset_settings_cache()
    cp_db.reset_engine_cache()

    from control_plane.main import create_app

    with TestClient(create_app()) as client:
        yield client

    cp_db.reset_engine_cache()
    cp_config.reset_settings_cache()


@pytest.fixture()
def demo_api() -> Iterator[TestClient]:
    """The synthetic Support API with freshly seeded state."""
    from demo_api.data import seed_state
    from demo_api.main import create_app

    with TestClient(create_app(seed_state())) as client:
        yield client


class InProcessTransport:
    """Dispatch an outbound tool request into the demo API without a socket.

    Substituting only the transport keeps every layer above it real: argument validation,
    request construction, destination policy, redirect refusal, and the response bound all
    still run exactly as they do in production.
    """

    def __init__(self, client: TestClient, *, origin: str = DEMO_ORIGIN) -> None:
        self._client = client
        self._origin = origin
        self.sent: list[Any] = []

    def send(self, request: Any, *, limits: Any) -> tuple[int, dict[str, str], bytes]:
        self.sent.append(request)
        target = request.url
        if target.startswith(self._origin):
            target = target[len(self._origin) :]
        response = self._client.request(
            request.method,
            target,
            headers=dict(request.headers),
            content=request.body,
        )
        return response.status_code, dict(response.headers), response.content


@pytest.fixture()
def published_snapshot(control_plane: TestClient, support_api_document: str) -> dict[str, Any]:
    """Register, review, publish, and snapshot the demo connector.

    Uses the real HTTP surface rather than reaching into the service layer, so the fixture
    is itself a check that the documented flow works.
    """
    response = control_plane.post(
        "/admin/v1/connectors",
        headers=ADMIN_HEADERS,
        json={
            "connector_key": "support-api",
            "document": support_api_document,
            "document_filename": "support-api.openapi.yaml",
            "base_url": DEMO_ORIGIN,
        },
    )
    assert response.status_code == 201, response.text
    revision = response.json()["revision"]

    response = control_plane.patch(
        "/admin/v1/connectors/support-api/draft",
        headers=ADMIN_HEADERS,
        json={
            "expected_revision": revision,
            "operations": [
                {
                    "operation_key": "post /v1/tickets/{ticket_id}/status",
                    "access_mode": "restricted",
                    "allowed_roles": ["support-lead"],
                    "requires_confirmation": True,
                },
                {"operation_key": "get /v1/teams", "selection": "excluded"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]

    response = control_plane.post(
        "/admin/v1/connectors/support-api/publish",
        headers=ADMIN_HEADERS,
        json={"expected_revision": revision, "version": "0.1.0"},
    )
    assert response.status_code == 201, response.text

    response = control_plane.post(
        "/admin/v1/deployments",
        headers=ADMIN_HEADERS,
        json={"deployment_key": "demo-workspace", "display_name": "Demo Workspace"},
    )
    assert response.status_code == 201, response.text

    response = control_plane.post(
        "/admin/v1/deployments/demo-workspace/snapshots",
        headers=ADMIN_HEADERS,
        json={"selections": [{"connector_key": "support-api", "version": "0.1.0"}]},
    )
    assert response.status_code == 201, response.text

    response = control_plane.get(
        "/internal/v1/deployments/demo-workspace/snapshot", headers=SERVICE_HEADERS
    )
    assert response.status_code == 200, response.text
    document: dict[str, Any] = response.json()
    return document


@pytest.fixture()
def outbound(demo_api: TestClient) -> InProcessTransport:
    """The transport the runtime sends through, so tests can inspect what was sent."""
    return InProcessTransport(demo_api)


@pytest.fixture()
def stub_resolver() -> _StubResolver:
    """A hermetic resolver mapping the demo host to a globally routable address."""
    return _StubResolver({"demo-api.internal": ("100.0.0.1",)})


@pytest.fixture()
def runtime_executor(outbound: InProcessTransport, stub_resolver: _StubResolver) -> Any:
    """The real executor, with only the socket and the resolver replaced."""
    from toollayer_policy import DestinationPolicy, ToolExecutor

    return ToolExecutor(
        # The demo origin is a name, not a literal address, so the allowlist check and the
        # post-resolution address check are both exercised — with a stub resolver so no DNS
        # query leaves the machine.
        policy=DestinationPolicy.from_origins([DEMO_ORIGIN], allow_plaintext_http=True),
        transport=outbound,
        resolver=stub_resolver,
    )


@pytest.fixture()
def loaded_snapshot(published_snapshot: dict[str, Any]) -> Any:
    """The published snapshot, loaded and verified the way the runtime loads it."""
    from runtime_service.snapshot import load_snapshot_document

    return load_snapshot_document(published_snapshot)


@pytest.fixture()
def orchestrator(loaded_snapshot: Any, runtime_executor: Any) -> Any:
    """A runtime wired to the published snapshot and the in-process demo API."""
    from runtime_service.orchestrator import Orchestrator
    from runtime_service.snapshot import SnapshotClient, SnapshotStore
    from toollayer_mock_llm import MockLLMProvider

    store = SnapshotStore(
        SnapshotClient(base_url="http://control-plane.invalid", service_token=SERVICE_TOKEN),
        deployment_key="demo-workspace",
    )
    store.set(loaded_snapshot)
    return Orchestrator(store=store, provider=MockLLMProvider(), executor=runtime_executor)


class _StubResolver:
    """A hermetic DNS resolver for tests."""

    def __init__(self, table: dict[str, tuple[str, ...]]) -> None:
        self._table = table

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            return self._table[host]
        except KeyError:
            from toollayer_contracts.errors import ErrorCode, PolicyDenied

            raise PolicyDenied(
                ErrorCode.UPSTREAM_UNAVAILABLE, "the destination host could not be resolved"
            ) from None


@pytest.fixture(autouse=True)
def _quiet_runtime_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep runtime settings deterministic regardless of the developer's shell."""
    from runtime_service import config as runtime_config

    for name in list(os.environ):
        if name.startswith("TOOLLAYER_") and name not in {
            "TOOLLAYER_CONTROL_PLANE_DATABASE_URL",
            "TOOLLAYER_ADMIN_TOKEN",
            "TOOLLAYER_SERVICE_TOKEN",
        }:
            monkeypatch.delenv(name, raising=False)
    runtime_config.reset_settings_cache()
    yield
    runtime_config.reset_settings_cache()
