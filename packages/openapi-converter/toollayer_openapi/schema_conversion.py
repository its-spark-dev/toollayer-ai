"""Converting OpenAPI schema objects into JSON Schema Draft 2020-12.

OpenAPI 3.1 schemas *are* Draft 2020-12 schemas, so most of the work is filtering rather
than translating. OpenAPI 3.0 schemas are close but not identical, and the two differences
that matter here are handled explicitly: ``nullable: true`` becomes a union type, and
``exclusiveMinimum``/``exclusiveMaximum`` change from booleans to numbers.

The filter is an **allowlist**. A keyword this module has not been taught is refused rather
than passed through, because a schema that reaches the runtime is used to validate model
output, and a keyword that silently means nothing there is a validation hole. Refusing an
operation is visible; a hole is not.
"""

from __future__ import annotations

from typing import Any, Final

from toollayer_openapi.errors import InvalidDocumentError, UnsupportedFeatureError

__all__ = ["MAX_SCHEMA_DEPTH", "convert_schema"]

MAX_SCHEMA_DEPTH: Final = 8

_SCALAR_TYPES: Final[frozenset[str]] = frozenset({"string", "integer", "number", "boolean"})
_ALL_TYPES: Final[frozenset[str]] = _SCALAR_TYPES | {"object", "array", "null"}

#: Keywords carried through unchanged when they apply to the declared type.
_ANNOTATION_KEYWORDS: Final[frozenset[str]] = frozenset({"description", "title"})
_STRING_KEYWORDS: Final[frozenset[str]] = frozenset({"minLength", "maxLength", "pattern", "format"})
_NUMBER_KEYWORDS: Final[frozenset[str]] = frozenset({"minimum", "maximum"})
_ARRAY_KEYWORDS: Final[frozenset[str]] = frozenset({"minItems", "maxItems"})

#: Keywords recognized but deliberately ignored. They describe the API or the documentation
#: rather than constraining a value, so dropping them changes nothing a validator checks.
_IGNORED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"example", "examples", "externalDocs", "readOnly", "writeOnly", "xml", "discriminator"}
)

#: Composition keywords. Supporting these properly means deciding what a partial match
#: should do at execution time, which is a semantic choice the source document does not
#: make. Refusing keeps the generated tool honest.
_COMPOSITION_KEYWORDS: Final[frozenset[str]] = frozenset({"oneOf", "allOf", "anyOf", "not"})


def convert_schema(schema: object, *, pointer: str = "", depth: int = 0) -> dict[str, Any]:
    """Convert one resolved OpenAPI schema object into a Draft 2020-12 schema."""
    if not isinstance(schema, dict):
        raise InvalidDocumentError("a schema must be an object", pointer=pointer)
    if depth > MAX_SCHEMA_DEPTH:
        raise UnsupportedFeatureError(
            f"the schema nests deeper than {MAX_SCHEMA_DEPTH} levels", pointer=pointer
        )

    present_composition = sorted(_COMPOSITION_KEYWORDS & set(schema))
    if present_composition:
        raise UnsupportedFeatureError(
            f"the schema keyword {present_composition[0]!r} is not supported", pointer=pointer
        )
    if "$ref" in schema:
        raise InvalidDocumentError(
            "the schema still contains a reference after resolution", pointer=pointer
        )

    declared_type = schema.get("type")
    if isinstance(declared_type, list):
        raise UnsupportedFeatureError(
            "union types are only supported through 'nullable'", pointer=pointer
        )
    if declared_type is None:
        raise UnsupportedFeatureError(
            "the schema does not declare a type; untyped values cannot be validated",
            pointer=pointer,
        )
    if not isinstance(declared_type, str) or declared_type not in _ALL_TYPES:
        raise UnsupportedFeatureError("the schema declares an unsupported type", pointer=pointer)

    converted: dict[str, Any] = {"type": declared_type}
    _apply_nullable(schema, converted, pointer)
    _apply_annotations(schema, converted, pointer)
    _apply_enum_and_default(schema, converted, declared_type, pointer)

    if declared_type == "string":
        _copy_supported(schema, converted, _STRING_KEYWORDS, pointer)
    elif declared_type in {"integer", "number"}:
        _copy_supported(schema, converted, _NUMBER_KEYWORDS, pointer)
        _apply_exclusive_bounds(schema, converted, pointer)
    elif declared_type == "array":
        _copy_supported(schema, converted, _ARRAY_KEYWORDS, pointer)
        items = schema.get("items")
        if items is None:
            raise UnsupportedFeatureError(
                "an array schema must declare its items", pointer=pointer
            )
        converted["items"] = convert_schema(items, pointer=f"{pointer}/items", depth=depth + 1)
    elif declared_type == "object":
        converted.update(_convert_object(schema, pointer=pointer, depth=depth))

    _reject_unknown_keywords(schema, declared_type, pointer)
    return converted


def _convert_object(schema: dict[str, Any], *, pointer: str, depth: int) -> dict[str, Any]:
    """Convert an object schema into a closed Draft 2020-12 object schema."""
    raw_properties = schema.get("properties")
    if not isinstance(raw_properties, dict) or not raw_properties:
        raise UnsupportedFeatureError(
            "an object schema must declare at least one property; free-form objects cannot "
            "be validated and are refused rather than passed through",
            pointer=pointer,
        )
    if schema.get("additionalProperties") not in (None, False):
        raise UnsupportedFeatureError(
            "an object schema that permits additional properties cannot be converted into a "
            "closed tool schema",
            pointer=pointer,
        )

    properties: dict[str, Any] = {}
    for name, value in raw_properties.items():
        if not isinstance(name, str) or not name:
            raise InvalidDocumentError(
                "a property name must be a non-empty string", pointer=pointer
            )
        escaped = name.replace("~", "~0").replace("/", "~1")
        properties[name] = convert_schema(
            value, pointer=f"{pointer}/properties/{escaped}", depth=depth + 1
        )

    raw_required = schema.get("required", [])
    if not isinstance(raw_required, list):
        raise InvalidDocumentError("'required' must be an array", pointer=f"{pointer}/required")
    required: list[str] = []
    for entry in raw_required:
        if not isinstance(entry, str):
            raise InvalidDocumentError(
                "'required' entries must be strings", pointer=f"{pointer}/required"
            )
        if entry not in properties:
            raise InvalidDocumentError(
                "'required' names a property the schema does not declare",
                pointer=f"{pointer}/required",
            )
        if entry not in required:
            required.append(entry)

    return {
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _apply_nullable(schema: dict[str, Any], converted: dict[str, Any], pointer: str) -> None:
    """Translate the OpenAPI 3.0 ``nullable`` flag into a Draft 2020-12 union type."""
    nullable = schema.get("nullable")
    if nullable is None:
        return
    if not isinstance(nullable, bool):
        raise InvalidDocumentError("'nullable' must be a boolean", pointer=pointer)
    if nullable:
        converted["type"] = [converted["type"], "null"]


def _apply_annotations(schema: dict[str, Any], converted: dict[str, Any], pointer: str) -> None:
    for keyword in sorted(_ANNOTATION_KEYWORDS):
        if keyword not in schema:
            continue
        value = schema[keyword]
        if not isinstance(value, str):
            raise InvalidDocumentError(f"'{keyword}' must be a string", pointer=pointer)
        text = value.strip()
        if text:
            converted[keyword] = text


def _apply_enum_and_default(
    schema: dict[str, Any],
    converted: dict[str, Any],
    declared_type: str,
    pointer: str,
) -> None:
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            raise InvalidDocumentError("'enum' must be a non-empty array", pointer=pointer)
        for value in enum:
            if value is not None and not _matches_type(value, declared_type):
                raise InvalidDocumentError(
                    "an 'enum' value does not match the declared type", pointer=pointer
                )
        converted["enum"] = list(enum)

    if "default" in schema:
        default = schema["default"]
        if not _matches_type(default, declared_type):
            raise InvalidDocumentError(
                "'default' does not match the declared type", pointer=pointer
            )
        if "enum" in converted and default not in converted["enum"]:
            raise InvalidDocumentError("'default' is not one of the enum values", pointer=pointer)
        converted["default"] = default


def _apply_exclusive_bounds(
    schema: dict[str, Any], converted: dict[str, Any], pointer: str
) -> None:
    """Normalize the OpenAPI 3.0 boolean form of the exclusive bound keywords.

    In 3.0, ``exclusiveMinimum: true`` modifies ``minimum``. In 3.1 and Draft 2020-12,
    ``exclusiveMinimum`` is itself the bound. Both are accepted; the 2020-12 form is emitted.
    """
    for bound, exclusive in (("minimum", "exclusiveMinimum"), ("maximum", "exclusiveMaximum")):
        if exclusive not in schema:
            continue
        value = schema[exclusive]
        if isinstance(value, bool):
            if not value:
                continue
            inclusive = converted.pop(bound, None)
            if inclusive is None:
                raise InvalidDocumentError(
                    f"'{exclusive}: true' requires a '{bound}'", pointer=pointer
                )
            converted[exclusive] = inclusive
        elif isinstance(value, (int, float)):
            converted[exclusive] = value
        else:
            raise InvalidDocumentError(
                f"'{exclusive}' must be a number or a boolean", pointer=pointer
            )


def _copy_supported(
    schema: dict[str, Any],
    converted: dict[str, Any],
    keywords: frozenset[str],
    pointer: str,
) -> None:
    for keyword in sorted(keywords):
        if keyword not in schema:
            continue
        value = schema[keyword]
        if keyword in {"pattern", "format"}:
            if not isinstance(value, str) or not value:
                raise InvalidDocumentError(
                    f"'{keyword}' must be a non-empty string", pointer=pointer
                )
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            raise InvalidDocumentError(f"'{keyword}' must be a number", pointer=pointer)
        converted[keyword] = value


def _reject_unknown_keywords(schema: dict[str, Any], declared_type: str, pointer: str) -> None:
    """Fail on any keyword this module neither converts nor knowingly ignores."""
    known = {
        "type",
        "nullable",
        "enum",
        "default",
        "exclusiveMinimum",
        "exclusiveMaximum",
        *_ANNOTATION_KEYWORDS,
        *_IGNORED_KEYWORDS,
    }
    if declared_type == "string":
        known |= _STRING_KEYWORDS
    elif declared_type in {"integer", "number"}:
        known |= _NUMBER_KEYWORDS
    elif declared_type == "array":
        known |= _ARRAY_KEYWORDS | {"items"}
    elif declared_type == "object":
        known |= {"properties", "required", "additionalProperties"}

    unknown = sorted(set(schema) - known)
    if unknown:
        raise UnsupportedFeatureError(
            f"the schema keyword {unknown[0]!r} is not supported for a "
            f"{declared_type!r} schema",
            pointer=pointer,
        )


def _matches_type(value: object, declared_type: str) -> bool:
    if declared_type == "string":
        return isinstance(value, str)
    if declared_type == "boolean":
        return isinstance(value, bool)
    if declared_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared_type == "array":
        return isinstance(value, list)
    if declared_type == "object":
        return isinstance(value, dict)
    if declared_type == "null":
        return value is None
    return False
