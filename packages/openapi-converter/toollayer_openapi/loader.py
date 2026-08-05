"""Loading and structurally validating an uploaded API description.

Ingestion is the first trust boundary in the whole system. Everything downstream — the
analyzer, the converter, the review console — treats the parsed document as structured data
rather than as an attack surface, and that is only safe because this module refuses anything
ambiguous before parsing produces a Python object.

Three properties are enforced here:

* **Bounded.** Byte length, nesting depth, and node count are capped before traversal, so a
  small upload cannot expand into an expensive or unbounded walk.
* **Unambiguous.** JSON is read with a strict reader that rejects duplicate keys, non-finite
  numbers, and trailing data. Two parsers disagreeing about which duplicate key wins is a
  classic way to get a reviewer and a runtime to see different documents.
* **Offline.** YAML is parsed in safe mode, and no loader ever resolves a URL or a file path.
  There is no network client and no filesystem resolver in this package at all, which makes
  server-side request forgery through a crafted ``$ref`` structurally impossible rather than
  merely blocked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

import yaml

from toollayer_contracts.canonical_json import digest_of
from toollayer_openapi.errors import DocumentTooLargeError, InvalidDocumentError

__all__ = ["LoadedDocument", "SourceLimits", "load_document"]

_SUPPORTED_MAJOR: Final = "3"


@dataclass(frozen=True, slots=True)
class SourceLimits:
    """Ingestion limits. Generous enough for real specifications, finite by design."""

    max_bytes: int = 2 * 1024 * 1024
    max_depth: int = 64
    max_nodes: int = 200_000
    max_operations: int = 256


@dataclass(frozen=True, slots=True)
class LoadedDocument:
    """A parsed API description plus the provenance of the bytes it came from."""

    document: dict[str, Any]
    spec_version: str
    filename: str
    digest: str
    byte_length: int
    source_format: str


def _strict_json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate object keys instead of silently keeping the last one."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidDocumentError(
                "the document contains a duplicate object key", pointer=f"/{key}"
            )
        result[key] = value
    return result


def _parse_json(text: str) -> Any:
    def _reject(value: str) -> float:
        raise InvalidDocumentError("the document contains a non-finite number")

    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_json_object_pairs,
            parse_constant=_reject,
        )
    except InvalidDocumentError:
        raise
    except ValueError:
        raise InvalidDocumentError("the document is not well-formed JSON") from None


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys.

    ``yaml.SafeLoader`` already refuses arbitrary object construction, tags, and Python
    types. It does *not* refuse duplicate keys, which would let a document read one way in
    the review console and another way in a different YAML implementation.
    """


def _no_duplicate_mapping(loader: _NoDuplicateKeyLoader, node: yaml.MappingNode) -> Any:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise InvalidDocumentError("the document contains a duplicate mapping key")
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_NoDuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_mapping
)


def _parse_yaml(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_NoDuplicateKeyLoader)
    except InvalidDocumentError:
        raise
    except yaml.YAMLError:
        raise InvalidDocumentError("the document is not well-formed YAML") from None


def _enforce_shape(node: object, limits: SourceLimits) -> None:
    """Walk the parsed document once, enforcing depth and node-count limits.

    Iterative rather than recursive so that a deeply nested document produces a clean
    ``InvalidDocumentError`` instead of a ``RecursionError`` from somewhere in the stack.
    """
    stack: list[tuple[object, int]] = [(node, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes:
            raise DocumentTooLargeError("the document contains too many nodes")
        if depth > limits.max_depth:
            raise DocumentTooLargeError("the document nests too deeply")
        if isinstance(current, dict):
            for key, value in current.items():
                if not isinstance(key, str):
                    raise InvalidDocumentError("object keys must be strings")
                stack.append((value, depth + 1))
        elif isinstance(current, list):
            for item in current:
                stack.append((item, depth + 1))
        elif isinstance(current, float) and (
            current != current or current in (float("inf"), float("-inf"))
        ):
            raise InvalidDocumentError("the document contains a non-finite number")


def load_document(
    raw: bytes,
    *,
    filename: str,
    limits: SourceLimits | None = None,
) -> LoadedDocument:
    """Parse an uploaded API description and record its provenance.

    The format is chosen by content, not by file extension: a document named ``.yaml`` that
    happens to be JSON is still valid JSON, and trusting the extension would let an upload
    choose its own parser.
    """
    limits = limits or SourceLimits()

    if not isinstance(raw, (bytes, bytearray)):
        raise InvalidDocumentError("the uploaded document must be raw bytes")
    if not raw:
        raise InvalidDocumentError("the uploaded document is empty")
    if len(raw) > limits.max_bytes:
        raise DocumentTooLargeError(
            f"the uploaded document exceeds the {limits.max_bytes} byte ingestion limit"
        )
    if raw.startswith(b"\xef\xbb\xbf"):
        raise InvalidDocumentError("the uploaded document starts with a UTF-8 byte order mark")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidDocumentError("the uploaded document is not valid UTF-8") from None

    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        document = _parse_json(text)
        source_format = "json"
    else:
        document = _parse_yaml(text)
        source_format = "yaml"

    if not isinstance(document, dict):
        raise InvalidDocumentError("the document root must be an object")

    _enforce_shape(document, limits)

    version = document.get("openapi")
    if not isinstance(version, str) or not version:
        raise InvalidDocumentError(
            "the document must declare a string 'openapi' version", pointer="/openapi"
        )
    if not version.startswith(f"{_SUPPORTED_MAJOR}."):
        raise InvalidDocumentError(
            f"only OpenAPI {_SUPPORTED_MAJOR}.x documents are supported", pointer="/openapi"
        )
    if not isinstance(document.get("paths"), dict):
        raise InvalidDocumentError("the document must declare a 'paths' object", pointer="/paths")

    return LoadedDocument(
        document=document,
        spec_version=version,
        filename=filename,
        digest=digest_of(bytes(raw)),
        byte_length=len(raw),
        source_format=source_format,
    )
