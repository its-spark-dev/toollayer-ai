"""Converting one OpenAPI operation into one provider-neutral tool definition.

This is the single most important transformation in the project, so its rules are stated
explicitly rather than left implicit in the code.

**Inputs.** One resolved path item, one resolved operation, and the ``(path, method)`` pair
that identifies them.

**Output.** One ``ToolDefinition``: a closed Draft 2020-12 input schema, an operation block
that maps validated arguments onto an HTTP request, a default policy, and provenance that
points back at the source.

**Invariants.**

* Conversion is deterministic. The same document always yields the same tool, byte for byte,
  which is what makes the published digest meaningful.
* Conversion is offline. No network or filesystem access happens anywhere in this package.
* Every argument the model can supply appears in exactly one binding, and every binding
  reads an argument the schema declares. A parameter cannot be smuggled in unvalidated.
* The input schema is closed. An argument the schema does not declare is rejected at
  runtime, not passed through.
* Policy is derived from the method, not guessed from prose: reads are safe by default,
  writes are not, and deletes require confirmation.

**Refusals.** The converter refuses rather than approximates. Header and cookie parameters,
non-JSON request bodies, composed schemas, unresolved references, ambiguous parameter names,
and non-default serialization styles all produce an explicit failure for that operation. The
sibling operations in the document still convert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from toollayer_contracts.models import (
    ArgumentBinding,
    ToolDefinition,
    ToolOperation,
    ToolPolicy,
    ToolProvenance,
)
from toollayer_openapi.errors import InvalidDocumentError, UnsupportedFeatureError
from toollayer_openapi.naming import derive_tool_name, normalize_tool_name, to_display_name
from toollayer_openapi.schema_conversion import convert_schema

__all__ = ["BODY_ARGUMENT", "SUPPORTED_METHODS", "convert_operation", "operation_key"]

SUPPORTED_METHODS: Final[tuple[str, ...]] = ("get", "post", "put", "patch", "delete")

#: The single argument name a JSON request body is exposed under.
#:
#: Flattening body fields into the top level would collide with same-named path and query
#: parameters, and the collision would depend on the source document rather than on
#: anything the reviewer chose. One nested object keeps the two namespaces separate and
#: makes the generated schema obviously match the source body schema.
BODY_ARGUMENT: Final = "body"

_JSON_MEDIA_TYPE: Final = "application/json"

#: Effect and confirmation defaults per method. A reviewer may tighten these; nothing in
#: the pipeline loosens them automatically.
_METHOD_POLICY: Final[dict[str, tuple[str, bool]]] = {
    "get": ("read", False),
    "post": ("write", False),
    "put": ("write", False),
    "patch": ("write", False),
    "delete": ("destructive", True),
}


def operation_key(path: str, method: str) -> str:
    """Return the stable identifier for one operation within a document."""
    return f"{method.lower()} {path}"


@dataclass(frozen=True, slots=True)
class _Parameter:
    name: str
    location: str
    required: bool
    schema: dict[str, Any]
    description: str | None


def convert_operation(
    *,
    path: str,
    method: str,
    path_item: dict[str, Any],
    operation: dict[str, Any],
) -> ToolDefinition:
    """Convert one resolved operation into a tool definition."""
    normalized_method = method.lower()
    if normalized_method not in SUPPORTED_METHODS:
        raise UnsupportedFeatureError(f"the HTTP method {method!r} is not supported")

    _reject_unsupported_operation_features(path_item, operation)

    parameters = _collect_parameters(path_item, operation)
    _check_path_placeholders(path, parameters)

    properties: dict[str, Any] = {}
    required: list[str] = []
    bindings: list[ArgumentBinding] = []

    for parameter in parameters:
        if parameter.name in properties:
            raise UnsupportedFeatureError(
                f"the parameter name {parameter.name!r} is used in more than one location; "
                "the flat argument object cannot represent both"
            )
        schema = convert_schema(parameter.schema, pointer=f"/parameters/{parameter.name}")
        if parameter.description:
            schema.setdefault("description", parameter.description)
        properties[parameter.name] = schema
        if parameter.required:
            required.append(parameter.name)
        bindings.append(
            ArgumentBinding(
                argument_pointer=f"/{_escape(parameter.name)}",
                target=parameter.location,
                target_name=parameter.name,
            )
        )

    body_media_type = _convert_request_body(operation, properties, required, bindings)

    input_schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }

    operation_id = _operation_id(operation)
    tool_name = (
        normalize_tool_name(operation_id)
        if operation_id is not None
        else derive_tool_name(normalized_method, path)
    )
    effect_class, requires_confirmation = _METHOD_POLICY[normalized_method]

    return ToolDefinition(
        tool_name=tool_name,
        display_name=_display_name(operation, tool_name),
        description=_description(operation, normalized_method, path),
        input_schema=input_schema,
        operation=ToolOperation(
            method=normalized_method.upper(),  # type: ignore[arg-type]
            path_template=path,
            bindings=tuple(bindings),
            request_body_media_type=body_media_type,
        ),
        policy=ToolPolicy(
            effect_class=effect_class,  # type: ignore[arg-type]
            requires_confirmation=requires_confirmation,
        ),
        provenance=ToolProvenance(
            source_operation_id=operation_id,
            source_path=path,
            source_method=normalized_method,  # type: ignore[arg-type]
            tags=_tags(operation),
            deprecated=_deprecated(operation),
            description_origin="source" if _has_prose(operation) else "generated",
        ),
    )


def _reject_unsupported_operation_features(
    path_item: dict[str, Any], operation: dict[str, Any]
) -> None:
    """Refuse operation-level features this converter will not approximate."""
    if "servers" in path_item or "servers" in operation:
        raise UnsupportedFeatureError(
            "per-path and per-operation server overrides are not supported; the connector "
            "declares one reviewed base URL"
        )
    if "callbacks" in operation:
        raise UnsupportedFeatureError("callbacks are not supported")
    security = operation.get("security")
    if security not in (None, []):
        raise UnsupportedFeatureError(
            "per-operation security requirements are not interpreted; authentication is a "
            "runtime concern resolved from the connector's auth profile"
        )


def _collect_parameters(path_item: dict[str, Any], operation: dict[str, Any]) -> list[_Parameter]:
    """Merge path-item and operation parameters, with the operation overriding.

    OpenAPI says an operation-level parameter replaces a path-item parameter with the same
    ``(name, in)`` pair. Merging in that order, keyed by that pair, is exactly that rule.
    """
    merged: dict[tuple[str, str], _Parameter] = {}

    for source, raw in (("path item", path_item.get("parameters")), ("operation", operation.get("parameters"))):
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise InvalidDocumentError(f"{source} 'parameters' must be an array")
        seen_in_source: set[tuple[str, str]] = set()
        for entry in raw:
            parameter = _parse_parameter(entry, source)
            key = (parameter.name, parameter.location)
            if key in seen_in_source:
                raise InvalidDocumentError(
                    f"the {source} declares the parameter {parameter.name!r} twice"
                )
            seen_in_source.add(key)
            merged[key] = parameter

    return list(merged.values())


def _parse_parameter(entry: object, source: str) -> _Parameter:
    if not isinstance(entry, dict):
        raise InvalidDocumentError(f"{source} parameter entries must be objects")
    if "$ref" in entry:
        raise InvalidDocumentError("a parameter still contains a reference after resolution")

    name = entry.get("name")
    location = entry.get("in")
    if not isinstance(name, str) or not name:
        raise InvalidDocumentError("a parameter must declare a non-empty name")
    if not isinstance(location, str) or not location:
        raise InvalidDocumentError(f"the parameter {name!r} must declare an 'in' location")
    if location in {"header", "cookie"}:
        raise UnsupportedFeatureError(
            f"the parameter {name!r} is a {location} parameter; header and cookie inputs are "
            "not exposed as model-supplied arguments"
        )
    if location not in {"path", "query"}:
        raise InvalidDocumentError(f"the parameter {name!r} declares an unknown location")

    required = entry.get("required", False)
    if not isinstance(required, bool):
        raise InvalidDocumentError(f"the parameter {name!r} has a non-boolean 'required'")
    if location == "path" and not required:
        raise InvalidDocumentError(
            f"the path parameter {name!r} must declare 'required: true'"
        )

    _reject_unsupported_serialization(entry, name, location)

    if "content" in entry:
        raise UnsupportedFeatureError(
            f"the parameter {name!r} uses a media-type-encoded value, which is not supported"
        )
    schema = entry.get("schema")
    if not isinstance(schema, dict):
        raise InvalidDocumentError(f"the parameter {name!r} must declare a schema object")

    description = entry.get("description")
    if description is not None and not isinstance(description, str):
        raise InvalidDocumentError(f"the parameter {name!r} has a non-string description")

    return _Parameter(
        name=name,
        location=location,
        required=required,
        schema=schema,
        description=description.strip() if isinstance(description, str) else None,
    )


def _reject_unsupported_serialization(entry: dict[str, Any], name: str, location: str) -> None:
    """Refuse a parameter whose wire encoding differs from the executor's assumption.

    The executor percent-encodes one value per parameter. ``style: deepObject`` or
    ``explode: false`` on an array would encode differently, so accepting them here would
    produce a request the API cannot parse — with no error until it reaches the API.
    """
    default_style = "simple" if location == "path" else "form"
    style = entry.get("style", default_style)
    if style != default_style:
        raise UnsupportedFeatureError(
            f"the parameter {name!r} uses the serialization style {style!r}, which the "
            "executor does not implement"
        )

    default_explode = location == "query"
    explode = entry.get("explode", default_explode)
    if not isinstance(explode, bool):
        raise InvalidDocumentError(f"the parameter {name!r} has a non-boolean 'explode'")
    if explode != default_explode:
        raise UnsupportedFeatureError(
            f"the parameter {name!r} overrides 'explode', which the executor does not implement"
        )

    for flag in ("allowReserved", "allowEmptyValue"):
        value = entry.get(flag, False)
        if not isinstance(value, bool):
            raise InvalidDocumentError(f"the parameter {name!r} has a non-boolean {flag!r}")
        if value:
            raise UnsupportedFeatureError(
                f"the parameter {name!r} sets {flag!r}, which the executor does not implement"
            )


def _check_path_placeholders(path: str, parameters: list[_Parameter]) -> None:
    """Every ``{placeholder}`` must have a parameter, and every path parameter a placeholder.

    An unmatched placeholder would reach the executor as a literal ``{id}`` in the URL. An
    unmatched path parameter would be silently dropped. Both are caught here, where the
    reviewer can still see which operation is wrong.
    """
    placeholders: list[str] = []
    depth = 0
    current: list[str] = []
    for character in path:
        if character == "{":
            if depth:
                raise InvalidDocumentError("the path contains a nested placeholder")
            depth = 1
            current = []
        elif character == "}":
            if not depth:
                raise InvalidDocumentError("the path contains an unbalanced placeholder")
            depth = 0
            placeholders.append("".join(current))
        elif depth:
            current.append(character)
    if depth:
        raise InvalidDocumentError("the path contains an unterminated placeholder")

    if len(placeholders) != len(set(placeholders)):
        raise InvalidDocumentError("the path repeats a placeholder name")

    declared = {parameter.name for parameter in parameters if parameter.location == "path"}
    missing = sorted(set(placeholders) - declared)
    if missing:
        raise InvalidDocumentError(
            f"the path placeholder {missing[0]!r} has no matching path parameter"
        )
    extra = sorted(declared - set(placeholders))
    if extra:
        raise InvalidDocumentError(
            f"the path parameter {extra[0]!r} has no matching path placeholder"
        )


def _convert_request_body(
    operation: dict[str, Any],
    properties: dict[str, Any],
    required: list[str],
    bindings: list[ArgumentBinding],
) -> str | None:
    """Convert a JSON request body into one nested ``body`` argument."""
    raw = operation.get("requestBody")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InvalidDocumentError("'requestBody' must be an object")

    content = raw.get("content")
    if not isinstance(content, dict) or not content:
        raise InvalidDocumentError("'requestBody' must declare a content object")

    media_types = sorted(content)
    if _JSON_MEDIA_TYPE not in content:
        raise UnsupportedFeatureError(
            f"the request body offers {media_types[0]!r}; only {_JSON_MEDIA_TYPE!r} is supported"
        )

    media = content[_JSON_MEDIA_TYPE]
    if not isinstance(media, dict):
        raise InvalidDocumentError("the request body media type entry must be an object")
    schema = media.get("schema")
    if not isinstance(schema, dict):
        raise InvalidDocumentError("the request body must declare a schema object")

    if BODY_ARGUMENT in properties:
        raise UnsupportedFeatureError(
            f"the operation declares a parameter named {BODY_ARGUMENT!r}, which collides with "
            "the request body argument"
        )

    body_schema = convert_schema(schema, pointer="/requestBody")
    if body_schema.get("type") != "object":
        raise UnsupportedFeatureError(
            "only object request bodies are supported; a scalar or array body cannot be "
            "described as a named tool argument"
        )

    body_required = raw.get("required", False)
    if not isinstance(body_required, bool):
        raise InvalidDocumentError("'requestBody.required' must be a boolean")

    properties[BODY_ARGUMENT] = body_schema
    if body_required:
        required.append(BODY_ARGUMENT)

    # One binding per body field rather than one for the whole object: the executor then
    # writes exactly the fields the schema declared, so an extra key that somehow survives
    # validation still cannot reach the upstream API.
    for field_name in body_schema.get("properties", {}):
        escaped = _escape(field_name)
        bindings.append(
            ArgumentBinding(
                argument_pointer=f"/{BODY_ARGUMENT}/{escaped}",
                target="body",
                target_name=f"/{escaped}",
            )
        )
    return _JSON_MEDIA_TYPE


def _operation_id(operation: dict[str, Any]) -> str | None:
    if "operationId" not in operation:
        return None
    value = operation["operationId"]
    if not isinstance(value, str) or not value.strip():
        raise InvalidDocumentError("'operationId' must be a non-empty string when present")
    if len(value) > 256:
        raise InvalidDocumentError("'operationId' is too long")
    return value


def _has_prose(operation: dict[str, Any]) -> bool:
    return any(
        isinstance(operation.get(field), str) and operation[field].strip()
        for field in ("description", "summary")
    )


def _description(operation: dict[str, Any], method: str, path: str) -> str:
    """Use the document's own prose when it has any, and say so in provenance if not.

    A generated fallback is a placeholder, not documentation. It is deliberately terse so a
    reviewer notices it and writes something better before publishing.
    """
    for field in ("description", "summary"):
        value = operation.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:1024]
    return f"Call {method.upper()} {path} on the connected API."[:1024]


def _display_name(operation: dict[str, Any], tool_name: str) -> str:
    summary = operation.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()[:128]
    return to_display_name(tool_name)[:128]


def _tags(operation: dict[str, Any]) -> tuple[str, ...]:
    raw = operation.get("tags", [])
    if not isinstance(raw, list):
        raise InvalidDocumentError("'tags' must be an array")
    tags: list[str] = []
    for tag in raw:
        if not isinstance(tag, str) or not tag.strip():
            raise InvalidDocumentError("'tags' entries must be non-empty strings")
        text = tag.strip()[:64]
        if text not in tags:
            tags.append(text)
    return tuple(tags[:32])


def _deprecated(operation: dict[str, Any]) -> bool:
    value = operation.get("deprecated", False)
    if not isinstance(value, bool):
        raise InvalidDocumentError("'deprecated' must be a boolean")
    return value


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")
