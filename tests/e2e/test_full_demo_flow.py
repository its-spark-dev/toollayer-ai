"""The end-to-end flow the README promises, executed as one test.

This is the claim the whole project rests on: an OpenAPI document goes in one end, and a
natural-language request comes out the other having called a real API under policy. If this
test passes, the README is not overstating anything.

It runs entirely offline — no API key, no network, and a deterministic provider.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.conftest import ADMIN_HEADERS, DEMO_ORIGIN, SERVICE_HEADERS

from toollayer_contracts import verify_digest
from toollayer_contracts.errors import ErrorCode, ToolLayerError
from toollayer_policy import CallerIdentity

pytestmark = pytest.mark.e2e

AGENT = CallerIdentity.of("avery@example.org", ["support-agent"])
LEAD = CallerIdentity.of("bao@example.org", ["support-lead"])


def test_openapi_document_to_governed_execution(
    control_plane, demo_api, support_api_document: str, outbound, stub_resolver
) -> None:
    """Upload → analyze → review → publish → deploy → load → ask → execute → reject."""
    from runtime_service.orchestrator import Orchestrator
    from runtime_service.snapshot import SnapshotClient, SnapshotStore, load_snapshot_document
    from toollayer_mock_llm import MockLLMProvider
    from toollayer_policy import DestinationPolicy, ToolExecutor

    # 1. Register the synthetic Support API.
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
    assert response.status_code == 201
    draft: dict[str, Any] = response.json()

    # 2. Analysis converted every operation and proposed a version.
    assert draft["proposed_version"] == "0.1.0"
    assert len(draft["analysis"]["operations"]) == 6
    generated = {
        entry["key"]: entry["tool"]["tool_name"] for entry in draft["analysis"]["operations"]
    }
    assert generated["get /v1/tickets"] == "list_support_tickets"
    assert generated["post /v1/tickets/{ticket_id}/status"] == "change_support_ticket_status"

    # 3. Review: drop one operation and restrict the state-changing one.
    response = control_plane.patch(
        "/admin/v1/connectors/support-api/draft",
        headers=ADMIN_HEADERS,
        json={
            "expected_revision": draft["revision"],
            "operations": [
                {"operation_key": "get /v1/teams", "selection": "excluded"},
                {
                    "operation_key": "post /v1/tickets/{ticket_id}/status",
                    "access_mode": "restricted",
                    "allowed_roles": ["support-lead"],
                    "requires_confirmation": True,
                },
            ],
        },
    )
    assert response.status_code == 200
    reviewed = response.json()
    assert reviewed["readiness"]["ready"] is True

    # 4. Publish an immutable version.
    response = control_plane.post(
        "/admin/v1/connectors/support-api/publish",
        headers=ADMIN_HEADERS,
        json={"expected_revision": reviewed["revision"], "version": "0.1.0"},
    )
    assert response.status_code == 201
    published = response.json()
    assert published["tool_count"] == 5
    assert "list_support_teams" not in published["tool_names"]

    # 5. Create a deployment and an immutable snapshot.
    control_plane.post(
        "/admin/v1/deployments",
        headers=ADMIN_HEADERS,
        json={"deployment_key": "demo-workspace", "display_name": "Demo Workspace"},
    )
    response = control_plane.post(
        "/admin/v1/deployments/demo-workspace/snapshots",
        headers=ADMIN_HEADERS,
        json={"selections": [{"connector_key": "support-api", "version": "0.1.0"}]},
    )
    assert response.status_code == 201
    snapshot_summary = response.json()

    # 6. The runtime loads the snapshot over the internal API and verifies it.
    served = control_plane.get(
        "/internal/v1/deployments/demo-workspace/snapshot", headers=SERVICE_HEADERS
    )
    assert served.status_code == 200
    document = served.json()
    assert document["snapshot_id"] == snapshot_summary["snapshot_id"]
    assert verify_digest(
        document, document["snapshot_digest"], exclude=("snapshot_id", "snapshot_digest")
    )

    store = SnapshotStore(
        SnapshotClient(base_url="http://control-plane.invalid", service_token="unused-token-000"),
        deployment_key="demo-workspace",
    )
    store.set(load_snapshot_document(document, etag=served.headers["etag"]))
    orchestrator = Orchestrator(
        store=store,
        provider=MockLLMProvider(),
        executor=ToolExecutor(
            policy=DestinationPolicy.from_origins([DEMO_ORIGIN], allow_plaintext_http=True),
            transport=outbound,
            resolver=stub_resolver,
        ),
    )

    # 7. Discovery is role-aware.
    assert {bound.tool_name for bound in orchestrator.discover(AGENT)} == {
        "list_support_tickets",
        "get_support_ticket",
        "list_support_team_members",
        "assign_support_ticket",
    }
    assert "change_support_ticket_status" in {
        bound.tool_name for bound in orchestrator.discover(LEAD)
    }

    # 8. A natural-language request selects a tool, validates arguments, and executes.
    outcome = orchestrator.handle(
        "show me the open high priority tickets for the billing team", caller=AGENT
    )
    assert outcome.selected_tool == "list_support_tickets"
    assert outcome.arguments == {
        "status": "open",
        "priority": "high",
        "team_id": "team-billing",
    }
    assert outcome.result is not None
    assert outcome.result.http_status == 200
    assert outcome.result.untrusted is True
    assert outcome.result.content["items"][0]["ticket_id"] == "TKT-1001"

    # The trace records each decision in order, so the demo can show them.
    steps = [step["step"] for step in outcome.trace.steps]
    assert steps == [
        "snapshot_loaded",
        "tools_discovered",
        "tool_selected",
        "arguments_generated",
        "arguments_validated",
        "policy_evaluated",
        "executed",
        "response_formatted",
    ]

    # 9. An authorized caller can change state, with confirmation.
    outcome = orchestrator.handle("mark ticket TKT-1001 as resolved", caller=LEAD, confirmed=True)
    assert outcome.selected_tool == "change_support_ticket_status"
    assert outcome.result is not None and outcome.result.http_status == 200
    assert outcome.result.content["status"] == "resolved"

    # 10. Three rejections, each with its own code.
    with pytest.raises(ToolLayerError) as unauthorized:
        orchestrator.execute_tool(
            tool_name="change_support_ticket_status",
            arguments={"ticket_id": "TKT-1003", "body": {"status": "closed"}},
            caller=AGENT,
            confirmed=True,
        )
    assert unauthorized.value.code == ErrorCode.ROLE_NOT_PERMITTED

    with pytest.raises(ToolLayerError) as fabricated:
        orchestrator.execute_tool(
            tool_name="delete_every_ticket", arguments={}, caller=LEAD, confirmed=True
        )
    assert fabricated.value.code == ErrorCode.UNKNOWN_TOOL

    with pytest.raises(ToolLayerError) as invalid:
        orchestrator.execute_tool(
            tool_name="list_support_tickets",
            arguments={"status": "open", "callback_url": "https://attacker.test/collect"},
            caller=AGENT,
        )
    assert invalid.value.code == ErrorCode.ARGUMENT_VALIDATION_FAILED

    # 11. Every outbound request went to the reviewed destination and nowhere else.
    assert outbound.sent
    assert all(request.url.startswith(DEMO_ORIGIN) for request in outbound.sent)


def test_the_runtime_serves_the_flow_over_http(
    control_plane, published_snapshot: dict[str, Any], runtime_executor, outbound
) -> None:
    """The same flow through the runtime's own HTTP surface, as a client would use it."""
    from fastapi.testclient import TestClient

    from runtime_service.main import create_app
    from runtime_service.orchestrator import Orchestrator
    from runtime_service.snapshot import SnapshotClient, SnapshotStore, load_snapshot_document
    from toollayer_mock_llm import MockLLMProvider

    store = SnapshotStore(
        SnapshotClient(base_url="http://control-plane.invalid", service_token="unused-token-000"),
        deployment_key="demo-workspace",
    )
    store.set(load_snapshot_document(published_snapshot))
    orchestrator = Orchestrator(store=store, provider=MockLLMProvider(), executor=runtime_executor)

    with TestClient(create_app(orchestrator)) as client:
        health = client.get("/healthz").json()
        assert health["provider"] == "mock"

        ready = client.get("/readyz").json()
        assert ready["status"] == "ready"
        assert ready["tool_count"] == 5

        agent_headers = {
            "x-toollayer-caller": "avery@example.org",
            "x-toollayer-roles": "support-agent",
        }
        tools = client.get("/v1/tools", headers=agent_headers).json()
        assert {tool["tool_name"] for tool in tools["tools"]} == {
            "list_support_tickets",
            "get_support_ticket",
            "list_support_team_members",
            "assign_support_ticket",
        }

        response = client.post(
            "/v1/chat",
            headers=agent_headers,
            json={"utterance": "show me the open high priority tickets for the billing team"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["selected_tool"] == "list_support_tickets"
        assert body["result"]["untrusted"] is True
        assert body["snapshot"]["revision"] == 1

        denied = client.post(
            "/v1/tools/change_support_ticket_status/execute",
            headers=agent_headers,
            json={
                "arguments": {"ticket_id": "TKT-1003", "body": {"status": "closed"}},
                "confirmed": True,
            },
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "role_not_permitted"

        unknown = client.post(
            "/v1/tools/delete_every_ticket/execute", headers=agent_headers, json={"arguments": {}}
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "unknown_tool"

        anonymous = client.get("/v1/tools").json()
        assert "change_support_ticket_status" not in {
            tool["tool_name"] for tool in anonymous["tools"]
        }
