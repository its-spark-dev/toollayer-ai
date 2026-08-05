"""Deriving stable tool names from OpenAPI operations.

A tool name is a dispatch identifier. A model produces it, and the runtime resolves it to an
exact operation, so it has to be *stable* (the same document always yields the same name),
*legible* (a reviewer can tell what it does), and *unique within a connector version*.

Two sources are used, in order:

1. ``operationId``, when the document declares one. It is the API author's own identifier,
   so it is the best available provenance.
2. A deterministic derivation from the method and path. This is *naming*, not invented
   provenance — ``source_operation_id`` stays null so nothing downstream can mistake a
   derived name for something the document actually said.

Collisions are never resolved by silently renaming. Two operations that normalize to the
same tool name are reported, because the fix belongs in the source document where a human
can make it meaningful.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

__all__ = ["MAX_TOOL_NAME_LENGTH", "derive_tool_name", "normalize_tool_name", "to_display_name"]

MAX_TOOL_NAME_LENGTH: Final = 64

_CAMEL_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYM_BOUNDARY: Final = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_IDENTIFIER: Final = re.compile(r"[^A-Za-z0-9]+")

#: Verb applied when a path-derived name has no better source. Chosen per method so the
#: derived name reads like what the operation does.
_METHOD_VERB: Final[dict[str, str]] = {
    "get": "get",
    "post": "create",
    "put": "replace",
    "patch": "update",
    "delete": "delete",
}


def _snake_case(value: str) -> str:
    """Convert an arbitrary identifier-ish string to lowercase snake_case."""
    spaced = _ACRONYM_BOUNDARY.sub("_", _CAMEL_BOUNDARY.sub("_", value))
    return _NON_IDENTIFIER.sub("_", spaced).strip("_").lower()


def _bounded(candidate: str) -> str:
    """Keep a name within the contract's length limit without losing uniqueness.

    Truncation alone would map two long, distinct names onto one identifier. Appending a
    digest of the full candidate keeps distinct inputs distinct while the readable prefix
    still tells a reviewer what the tool is.
    """
    if len(candidate) <= MAX_TOOL_NAME_LENGTH:
        return candidate
    suffix = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]
    keep = MAX_TOOL_NAME_LENGTH - len(suffix) - 1
    return f"{candidate[:keep].rstrip('_')}_{suffix}"


def normalize_tool_name(value: str) -> str:
    """Normalize an ``operationId`` into a canonical tool name."""
    candidate = _snake_case(value)
    if not candidate:
        raise ValueError("the operation identifier cannot produce a tool name")
    if candidate[0].isdigit():
        candidate = f"op_{candidate}"
    return _bounded(candidate)


def derive_tool_name(method: str, path: str) -> str:
    """Derive a deterministic tool name from an HTTP method and a path template.

    ``GET /v1/tickets/{ticket_id}`` becomes ``get_tickets_by_ticket_id``.

    Version-looking leading segments (``/v1``, ``/v2beta``) are dropped, because they say
    something about the API's URL layout and nothing about what the operation does — and
    keeping them would make every tool name in a versioned API start with the same token.
    """
    verb = _METHOD_VERB.get(method.lower())
    if verb is None:
        raise ValueError(f"unsupported HTTP method: {method!r}")

    segments: list[str] = []
    raw_segments = [segment for segment in path.strip("/").split("/") if segment]
    for index, segment in enumerate(raw_segments):
        if index == 0 and re.fullmatch(r"v\d+[a-z0-9]*", segment.lower()):
            continue
        if segment.startswith("{") and segment.endswith("}"):
            normalized = _snake_case(segment[1:-1])
            if normalized:
                segments.extend(("by", normalized))
        else:
            normalized = _snake_case(segment)
            if normalized:
                segments.append(normalized)

    if not segments:
        return _bounded(f"{verb}_root")
    return _bounded(f"{verb}_{'_'.join(segments)}")


def to_display_name(tool_name: str) -> str:
    """Turn a canonical tool name into a human-readable console label."""
    words = [word for word in tool_name.split("_") if word]
    if not words:
        return tool_name
    return " ".join([words[0].capitalize(), *words[1:]])
