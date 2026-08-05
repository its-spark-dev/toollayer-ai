"""Conversion behavior: what the converter produces, and what it refuses.

The refusals matter as much as the conversions. A converter that quietly approximates an
unsupported feature produces a tool that a model will call and that will then behave
differently from what the source document described.
"""

from __future__ import annotations

import json

import pytest

from toollayer_openapi import (
    DocumentTooLargeError,
    InvalidDocumentError,
    SourceLimits,
    UnsupportedFeatureError,
    analyze_document,
    convert_operation,
    derive_tool_name,
    load_document,
    normalize_tool_name,
)

pytestmark = pytest.mark.unit


def _document(paths: dict, **extra) -> bytes:
    return json.dumps({"openapi": "3.1.0", "info": {"title": "T", "version": "1"}, "paths": paths, **extra}).encode()


def _operation(**overrides) -> dict:
    base = {"summary": "Do a thing", "responses": {"200": {"description": "ok"}}}
    base.update(overrides)
    return base


class TestLoading:
    def test_accepts_json_and_yaml_by_content_not_extension(self) -> None:
        as_json = load_document(_document({"/x": {"get": _operation()}}), filename="spec.yaml")
        assert as_json.source_format == "json"

        as_yaml = load_document(
            b"openapi: 3.0.3\ninfo:\n  title: T\n  version: '1'\npaths: {}\n",
            filename="spec.json",
        )
        assert as_yaml.source_format == "yaml"

    def test_records_the_digest_of_the_exact_uploaded_bytes(self) -> None:
        raw = _document({"/x": {"get": _operation()}})
        loaded = load_document(raw, filename="s.json")
        assert loaded.digest.startswith("sha256:")
        assert loaded.byte_length == len(raw)
        # Re-loading identical bytes must produce an identical digest, or the published
        # provenance would not be reproducible.
        assert load_document(raw, filename="other-name.json").digest == loaded.digest

    def test_rejects_duplicate_json_keys(self) -> None:
        raw = b'{"openapi":"3.1.0","paths":{},"paths":{"/a":{}}}'
        with pytest.raises(InvalidDocumentError, match="duplicate object key"):
            load_document(raw, filename="s.json")

    def test_rejects_duplicate_yaml_keys(self) -> None:
        raw = b"openapi: '3.1.0'\npaths: {}\npaths:\n  /a: {}\n"
        with pytest.raises(InvalidDocumentError, match="duplicate mapping key"):
            load_document(raw, filename="s.yaml")

    def test_rejects_a_byte_order_mark(self) -> None:
        with pytest.raises(InvalidDocumentError, match="byte order mark"):
            load_document(b"\xef\xbb\xbf{}", filename="s.json")

    def test_rejects_a_document_over_the_byte_limit(self) -> None:
        with pytest.raises(DocumentTooLargeError):
            load_document(
                _document({"/x": {"get": _operation()}}),
                filename="s.json",
                limits=SourceLimits(max_bytes=10),
            )

    def test_rejects_a_document_that_nests_too_deeply(self) -> None:
        nested: dict = {}
        cursor = nested
        for _ in range(80):
            cursor["child"] = {}
            cursor = cursor["child"]
        raw = json.dumps({"openapi": "3.1.0", "paths": {}, "x-deep": nested}).encode()
        with pytest.raises(DocumentTooLargeError, match="nests too deeply"):
            load_document(raw, filename="s.json")

    def test_rejects_a_swagger_2_document(self) -> None:
        with pytest.raises(InvalidDocumentError, match="'openapi' version"):
            load_document(b'{"swagger":"2.0","paths":{}}', filename="s.json")

    def test_rejects_an_unsupported_openapi_major_version(self) -> None:
        with pytest.raises(InvalidDocumentError, match="OpenAPI 3"):
            load_document(b'{"openapi":"4.0.0","paths":{}}', filename="s.json")


class TestParameterConversion:
    def test_converts_path_and_query_parameters_into_a_closed_schema(self) -> None:
        tool = convert_operation(
            path="/v1/items/{item_id}",
            method="get",
            path_item={},
            operation=_operation(
                operationId="getItem",
                parameters=[
                    {"name": "item_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {
                        "name": "verbose",
                        "in": "query",
                        "schema": {"type": "boolean"},
                        "description": "Include detail.",
                    },
                ],
            ),
        )
        assert tool.tool_name == "get_item"
        assert tool.input_schema["additionalProperties"] is False
        assert tool.input_schema["required"] == ["item_id"]
        assert set(tool.input_schema["properties"]) == {"item_id", "verbose"}
        assert tool.operation.method == "GET"
        assert {(b.target, b.target_name) for b in tool.operation.bindings} == {
            ("path", "item_id"),
            ("query", "verbose"),
        }

    def test_operation_parameters_override_path_item_parameters(self) -> None:
        tool = convert_operation(
            path="/v1/items",
            method="get",
            path_item={
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 10}}
                ]
            },
            operation=_operation(
                parameters=[
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 99}}
                ]
            ),
        )
        assert tool.input_schema["properties"]["limit"]["maximum"] == 99

    def test_preserves_enum_default_and_bounds(self) -> None:
        tool = convert_operation(
            path="/v1/items",
            method="get",
            path_item={},
            operation=_operation(
                parameters=[
                    {
                        "name": "state",
                        "in": "query",
                        "schema": {"type": "string", "enum": ["a", "b"], "default": "a"},
                    },
                    {
                        "name": "size",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                ]
            ),
        )
        state = tool.input_schema["properties"]["state"]
        assert state["enum"] == ["a", "b"] and state["default"] == "a"
        assert tool.input_schema["properties"]["size"]["minimum"] == 1

    @pytest.mark.parametrize(
        ("parameter", "expected"),
        [
            ({"name": "h", "in": "header", "schema": {"type": "string"}}, UnsupportedFeatureError),
            ({"name": "c", "in": "cookie", "schema": {"type": "string"}}, UnsupportedFeatureError),
            (
                {"name": "q", "in": "query", "style": "deepObject", "schema": {"type": "string"}},
                UnsupportedFeatureError,
            ),
            (
                {"name": "q", "in": "query", "allowReserved": True, "schema": {"type": "string"}},
                UnsupportedFeatureError,
            ),
            (
                {"name": "q", "in": "query", "schema": {"oneOf": [{"type": "string"}]}},
                UnsupportedFeatureError,
            ),
            ({"name": "q", "in": "query", "schema": {}}, UnsupportedFeatureError),
            ({"name": "q", "in": "query"}, InvalidDocumentError),
        ],
    )
    def test_refuses_features_it_will_not_approximate(self, parameter, expected) -> None:
        with pytest.raises(expected):
            convert_operation(
                path="/v1/items",
                method="get",
                path_item={},
                operation=_operation(parameters=[parameter]),
            )

    def test_rejects_a_path_parameter_that_is_not_required(self) -> None:
        with pytest.raises(InvalidDocumentError, match="required: true"):
            convert_operation(
                path="/v1/items/{id}",
                method="get",
                path_item={},
                operation=_operation(
                    parameters=[{"name": "id", "in": "path", "schema": {"type": "string"}}]
                ),
            )

    def test_rejects_a_placeholder_without_a_parameter(self) -> None:
        with pytest.raises(InvalidDocumentError, match="no matching path parameter"):
            convert_operation(
                path="/v1/items/{id}", method="get", path_item={}, operation=_operation()
            )

    def test_rejects_a_name_used_in_two_locations(self) -> None:
        with pytest.raises(UnsupportedFeatureError, match="more than one location"):
            convert_operation(
                path="/v1/items/{id}",
                method="get",
                path_item={},
                operation=_operation(
                    parameters=[
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "id", "in": "query", "schema": {"type": "string"}},
                    ]
                ),
            )

    def test_rejects_a_per_operation_server_override(self) -> None:
        with pytest.raises(UnsupportedFeatureError, match="server overrides"):
            convert_operation(
                path="/v1/items",
                method="get",
                path_item={},
                operation=_operation(servers=[{"url": "https://elsewhere.example"}]),
            )


class TestRequestBodyConversion:
    def test_converts_a_json_object_body_into_one_nested_argument(self) -> None:
        tool = convert_operation(
            path="/v1/items",
            method="post",
            path_item={},
            operation=_operation(
                operationId="createItem",
                requestBody={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "note": {"type": "string"},
                                },
                            }
                        }
                    },
                },
            ),
        )
        assert "body" in tool.input_schema["properties"]
        assert tool.input_schema["required"] == ["body"]
        assert tool.operation.request_body_media_type == "application/json"
        # One binding per field, so the executor writes exactly the declared fields.
        assert {b.target_name for b in tool.operation.bindings if b.target == "body"} == {
            "/name",
            "/note",
        }

    def test_derives_a_write_policy_from_the_method(self) -> None:
        tool = convert_operation(
            path="/v1/items/{id}",
            method="delete",
            path_item={},
            operation=_operation(
                parameters=[{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]
            ),
        )
        assert tool.policy.effect_class == "destructive"
        assert tool.policy.requires_confirmation is True

    def test_refuses_a_non_json_body(self) -> None:
        with pytest.raises(UnsupportedFeatureError, match="application/json"):
            convert_operation(
                path="/v1/items",
                method="post",
                path_item={},
                operation=_operation(
                    requestBody={"content": {"application/xml": {"schema": {"type": "object"}}}}
                ),
            )


class TestNaming:
    @pytest.mark.parametrize(
        ("operation_id", "expected"),
        [
            ("listSupportTickets", "list_support_tickets"),
            ("Get_Ticket", "get_ticket"),
            ("HTTPServerInfo", "http_server_info"),
            ("123start", "op_123start"),
        ],
    )
    def test_normalizes_operation_ids(self, operation_id: str, expected: str) -> None:
        assert normalize_tool_name(operation_id) == expected

    @pytest.mark.parametrize(
        ("method", "path", "expected"),
        [
            ("get", "/v1/tickets/{ticket_id}", "get_tickets_by_ticket_id"),
            ("post", "/v1/tickets", "create_tickets"),
            ("delete", "/orders/{id}/lines/{line_id}", "delete_orders_by_id_lines_by_line_id"),
            ("get", "/", "get_root"),
        ],
    )
    def test_derives_names_from_method_and_path(self, method, path, expected) -> None:
        assert derive_tool_name(method, path) == expected

    def test_bounds_a_long_name_without_losing_uniqueness(self) -> None:
        long_a = normalize_tool_name("a" * 90)
        long_b = normalize_tool_name("a" * 91)
        assert len(long_a) <= 64 and len(long_b) <= 64
        assert long_a != long_b

    def test_a_derived_name_never_claims_source_provenance(self) -> None:
        tool = convert_operation(
            path="/v1/items", method="get", path_item={}, operation=_operation()
        )
        assert tool.tool_name == "get_items"
        assert tool.provenance.source_operation_id is None


class TestAnalysis:
    def test_one_bad_operation_does_not_fail_the_document(self) -> None:
        raw = _document(
            {
                "/good": {"get": _operation(operationId="good")},
                "/bad": {
                    "get": _operation(
                        operationId="bad",
                        parameters=[{"name": "h", "in": "header", "schema": {"type": "string"}}],
                    )
                },
            },
            servers=[{"url": "https://api.example.org"}],
        )
        result = analyze_document(load_document(raw, filename="s.json"))
        assert [tool.tool_name for tool in result.tools] == ["good"]
        codes = {diagnostic.code for diagnostic in result.all_diagnostics}
        assert "unsupported_spec_feature" in codes

    def test_reports_a_tool_name_collision_instead_of_renaming(self) -> None:
        raw = _document(
            {
                "/a": {"get": _operation(operationId="listThings")},
                "/b": {"get": _operation(operationId="list_things")},
            },
            servers=[{"url": "https://api.example.org"}],
        )
        result = analyze_document(load_document(raw, filename="s.json"))
        assert len(result.tools) == 1
        assert any(d.code == "tool_name_collision" for d in result.all_diagnostics)

    def test_a_missing_description_is_flagged_rather_than_invented(self) -> None:
        raw = _document(
            {"/a": {"get": {"operationId": "a", "responses": {"200": {"description": "ok"}}}}},
            servers=[{"url": "https://api.example.org"}],
        )
        result = analyze_document(load_document(raw, filename="s.json"))
        assert result.tools[0].provenance.description_origin == "generated"
        assert any(d.code == "description_generated" for d in result.all_diagnostics)

    def test_an_explicit_base_url_overrides_the_declared_server(self) -> None:
        raw = _document(
            {"/a": {"get": _operation(operationId="a")}},
            servers=[{"url": "https://published.example.org"}],
        )
        result = analyze_document(
            load_document(raw, filename="s.json"), base_url_override="https://internal.example.org"
        )
        assert result.base_url == "https://internal.example.org"
