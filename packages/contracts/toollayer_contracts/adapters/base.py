"""The provider adapter boundary.

The canonical tool definition is this project's source of truth. A provider's tool format
is an *output projection* of it, produced on demand and never persisted as connector state.
That direction matters: if the canonical definition were derived from a provider format,
the project would inherit whichever provider it was built against.

An adapter has one hard obligation: **never widen what the canonical definition asserts.**
If a constraint cannot be expressed in the target format, the adapter fails with a stable
diagnostic instead of dropping the constraint. A silently dropped ``maxLength`` is a
validation hole that only shows up when someone exploits it.

Perfect cross-provider portability is not claimed. Providers differ in what their schema
subsets accept, so a tool that projects cleanly for one provider may be rejected for
another. That is reported per tool, and the sibling tools stay usable.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from toollayer_contracts.models import ToolDefinition

__all__ = [
    "AdapterDiagnostic",
    "AdapterError",
    "ProviderAdapter",
    "ProviderProjection",
    "SUPPORTED_SCHEMA_KEYWORDS",
    "walk_schema",
]

#: The Draft 2020-12 subset the adapters are audited to project without loss. Anything
#: outside this set fails closed rather than being dropped from the projection.
SUPPORTED_SCHEMA_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "$schema",
        "additionalProperties",
        "description",
        "default",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "type",
    }
)

_SUPPORTED_TYPES: Final[frozenset[str]] = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)

MAX_NESTING_DEPTH: Final = 8
MAX_TOTAL_PROPERTIES: Final = 500


@dataclass(frozen=True, slots=True)
class AdapterDiagnostic:
    """A stable, value-free reason one tool could not be projected."""

    tool_name: str
    code: str
    message: str
    pointer: str = ""


class AdapterError(ValueError):
    """One tool cannot be projected into a provider format without loss."""

    def __init__(self, code: str, message: str, pointer: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.pointer = pointer


@dataclass(frozen=True, slots=True)
class ProviderProjection:
    """The result of projecting a set of tools for one provider.

    ``tools`` holds the payloads the caller sends to the provider. ``diagnostics`` holds
    one entry per tool that was excluded. A partially projectable connector is normal and
    is not an error: the usable tools stay usable.
    """

    provider: str
    tools: tuple[dict[str, Any], ...]
    diagnostics: tuple[AdapterDiagnostic, ...]

    @property
    def complete(self) -> bool:
        return not self.diagnostics


def walk_schema(schema: dict[str, Any], pointer: str = "") -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield ``(pointer, subschema)`` for a schema and every nested subschema."""
    yield pointer, schema
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, value in properties.items():
            if isinstance(value, dict):
                escaped = str(name).replace("~", "~0").replace("/", "~1")
                yield from walk_schema(value, f"{pointer}/properties/{escaped}")
    items = schema.get("items")
    if isinstance(items, dict):
        yield from walk_schema(items, f"{pointer}/items")


def assert_projectable(schema: dict[str, Any]) -> None:
    """Reject a schema this project has not audited for lossless projection.

    Shared by every adapter so that "supported" means the same thing everywhere. A
    provider-specific adapter may narrow this further, but never widen it.
    """
    total_properties = 0
    for pointer, subschema in walk_schema(schema):
        depth = pointer.count("/properties/") + pointer.count("/items")
        if depth > MAX_NESTING_DEPTH:
            raise AdapterError(
                "schema_nesting_limit_exceeded",
                "the input schema nests deeper than provider projection supports",
                pointer,
            )
        unsupported = sorted(set(subschema) - SUPPORTED_SCHEMA_KEYWORDS)
        if unsupported:
            raise AdapterError(
                "unsupported_schema_keyword",
                f"the input schema uses the unsupported keyword {unsupported[0]!r}",
                pointer,
            )
        declared_type = subschema.get("type")
        if declared_type is not None and (
            not isinstance(declared_type, str) or declared_type not in _SUPPORTED_TYPES
        ):
            raise AdapterError(
                "unsupported_schema_type",
                "the input schema uses a type providers cannot be given losslessly",
                pointer,
            )
        if declared_type == "object":
            if subschema.get("additionalProperties") is not False:
                raise AdapterError(
                    "open_object_unsupported",
                    "open object schemas cannot be projected without weakening them",
                    pointer,
                )
            properties = subschema.get("properties")
            if isinstance(properties, dict):
                total_properties += len(properties)
    if total_properties > MAX_TOTAL_PROPERTIES:
        raise AdapterError(
            "schema_property_limit_exceeded",
            "the input schema declares more properties than provider projection supports",
            "",
        )


class ProviderAdapter(ABC):
    """Translate canonical tool definitions into one provider's tool format."""

    #: Stable provider identifier used in API paths and diagnostics.
    provider: str

    @abstractmethod
    def project_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        """Project one tool, or raise ``AdapterError`` if it cannot be done losslessly."""

    def project(self, tools: Sequence[ToolDefinition]) -> ProviderProjection:
        """Project a set of tools, isolating per-tool failures."""
        payloads: list[dict[str, Any]] = []
        diagnostics: list[AdapterDiagnostic] = []
        for tool in tools:
            try:
                payloads.append(self.project_tool(tool))
            except AdapterError as error:
                diagnostics.append(
                    AdapterDiagnostic(
                        tool_name=tool.tool_name,
                        code=error.code,
                        message=str(error),
                        pointer=error.pointer,
                    )
                )
        return ProviderProjection(
            provider=self.provider,
            tools=tuple(payloads),
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _prepared_schema(tool: ToolDefinition) -> dict[str, Any]:
        """Return a deep copy of the input schema, checked for projectability.

        Deep-copied because a projection must never mutate the canonical definition it was
        derived from — the same in-memory tool object is projected for several providers.
        """
        schema = copy.deepcopy(tool.input_schema)
        assert_projectable(schema)
        return schema
