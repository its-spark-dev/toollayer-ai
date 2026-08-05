"""Projection to an OpenAI-compatible function tool format.

Shape produced::

    {"type": "function",
     "function": {"name": ..., "description": ..., "parameters": {...}, "strict": true}}

Two provider-specific transformations apply on top of the shared audited subset.

**The dialect marker is dropped.** ``$schema`` describes the schema language, not the
arguments, so removing it from the projected parameters asserts nothing different.

**Optional properties become nullable-and-required.** Strict function calling requires every
declared property to appear in ``required``. Simply adding a name to ``required`` would make
an optional argument mandatory, which changes what the tool accepts — so instead the
property's type is widened to admit ``null`` and the name is added to ``required``. The model
then signals "not provided" by passing ``null``.

This is a *controlled normalization*, not a lossless projection: the projected schema accepts
one value the canonical schema does not. It is safe only because the runtime reverses it
before validation — :func:`normalize_provider_arguments` drops the ``null`` placeholders, and
the resulting object is validated against the unmodified canonical schema. The canonical
schema, never the projection, decides what actually executes.

This is exactly the kind of provider difference that makes perfect cross-provider portability
impossible to promise. It is handled explicitly and reversibly rather than silently.
"""

from __future__ import annotations

import copy
from typing import Any

from toollayer_contracts.adapters.base import AdapterError, ProviderAdapter
from toollayer_contracts.models import ToolDefinition

__all__ = ["OpenAIToolAdapter", "normalize_provider_arguments"]


class OpenAIToolAdapter(ProviderAdapter):
    """Project canonical tools into an OpenAI-compatible function tool payload."""

    provider = "openai"

    def project_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        schema = self._prepared_schema(tool)
        schema.pop("$schema", None)
        projected = _strictify(schema, pointer="")
        return {
            "type": "function",
            "function": {
                "name": tool.tool_name,
                "description": tool.description,
                "parameters": projected,
                "strict": True,
            },
        }


def _strictify(schema: dict[str, Any], *, pointer: str) -> dict[str, Any]:
    """Return a copy of ``schema`` in which every declared property is required.

    Optional properties are widened to admit ``null``. A property that is already nullable
    is left alone. A property with no declared ``type`` cannot be widened without guessing,
    so it is rejected rather than projected with a weaker meaning.
    """
    if schema.get("type") != "object":
        return schema

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema

    result = dict(schema)
    declared = list(properties)
    required = schema.get("required")
    required_names = list(required) if isinstance(required, list) else []
    optional = [name for name in declared if name not in required_names]

    new_properties: dict[str, Any] = {}
    for name, value in properties.items():
        escaped = str(name).replace("~", "~0").replace("/", "~1")
        child_pointer = f"{pointer}/properties/{escaped}"
        if not isinstance(value, dict):
            raise AdapterError(
                "unsupported_schema_shape",
                "a property schema must be an object",
                child_pointer,
            )
        child = _strictify(copy.deepcopy(value), pointer=child_pointer)
        items = child.get("items")
        if isinstance(items, dict):
            child["items"] = _strictify(items, pointer=f"{child_pointer}/items")
        if name in optional:
            child = _allow_null(child, child_pointer)
        new_properties[name] = child

    result["properties"] = new_properties
    result["required"] = declared
    result["additionalProperties"] = False
    return result


def _allow_null(schema: dict[str, Any], pointer: str) -> dict[str, Any]:
    """Widen a property schema so that ``null`` means "argument not provided".

    An ``enum`` has to be widened alongside the type. Widening only ``type`` would produce
    a schema that claims to accept ``null`` and then rejects it on the enum — a projection
    that looks correct and fails at call time.
    """
    declared = schema.get("type")
    if isinstance(declared, list):
        if "null" not in declared:
            schema["type"] = [*declared, "null"]
    elif isinstance(declared, str):
        if declared != "null":
            schema["type"] = [declared, "null"]
    else:
        raise AdapterError(
            "unsupported_schema_shape",
            "an optional property without a declared type cannot be projected to strict mode",
            pointer,
        )

    enum = schema.get("enum")
    if isinstance(enum, list) and None not in enum:
        schema["enum"] = [*enum, None]
    return schema


def normalize_provider_arguments(
    canonical_input_schema: dict[str, Any],
    arguments: object,
) -> dict[str, Any]:
    """Undo the strict-mode normalization before canonical validation.

    A ``null`` for a property that the canonical schema does not declare as nullable is the
    placeholder this adapter introduced, so it is removed. Every other value is passed
    through untouched — including a ``null`` the canonical schema genuinely allows.

    Arguments that the canonical schema does not declare at all are *kept*, so that the
    subsequent schema validation rejects them. Dropping them here would let a fabricated
    argument disappear silently instead of failing loudly.
    """
    if not isinstance(arguments, dict):
        raise AdapterError("invalid_arguments", "tool arguments must be a JSON object")

    properties = canonical_input_schema.get("properties")
    if not isinstance(properties, dict):
        return dict(arguments)

    required = canonical_input_schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()

    normalized: dict[str, Any] = {}
    for name, value in arguments.items():
        declared = properties.get(name)
        if value is None and name not in required_names and isinstance(declared, dict):
            declared_type = declared.get("type")
            nullable = declared_type == "null" or (
                isinstance(declared_type, list) and "null" in declared_type
            )
            if not nullable:
                continue
        normalized[name] = value
    return normalized
