"""Validating model-proposed arguments and turning them into an HTTP request.

Model output is untrusted input. It arrives as a JSON object that *claims* to satisfy a
tool's schema, and everything downstream depends on that claim actually being true — so it
is checked here, against the published schema, before a single value is used to build a URL.

Two properties are worth stating explicitly.

**Validation precedes construction.** No argument is read, encoded, or placed anywhere until
the whole object has passed schema validation. Building the URL first and validating after
would mean a rejected call had already done work with attacker-controlled values.

**Bindings drive construction, not the arguments.** The request is assembled by iterating the
tool's published bindings and looking each one up, never by iterating the supplied arguments.
An argument the tool did not declare therefore has nowhere to go even if it somehow survived
validation. The closed schema and the binding-driven walk are two independent reasons the
same attack fails.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlencode

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from toollayer_contracts.errors import ErrorCode, ErrorDetail, ToolLayerError
from toollayer_contracts.models import ToolDefinition

__all__ = ["ArgumentValidationError", "PreparedRequest", "prepare_request", "validate_arguments"]

_MAX_ARGUMENT_BYTES: Final = 64 * 1024


class ArgumentValidationError(ToolLayerError):
    """The proposed arguments do not satisfy the tool's published input schema."""

    code = ErrorCode.ARGUMENT_VALIDATION_FAILED


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """A fully constructed outbound request, ready for the executor."""

    method: str
    url: str
    path: str
    query: tuple[tuple[str, str], ...]
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    content_type: str | None


def validate_arguments(tool: ToolDefinition, arguments: object) -> dict[str, Any]:
    """Validate ``arguments`` against the tool's input schema and return them.

    Every schema violation is reported, not just the first, so a caller correcting a model's
    output does not have to iterate one field at a time. Messages name the failing keyword
    and location and never the rejected value.
    """
    if not isinstance(arguments, dict):
        raise ArgumentValidationError("tool arguments must be a JSON object", pointer="")

    encoded = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
    if len(encoded) > _MAX_ARGUMENT_BYTES:
        raise ArgumentValidationError(
            f"tool arguments exceed the {_MAX_ARGUMENT_BYTES} byte limit", pointer=""
        )

    validator = Draft202012Validator(tool.input_schema)
    errors: list[JsonSchemaValidationError] = sorted(
        validator.iter_errors(arguments), key=lambda error: list(error.absolute_path)
    )
    if errors:
        details = tuple(
            ErrorDetail(
                code=f"schema.{error.validator if isinstance(error.validator, str) else 'schema'}",
                message=(
                    "the argument violates the "
                    f"{error.validator if isinstance(error.validator, str) else 'schema'!r} "
                    "constraint"
                ),
                pointer=_pointer(error.absolute_path),
            )
            for error in errors[:25]
        )
        raise ArgumentValidationError(
            "the proposed arguments do not satisfy the tool's input schema",
            pointer=_pointer(errors[0].absolute_path),
            details=details,
        )
    return arguments


def prepare_request(
    tool: ToolDefinition,
    arguments: dict[str, Any],
    *,
    base_url: str,
) -> PreparedRequest:
    """Build the outbound request for a tool call whose arguments already validated."""
    path = tool.operation.path_template
    query: list[tuple[str, str]] = []
    headers: list[tuple[str, str]] = []
    body: dict[str, Any] = {}
    has_body = False

    for binding in tool.operation.bindings:
        present, value = _read_pointer(arguments, binding.argument_pointer)
        if not present or value is None:
            # An absent optional argument contributes nothing. It is not sent as an empty
            # string, because "" and "not provided" mean different things to most APIs.
            continue

        if binding.target == "path":
            placeholder = "{" + binding.target_name + "}"
            if placeholder not in path:
                raise ArgumentValidationError(
                    "a path binding does not match the tool's path template",
                    pointer=binding.argument_pointer,
                )
            # `safe=""` so that a value containing `/` or `?` is encoded rather than
            # becoming extra path segments or a query string. This is the control that
            # stops an argument from rewriting the request target.
            path = path.replace(placeholder, quote(_scalar(value, binding.argument_pointer), safe=""))
        elif binding.target == "query":
            query.append((binding.target_name, _scalar(value, binding.argument_pointer)))
        elif binding.target == "header":
            headers.append((binding.target_name, _header_value(value, binding.argument_pointer)))
        else:
            _write_pointer(body, binding.target_name, value)
            has_body = True

    if "{" in path or "}" in path:
        raise ArgumentValidationError(
            "the request path still contains an unfilled placeholder", pointer=""
        )

    encoded_body: bytes | None = None
    content_type: str | None = None
    if has_body:
        content_type = tool.operation.request_body_media_type or "application/json"
        encoded_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    url = base_url.rstrip("/") + path
    if query:
        url = f"{url}?{urlencode(query, doseq=False)}"

    # The protocol headers are decided here rather than in a transport. A transport that
    # added its own content type could send a body the upstream parses differently from what
    # the tool definition declared, and every transport would have to remember to do it.
    protocol_headers: list[tuple[str, str]] = [("accept", "application/json")]
    if content_type is not None:
        protocol_headers.append(("content-type", content_type))

    return PreparedRequest(
        method=tool.operation.method,
        url=url,
        path=path,
        query=tuple(query),
        headers=tuple([*protocol_headers, *headers]),
        body=encoded_body,
        content_type=content_type,
    )


def _scalar(value: object, pointer: str) -> str:
    """Render a validated scalar for a URL.

    Booleans are rendered lowercase because that is what JSON and every query-string
    convention in the demo API expects; Python's ``str(True)`` would send ``True``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise ArgumentValidationError(
        "only scalar arguments can be placed in a path or query", pointer=pointer
    )


def _header_value(value: object, pointer: str) -> str:
    text = _scalar(value, pointer)
    if any(character in text for character in ("\r", "\n", "\0")):
        raise ArgumentValidationError(
            "a header value may not contain a control character", pointer=pointer
        )
    return text


def _read_pointer(document: dict[str, Any], pointer: str) -> tuple[bool, Any]:
    """Read an RFC 6901 pointer, reporting presence separately from value.

    Presence is returned separately because ``None`` is a legitimate value for a nullable
    argument, and collapsing "absent" and "null" would send a null where the caller meant
    to send nothing.
    """
    if pointer == "":
        return True, document
    current: Any = document
    for token in pointer.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _write_pointer(document: dict[str, Any], pointer: str, value: Any) -> None:
    """Write a value at an RFC 6901 pointer, creating intermediate objects."""
    tokens = [token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:]]
    if not tokens:
        raise ArgumentValidationError("a body binding must target a property", pointer=pointer)
    current = document
    for token in tokens[:-1]:
        nested = current.get(token)
        if not isinstance(nested, dict):
            nested = {}
            current[token] = nested
        current = nested
    current[tokens[-1]] = value


def _pointer(path: Any) -> str:
    return "".join(f"/{str(part).replace('~', '~0').replace('/', '~1')}" for part in path)
