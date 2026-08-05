"""Schema validation for ToolLayer contract documents.

The schemas reference each other (a connector embeds tools; a snapshot embeds connectors),
so every validator is built against a registry holding all of them. Nothing is fetched over
the network — the registry is populated from the files packaged with this module, and a
document that references anything else fails rather than triggering a lookup.

Failure messages carry a JSON Pointer and the failing keyword. They never carry the value
that failed, because these documents are untrusted input and error text ends up in logs.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from functools import cache, lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from toollayer_contracts.errors import ContractViolationError, ErrorDetail
from toollayer_contracts.version import IncompatibleContractVersionError, require_supported

__all__ = [
    "CONNECTOR_SCHEMA",
    "DEPLOYMENT_SNAPSHOT_SCHEMA",
    "ERROR_ENVELOPE_SCHEMA",
    "TOOL_SCHEMA",
    "load_schema",
    "schema_names",
    "validate_connector_definition",
    "validate_deployment_snapshot",
    "validate_error_envelope",
    "validate_tool_definition",
    "validate_tool_input_schema",
]

TOOL_SCHEMA = "tool-definition.schema.json"
CONNECTOR_SCHEMA = "connector-definition.schema.json"
DEPLOYMENT_SNAPSHOT_SCHEMA = "deployment-snapshot.schema.json"
TOOL_EXECUTION_SCHEMA = "tool-execution.schema.json"
ERROR_ENVELOPE_SCHEMA = "error-envelope.schema.json"

_SCHEMA_NAMES: tuple[str, ...] = (
    TOOL_SCHEMA,
    CONNECTOR_SCHEMA,
    DEPLOYMENT_SNAPSHOT_SCHEMA,
    TOOL_EXECUTION_SCHEMA,
    ERROR_ENVELOPE_SCHEMA,
)


def schema_names() -> tuple[str, ...]:
    """Return every packaged contract schema file name."""
    return _SCHEMA_NAMES


@cache
def load_schema(name: str) -> dict[str, Any]:
    """Load one packaged contract schema by file name."""
    if name not in _SCHEMA_NAMES:
        raise KeyError(f"unknown contract schema: {name}")
    text = resources.files("toollayer_contracts.schemas").joinpath(name).read_text("utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema


@lru_cache(maxsize=1)
def _registry() -> Registry[Any]:
    """Build a resolver registry containing every packaged schema.

    Sibling schemas are registered under both their absolute ``$id`` and their bare file
    name, because the schemas reference each other by relative file name.
    """
    registry: Registry[Any] = Registry()
    for name in _SCHEMA_NAMES:
        schema = load_schema(name)
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = registry.with_resource(str(schema["$id"]), resource)
        registry = registry.with_resource(name, resource)
    return registry


@cache
def _validator(name: str) -> Draft202012Validator:
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=_registry(), format_checker=FormatChecker())


def _pointer(path: Iterable[object]) -> str:
    return "".join(f"/{str(part).replace('~', '~0').replace('/', '~1')}" for part in path)


def _detail(error: JsonSchemaValidationError) -> ErrorDetail:
    keyword = error.validator if isinstance(error.validator, str) else "schema"
    return ErrorDetail(
        code=f"schema.{keyword}",
        message=f"the document violates the {keyword!r} constraint",
        pointer=_pointer(error.absolute_path) or "",
    )


def _validate(document: object, *, schema_name: str, label: str) -> None:
    errors: Sequence[JsonSchemaValidationError] = sorted(
        _validator(schema_name).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    raise ContractViolationError(
        f"the {label} does not satisfy its contract",
        pointer=_pointer(first.absolute_path) or "",
        details=tuple(_detail(error) for error in errors[:50]),
    )


def _require_contract_version(document: object, label: str) -> None:
    if not isinstance(document, dict):
        raise ContractViolationError(f"the {label} must be a JSON object", pointer="")
    try:
        require_supported(document.get("contract_version"))
    except IncompatibleContractVersionError as exc:
        raise ContractViolationError(str(exc), pointer="/contract_version") from None


def validate_tool_definition(document: object) -> None:
    """Validate one tool definition."""
    _validate(document, schema_name=TOOL_SCHEMA, label="tool definition")
    validate_tool_input_schema(
        document["input_schema"] if isinstance(document, dict) else None,
        pointer="/input_schema",
    )


def validate_connector_definition(document: object) -> None:
    """Validate one connector definition, including semantics the schema cannot express."""
    _require_contract_version(document, "connector definition")
    _validate(document, schema_name=CONNECTOR_SCHEMA, label="connector definition")
    assert isinstance(document, dict)

    seen: set[str] = set()
    for index, tool in enumerate(document["tools"]):
        name = tool["tool_name"]
        if name in seen:
            raise ContractViolationError(
                "tool names must be unique within a connector version",
                pointer=f"/tools/{index}/tool_name",
            )
        seen.add(name)
        validate_tool_input_schema(tool["input_schema"], pointer=f"/tools/{index}/input_schema")


def validate_deployment_snapshot(document: object) -> None:
    """Validate one deployment snapshot, including per-connector semantics."""
    _require_contract_version(document, "deployment snapshot")
    _validate(document, schema_name=DEPLOYMENT_SNAPSHOT_SCHEMA, label="deployment snapshot")
    assert isinstance(document, dict)

    seen: set[str] = set()
    for index, connector in enumerate(document["connectors"]):
        key = connector["connector_key"]
        if key in seen:
            raise ContractViolationError(
                "a snapshot may pin only one version per connector",
                pointer=f"/connectors/{index}/connector_key",
            )
        seen.add(key)
        if connector["lifecycle_state"] != "published":
            raise ContractViolationError(
                "a snapshot may contain only published connector versions",
                pointer=f"/connectors/{index}/lifecycle_state",
            )
        validate_connector_definition(connector)


def validate_error_envelope(document: object) -> None:
    """Validate one error envelope."""
    _validate(document, schema_name=ERROR_ENVELOPE_SCHEMA, label="error envelope")


def validate_tool_input_schema(document: object, *, pointer: str = "") -> None:
    """Check that a tool's ``input_schema`` is itself a usable Draft 2020-12 schema.

    A tool input schema is both data (it lives inside a contract document) and code (the
    runtime executes it against model output). Validating the outer document proves only
    the first; this proves the second, before anything tries to validate arguments with it.
    """
    if not isinstance(document, dict):
        raise ContractViolationError("input_schema must be an object", pointer=pointer)
    try:
        Draft202012Validator.check_schema(document)
    except Exception:
        raise ContractViolationError(
            "input_schema is not a valid Draft 2020-12 schema", pointer=pointer
        ) from None
