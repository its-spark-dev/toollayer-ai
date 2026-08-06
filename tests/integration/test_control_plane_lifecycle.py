"""Control Plane lifecycle: draft → review → publish → deploy → snapshot.

These exercise the rules that only exist once several components are wired together:
optimistic concurrency, publication readiness, immutability, and snapshot construction.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.conftest import ADMIN_HEADERS, DEMO_ORIGIN, SERVICE_HEADERS

from toollayer_contracts import SNAPSHOT_DIGEST_EXCLUDED, verify_digest

pytestmark = pytest.mark.integration


def _register(client, document: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "connector_key": "support-api",
        "document": document,
        "document_filename": "support-api.openapi.yaml",
        "base_url": DEMO_ORIGIN,
    }
    payload.update(overrides)
    response = client.post("/admin/v1/connectors", headers=ADMIN_HEADERS, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestIngestion:
    def test_registering_a_document_produces_a_reviewable_draft(
        self, control_plane, support_api_document: str
    ) -> None:
        draft = _register(control_plane, support_api_document)
        assert draft["revision"] == 1
        assert draft["source"]["digest"].startswith("sha256:")
        assert draft["source"]["spec_version"] == "3.1.0"
        assert len(draft["analysis"]["operations"]) == 6
        assert all(operation["tool"] for operation in draft["analysis"]["operations"])
        assert draft["readiness"]["ready"] is True

    def test_the_draft_keeps_the_source_operation_beside_the_generated_tool(
        self, control_plane, support_api_document: str
    ) -> None:
        """The console shows both. Without the source, the transformation is unreviewable."""
        draft = _register(control_plane, support_api_document)
        operation = next(
            entry for entry in draft["analysis"]["operations"] if entry["key"] == "get /v1/tickets"
        )
        assert operation["source_operation"]["operationId"] == "listSupportTickets"
        assert operation["tool"]["tool_name"] == "list_support_tickets"

    def test_re_registering_replaces_the_draft(
        self, control_plane, support_api_document: str
    ) -> None:
        first = _register(control_plane, support_api_document)
        control_plane.patch(
            "/admin/v1/connectors/support-api/draft",
            headers=ADMIN_HEADERS,
            json={"expected_revision": first["revision"], "operations": []},
        )
        second = _register(control_plane, support_api_document)
        assert second["revision"] == 1

    def test_a_malformed_document_is_rejected_with_a_pointer(self, control_plane) -> None:
        response = control_plane.post(
            "/admin/v1/connectors",
            headers=ADMIN_HEADERS,
            json={"connector_key": "broken", "document": "openapi: '3.1.0'\nno_paths: true\n"},
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "invalid_source_document"
        assert error["pointer"] == "/paths"


class TestReview:
    def test_a_review_update_increments_the_revision(
        self, control_plane, support_api_document: str
    ) -> None:
        draft = _register(control_plane, support_api_document)
        response = control_plane.patch(
            "/admin/v1/connectors/support-api/draft",
            headers=ADMIN_HEADERS,
            json={
                "expected_revision": draft["revision"],
                "operations": [
                    {
                        "operation_key": "get /v1/tickets",
                        "description": "Search the support queue by status, priority, or team.",
                    }
                ],
            },
        )
        assert response.status_code == 200
        updated = response.json()
        assert updated["revision"] == draft["revision"] + 1
        entry = next(
            item
            for item in updated["review"]["operations"]
            if item["operation_key"] == "get /v1/tickets"
        )
        assert entry["description_origin"] == "human"

    def test_a_stale_revision_is_rejected_rather_than_merged(
        self, control_plane, support_api_document: str
    ) -> None:
        draft = _register(control_plane, support_api_document)
        body = {
            "expected_revision": draft["revision"],
            "operations": [{"operation_key": "get /v1/tickets", "selection": "excluded"}],
        }
        assert (
            control_plane.patch(
                "/admin/v1/connectors/support-api/draft", headers=ADMIN_HEADERS, json=body
            ).status_code
            == 200
        )
        second = control_plane.patch(
            "/admin/v1/connectors/support-api/draft", headers=ADMIN_HEADERS, json=body
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "revision_conflict"

    def test_a_restricted_tool_with_no_roles_is_refused_at_review_time(
        self, control_plane, support_api_document: str
    ) -> None:
        draft = _register(control_plane, support_api_document)
        response = control_plane.patch(
            "/admin/v1/connectors/support-api/draft",
            headers=ADMIN_HEADERS,
            json={
                "expected_revision": draft["revision"],
                "operations": [
                    {
                        "operation_key": "get /v1/tickets",
                        "access_mode": "restricted",
                        "allowed_roles": [],
                    }
                ],
            },
        )
        assert response.status_code == 400

    def test_excluding_every_operation_blocks_publication(
        self, control_plane, support_api_document: str
    ) -> None:
        draft = _register(control_plane, support_api_document)
        response = control_plane.patch(
            "/admin/v1/connectors/support-api/draft",
            headers=ADMIN_HEADERS,
            json={
                "expected_revision": draft["revision"],
                "operations": [
                    {"operation_key": entry["key"], "selection": "excluded"}
                    for entry in draft["analysis"]["operations"]
                ],
            },
        )
        assert response.status_code == 200
        readiness = response.json()["readiness"]
        assert readiness["ready"] is False
        assert "publication.no_operation_selected" in readiness["issues"]


class TestPublication:
    def test_publishing_produces_an_immutable_digest_verified_version(
        self, control_plane, support_api_document: str
    ) -> None:
        draft = _register(control_plane, support_api_document)
        published = control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": draft["revision"], "version": "0.1.0"},
        )
        assert published.status_code == 201
        summary = published.json()
        assert summary["tool_count"] == 6

        stored = control_plane.get(
            "/admin/v1/connectors/support-api/versions/0.1.0", headers=ADMIN_HEADERS
        ).json()
        assert verify_digest(stored["document"], summary["document_digest"])
        assert stored["document"]["lifecycle_state"] == "published"
        assert stored["document"]["audit"]["published_at"] is not None

    def test_publication_consumes_the_draft(self, control_plane, support_api_document: str) -> None:
        draft = _register(control_plane, support_api_document)
        control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": draft["revision"], "version": "0.1.0"},
        )
        assert (
            control_plane.get(
                "/admin/v1/connectors/support-api/draft", headers=ADMIN_HEADERS
            ).status_code
            == 404
        )

    def test_republishing_the_same_version_is_refused(
        self, control_plane, support_api_document: str
    ) -> None:
        draft = _register(control_plane, support_api_document)
        control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": draft["revision"], "version": "0.1.0"},
        )
        # A new draft, then an attempt to reuse a version number that is already published.
        redraft = _register(control_plane, support_api_document)
        response = control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": redraft["revision"], "version": "0.1.0"},
        )
        assert response.status_code == 400
        assert "greater than" in response.json()["error"]["message"]

    def test_a_version_must_increase(self, control_plane, support_api_document: str) -> None:
        draft = _register(control_plane, support_api_document)
        control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": draft["revision"], "version": "0.2.0"},
        )
        redraft = _register(control_plane, support_api_document)
        response = control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": redraft["revision"], "version": "0.1.5"},
        )
        assert response.status_code == 400

    def test_a_second_publication_produces_an_independent_version(
        self, control_plane, support_api_document: str
    ) -> None:
        draft = _register(control_plane, support_api_document)
        first = control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": draft["revision"], "version": "0.1.0"},
        ).json()

        redraft = _register(control_plane, support_api_document)
        control_plane.patch(
            "/admin/v1/connectors/support-api/draft",
            headers=ADMIN_HEADERS,
            json={
                "expected_revision": redraft["revision"],
                "operations": [{"operation_key": "get /v1/teams", "selection": "excluded"}],
            },
        )
        second = control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": redraft["revision"] + 1, "version": "0.2.0"},
        ).json()

        assert second["tool_count"] == first["tool_count"] - 1
        assert second["document_digest"] != first["document_digest"]

        # The earlier version is untouched by the later one.
        original = control_plane.get(
            "/admin/v1/connectors/support-api/versions/0.1.0", headers=ADMIN_HEADERS
        ).json()
        assert original["summary"]["document_digest"] == first["document_digest"]

    def test_a_publication_that_is_not_ready_reports_every_blocking_issue(
        self, control_plane
    ) -> None:
        document = (
            "openapi: '3.1.0'\n"
            "info: {title: Bare API, version: '1'}\n"
            "servers: [{url: 'https://api.example.org'}]\n"
            "paths:\n"
            "  /things:\n"
            "    get:\n"
            "      operationId: listThings\n"
            "      responses: {'200': {description: ok}}\n"
        )
        draft = control_plane.post(
            "/admin/v1/connectors",
            headers=ADMIN_HEADERS,
            json={"connector_key": "bare", "document": document},
        ).json()
        assert draft["readiness"]["ready"] is False

        response = control_plane.post(
            "/admin/v1/connectors/bare/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": draft["revision"]},
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "not_ready_for_publication"
        assert any("description_placeholder" in detail["message"] for detail in error["details"])


class TestDeploymentsAndSnapshots:
    def test_a_snapshot_pins_exact_versions_and_verifies_by_digest(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        assert published_snapshot["revision"] == 1
        assert len(published_snapshot["connectors"]) == 1
        assert published_snapshot["connectors"][0]["version"] == "0.1.0"
        assert verify_digest(
            published_snapshot,
            published_snapshot["snapshot_digest"],
            exclude=SNAPSHOT_DIGEST_EXCLUDED,
        )

    def test_snapshot_revisions_increase_and_only_the_newest_is_active(
        self, control_plane, published_snapshot: dict[str, Any], support_api_document: str
    ) -> None:
        redraft = _register(control_plane, support_api_document)
        control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": redraft["revision"], "version": "0.2.0"},
        )
        second = control_plane.post(
            "/admin/v1/deployments/demo-workspace/snapshots",
            headers=ADMIN_HEADERS,
            json={"selections": [{"connector_key": "support-api", "version": "0.2.0"}]},
        ).json()
        assert second["revision"] == 2

        snapshots = control_plane.get(
            "/admin/v1/deployments/demo-workspace/snapshots", headers=ADMIN_HEADERS
        ).json()
        assert [entry["active"] for entry in snapshots] == [False, True]

        served = control_plane.get(
            "/internal/v1/deployments/demo-workspace/snapshot", headers=SERVICE_HEADERS
        ).json()
        assert served["revision"] == 2

    def test_the_earlier_snapshot_remains_byte_identical(
        self, control_plane, published_snapshot: dict[str, Any], support_api_document: str
    ) -> None:
        before = control_plane.get(
            "/admin/v1/deployments/demo-workspace/snapshots/1", headers=ADMIN_HEADERS
        ).json()
        redraft = _register(control_plane, support_api_document)
        control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": redraft["revision"], "version": "0.2.0"},
        )
        control_plane.post(
            "/admin/v1/deployments/demo-workspace/snapshots",
            headers=ADMIN_HEADERS,
            json={"selections": [{"connector_key": "support-api", "version": "0.2.0"}]},
        )
        after = control_plane.get(
            "/admin/v1/deployments/demo-workspace/snapshots/1", headers=ADMIN_HEADERS
        ).json()
        assert after["document"] == before["document"]

    def test_a_disabled_version_cannot_enter_a_new_snapshot(
        self, control_plane, published_snapshot: dict[str, Any]
    ) -> None:
        control_plane.post(
            "/admin/v1/connectors/support-api/versions/0.1.0/disable",
            headers=ADMIN_HEADERS,
            json={"reason": "superseded by a corrected specification"},
        )
        response = control_plane.post(
            "/admin/v1/deployments/demo-workspace/snapshots",
            headers=ADMIN_HEADERS,
            json={"selections": [{"connector_key": "support-api", "version": "0.1.0"}]},
        )
        assert response.status_code == 400
        assert "disabled" in response.json()["error"]["message"]

    def test_disabling_changes_availability_not_content(
        self, control_plane, published_snapshot: dict[str, Any]
    ) -> None:
        before = control_plane.get(
            "/admin/v1/connectors/support-api/versions/0.1.0", headers=ADMIN_HEADERS
        ).json()
        control_plane.post(
            "/admin/v1/connectors/support-api/versions/0.1.0/disable",
            headers=ADMIN_HEADERS,
            json={"reason": "superseded"},
        )
        after = control_plane.get(
            "/admin/v1/connectors/support-api/versions/0.1.0", headers=ADMIN_HEADERS
        ).json()
        assert after["document"] == before["document"]
        assert after["summary"]["disabled"] is True
        assert verify_digest(after["document"], after["summary"]["document_digest"])

    def test_a_snapshot_cannot_pin_two_versions_of_one_connector(
        self, control_plane, published_snapshot: dict[str, Any], support_api_document: str
    ) -> None:
        redraft = _register(control_plane, support_api_document)
        control_plane.post(
            "/admin/v1/connectors/support-api/publish",
            headers=ADMIN_HEADERS,
            json={"expected_revision": redraft["revision"], "version": "0.2.0"},
        )
        response = control_plane.post(
            "/admin/v1/deployments/demo-workspace/snapshots",
            headers=ADMIN_HEADERS,
            json={
                "selections": [
                    {"connector_key": "support-api", "version": "0.1.0"},
                    {"connector_key": "support-api", "version": "0.2.0"},
                ]
            },
        )
        assert response.status_code == 400

    def test_conditional_reads_avoid_re_sending_an_unchanged_snapshot(
        self, control_plane, published_snapshot: dict[str, Any]
    ) -> None:
        first = control_plane.get(
            "/internal/v1/deployments/demo-workspace/snapshot", headers=SERVICE_HEADERS
        )
        etag = first.headers["etag"]
        again = control_plane.get(
            "/internal/v1/deployments/demo-workspace/snapshot",
            headers={**SERVICE_HEADERS, "if-none-match": etag},
        )
        assert again.status_code == 304
        assert not again.content

    def test_a_deployment_without_a_snapshot_reports_it_as_unavailable(self, control_plane) -> None:
        control_plane.post(
            "/admin/v1/deployments",
            headers=ADMIN_HEADERS,
            json={"deployment_key": "empty-workspace", "display_name": "Empty"},
        )
        response = control_plane.get(
            "/internal/v1/deployments/empty-workspace/snapshot", headers=SERVICE_HEADERS
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "snapshot_unavailable"
