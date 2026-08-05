"""Builders for contract objects used across the test suite.

Every required field is supplied explicitly. That is slightly verbose, and deliberately so:
a factory that quietly defaulted ``policy`` would hide exactly the kind of drift
``tests/contract`` exists to catch.
"""

from __future__ import annotations

from typing import Any

from toollayer_contracts.models import (
    ArgumentBinding,
    ToolAccessPolicy,
    ToolDefinition,
    ToolOperation,
    ToolPolicy,
    ToolProvenance,
)

__all__ = ["closed_schema", "make_operation", "make_policy", "make_provenance", "make_tool"]


def closed_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def make_policy(
    *,
    effect_class: str = "read",
    requires_confirmation: bool = False,
    access: ToolAccessPolicy | None = None,
) -> ToolPolicy:
    return ToolPolicy(
        effect_class=effect_class,  # type: ignore[arg-type]
        requires_confirmation=requires_confirmation,
        access=access or ToolAccessPolicy.public(),
    )


def make_provenance(
    *,
    source_path: str = "/v1/things",
    source_method: str = "get",
    source_operation_id: str | None = None,
    tags: tuple[str, ...] = (),
    deprecated: bool = False,
    description_origin: str = "source",
) -> ToolProvenance:
    return ToolProvenance(
        source_operation_id=source_operation_id,
        source_path=source_path,
        source_method=source_method,  # type: ignore[arg-type]
        tags=tags,
        deprecated=deprecated,
        description_origin=description_origin,  # type: ignore[arg-type]
    )


def make_operation(
    *,
    method: str = "GET",
    path_template: str = "/v1/things",
    bindings: tuple[ArgumentBinding, ...] = (),
    request_body_media_type: str | None = None,
) -> ToolOperation:
    return ToolOperation(
        protocol="http",
        method=method,  # type: ignore[arg-type]
        path_template=path_template,
        bindings=bindings,
        request_body_media_type=request_body_media_type,  # type: ignore[arg-type]
    )


def make_tool(**overrides: Any) -> ToolDefinition:
    defaults: dict[str, Any] = {
        "tool_name": "list_things",
        "display_name": "List things",
        "description": "List the things.",
        "input_schema": closed_schema({"status": {"type": "string"}}),
        "operation": make_operation(
            bindings=(
                ArgumentBinding(argument_pointer="/status", target="query", target_name="status"),
            )
        ),
        "policy": make_policy(),
        "provenance": make_provenance(),
    }
    defaults.update(overrides)
    return ToolDefinition(**defaults)
