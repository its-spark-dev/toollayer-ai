"""Contract tests: the two services must keep agreeing about the shared documents.

The Control Plane and the Runtime never import each other. The only thing holding them
together is the contract package, so these tests check the things that would let the two
drift apart without any other test failing:

* the normative JSON Schemas and the Pydantic models describe the same documents;
* a document the Control Plane produces is one the Runtime accepts;
* the provider adapters do not weaken what the canonical definition asserts;
* a version mismatch is refused rather than half-understood.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError
from tests.conftest import ADMIN_HEADERS, SERVICE_HEADERS

from toollayer_contracts import (
    CONTRACT_VERSION,
    ConnectorDefinition,
    DeploymentSnapshot,
    ToolDefinition,
    load_schema,
    schema_names,
    validate_connector_definition,
    validate_deployment_snapshot,
    validate_error_envelope,
    validate_tool_definition,
    verify_digest,
)
from toollayer_contracts.adapters import SUPPORTED_PROVIDERS, get_adapter
from toollayer_contracts.adapters.openai_adapter import normalize_provider_arguments
from toollayer_contracts.errors import ContractViolationError, ErrorEnvelope

pytestmark = pytest.mark.contract


class TestSchemas:
    def test_every_packaged_schema_is_a_valid_draft_2020_12_schema(self) -> None:
        for name in schema_names():
            Draft202012Validator.check_schema(load_schema(name))

    def test_every_schema_declares_a_stable_identifier(self) -> None:
        for name in schema_names():
            schema = load_schema(name)
            assert str(schema["$id"]).startswith("https://contracts.toollayer.example/v1/")

    def test_the_error_envelope_matches_what_the_error_type_produces(self) -> None:
        envelope = ErrorEnvelope(
            code="argument_validation_failed",
            message="the proposed arguments do not satisfy the tool's input schema",
            pointer="/limit",
            request_id="abc123",
        )
        validate_error_envelope(envelope.to_dict())


class TestModelSchemaAgreement:
    """The models and the schemas must accept and reject the same documents.

    They are two encodings of one contract. If they disagree, a document can pass in-process
    validation and fail for a consumer in another language — or the reverse, which is worse.
    """

    def test_a_model_built_tool_satisfies_the_schema(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        for connector in published_snapshot["connectors"]:
            for tool in connector["tools"]:
                validate_tool_definition(tool)
                round_tripped = ToolDefinition.model_validate(tool).model_dump(mode="json")
                validate_tool_definition(round_tripped)
                assert round_tripped == tool

    def test_a_connector_round_trips_through_the_model_unchanged(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        for connector in published_snapshot["connectors"]:
            round_tripped = ConnectorDefinition.model_validate(connector).model_dump(mode="json")
            assert round_tripped == connector
            validate_connector_definition(round_tripped)

    def test_a_snapshot_round_trips_through_the_model_unchanged(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        round_tripped = DeploymentSnapshot.model_validate(published_snapshot).model_dump(
            mode="json"
        )
        assert round_tripped == published_snapshot
        validate_deployment_snapshot(round_tripped)

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda t: t.update(tool_name="Not Canonical"), id="bad-tool-name"),
            pytest.param(
                lambda t: t["input_schema"].update(additionalProperties=True), id="open-schema"
            ),
            pytest.param(lambda t: t.pop("policy"), id="missing-policy"),
            pytest.param(lambda t: t.update(surprise=1), id="unknown-field"),
            pytest.param(
                lambda t: t["operation"].update(path_template="https://elsewhere.example"),
                id="absolute-path",
            ),
        ],
    )
    def test_schema_and_model_both_reject_the_same_malformed_tool(
        self, published_snapshot: dict[str, Any], mutate
    ) -> None:
        tool = ToolDefinition.model_validate(
            published_snapshot["connectors"][0]["tools"][0]
        ).model_dump(mode="json")
        mutate(tool)

        with pytest.raises(ContractViolationError):
            validate_tool_definition(tool)
        with pytest.raises(PydanticValidationError):
            ToolDefinition.model_validate(tool)


class TestCrossServiceCompatibility:
    def test_the_runtime_accepts_what_the_control_plane_published(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        from runtime_service.snapshot import load_snapshot_document

        loaded = load_snapshot_document(published_snapshot)
        assert loaded.revision == 1
        assert loaded.tools_by_name

    def test_the_snapshot_verifies_against_its_own_digest(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        assert verify_digest(
            published_snapshot,
            published_snapshot["snapshot_digest"],
            exclude=("snapshot_id", "snapshot_digest"),
        )

    def test_the_runtime_refuses_a_snapshot_whose_content_was_altered(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        from runtime_service.snapshot import SnapshotIntegrityError, load_snapshot_document

        tampered = dict(published_snapshot)
        tampered["connectors"] = [dict(connector) for connector in tampered["connectors"]]
        tampered["connectors"][0]["runtime"] = {
            **tampered["connectors"][0]["runtime"],
            "base_url": "https://attacker.test",
        }
        with pytest.raises(SnapshotIntegrityError, match="digest"):
            load_snapshot_document(tampered)

    def test_the_runtime_refuses_a_different_contract_major_version(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        from runtime_service.snapshot import load_snapshot_document

        future = {**published_snapshot, "contract_version": "2.0.0"}
        with pytest.raises(Exception, match="major"):
            load_snapshot_document(future)

    def test_the_published_document_declares_this_build_s_contract_version(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        assert published_snapshot["contract_version"] == CONTRACT_VERSION
        for connector in published_snapshot["connectors"]:
            assert connector["contract_version"] == CONTRACT_VERSION


class TestProviderAdapters:
    def test_both_adapters_project_every_published_tool(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        tools = ConnectorDefinition.model_validate(published_snapshot["connectors"][0]).tools
        for provider in SUPPORTED_PROVIDERS:
            projection = get_adapter(provider).project(tools)
            assert projection.complete, projection.diagnostics
            assert len(projection.tools) == len(tools)

    def test_projection_does_not_mutate_the_canonical_definition(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        connector = ConnectorDefinition.model_validate(published_snapshot["connectors"][0])
        before = [tool.model_dump(mode="json") for tool in connector.tools]
        for provider in SUPPORTED_PROVIDERS:
            get_adapter(provider).project(connector.tools)
        after = [tool.model_dump(mode="json") for tool in connector.tools]
        assert before == after

    def test_the_anthropic_projection_preserves_optionality_exactly(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        connector = ConnectorDefinition.model_validate(published_snapshot["connectors"][0])
        tool = connector.tool("list_support_tickets")
        assert tool is not None
        projected = get_adapter("anthropic").project_tool(tool)
        assert projected["input_schema"] == tool.input_schema

    def test_the_openai_strict_projection_is_reversible(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        """Strict mode forces every property to be required, so the runtime must be able to
        undo it. If it were not reversible, the normalization would silently change what the
        tool accepts."""
        connector = ConnectorDefinition.model_validate(published_snapshot["connectors"][0])
        tool = connector.tool("list_support_tickets")
        assert tool is not None

        projected = get_adapter("openai").project_tool(tool)
        parameters = projected["function"]["parameters"]
        assert set(parameters["required"]) == set(parameters["properties"])

        # What a model would emit under strict mode: every property present, absent ones null.
        model_output = dict.fromkeys(parameters["properties"])
        model_output["status"] = "open"

        normalized = normalize_provider_arguments(tool.input_schema, model_output)
        assert normalized == {"status": "open"}
        Draft202012Validator(tool.input_schema).validate(normalized)

    def test_normalization_keeps_an_undeclared_argument_so_validation_can_reject_it(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        connector = ConnectorDefinition.model_validate(published_snapshot["connectors"][0])
        tool = connector.tool("list_support_tickets")
        assert tool is not None
        normalized = normalize_provider_arguments(tool.input_schema, {"smuggled": None})
        assert "smuggled" in normalized
        assert list(Draft202012Validator(tool.input_schema).iter_errors(normalized))

    def test_a_widened_enum_still_admits_the_null_placeholder(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        connector = ConnectorDefinition.model_validate(published_snapshot["connectors"][0])
        tool = connector.tool("list_support_tickets")
        assert tool is not None
        parameters = get_adapter("openai").project_tool(tool)["function"]["parameters"]
        status = parameters["properties"]["status"]
        assert "null" in status["type"] and None in status["enum"]
        Draft202012Validator(parameters).validate(dict.fromkeys(parameters["properties"]))


class TestInternalApiShape:
    def test_the_internal_read_api_serves_a_document_the_contract_accepts(
        self, control_plane, published_snapshot: dict[str, Any]
    ) -> None:
        response = control_plane.get(
            "/internal/v1/deployments/demo-workspace/snapshot", headers=SERVICE_HEADERS
        )
        assert response.status_code == 200
        validate_deployment_snapshot(response.json())
        assert response.headers["etag"] == f'"{published_snapshot["snapshot_digest"]}"'

    def test_an_admin_token_does_not_authorize_the_internal_api(
        self, control_plane, published_snapshot: dict[str, Any]
    ) -> None:
        response = control_plane.get(
            "/internal/v1/deployments/demo-workspace/snapshot",
            headers={"x-toollayer-service-token": ADMIN_HEADERS["x-toollayer-admin-token"]},
        )
        assert response.status_code == 401

    def test_a_service_token_does_not_authorize_the_admin_api(self, control_plane) -> None:
        response = control_plane.get(
            "/admin/v1/connectors",
            headers={"x-toollayer-admin-token": SERVICE_HEADERS["x-toollayer-service-token"]},
        )
        assert response.status_code == 401

    def test_every_failure_uses_the_shared_error_envelope(self, control_plane) -> None:
        for request in (
            lambda: control_plane.get("/admin/v1/connectors"),
            lambda: control_plane.get("/admin/v1/connectors/missing", headers=ADMIN_HEADERS),
            lambda: control_plane.post("/admin/v1/connectors", headers=ADMIN_HEADERS, json={}),
        ):
            response = request()
            assert response.status_code >= 400
            validate_error_envelope(response.json())
