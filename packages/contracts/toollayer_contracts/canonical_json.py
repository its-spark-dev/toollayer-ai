"""Deterministic serialization and content digests.

Immutability in this project is a claim a consumer can check, not a promise the producer
makes. That only works if two processes serializing the same logical document produce the
same bytes, so serialization is pinned here: sorted keys, no insignificant whitespace, no
non-finite numbers, and UTF-8 without escaping.

The digest deliberately excludes the fields that carry the digest itself, so a document can
embed its own digest without the value depending on itself.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "CanonicalJsonError",
    "canonical_bytes",
    "canonical_json",
    "content_digest",
    "digest_of",
    "verify_digest",
]

_DIGEST_PREFIX = "sha256:"


class CanonicalJsonError(ValueError):
    """A value cannot be serialized deterministically."""


def _assert_serializable(value: object, path: str = "") -> None:
    """Reject values whose JSON encoding would be ambiguous or lossy."""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CanonicalJsonError(f"non-finite number at {path or '<root>'}")
        return
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalJsonError(f"non-string object key at {path or '<root>'}")
            _assert_serializable(value[key], f"{path}/{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_serializable(item, f"{path}/{index}")
        return
    raise CanonicalJsonError(f"unsupported type at {path or '<root>'}")


def canonical_json(document: object) -> str:
    """Serialize ``document`` so that equal documents always produce equal text."""
    _assert_serializable(document)
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(document: object) -> bytes:
    """Serialize ``document`` to canonical UTF-8 bytes."""
    return canonical_json(document).encode("utf-8")


def digest_of(data: bytes) -> str:
    """Return a prefixed SHA-256 digest of raw bytes."""
    if not isinstance(data, (bytes, bytearray)):
        raise CanonicalJsonError("digest input must be bytes")
    return _DIGEST_PREFIX + hashlib.sha256(bytes(data)).hexdigest()


def content_digest(document: object, *, exclude: Iterable[str] = ()) -> str:
    """Return the canonical digest of ``document``, ignoring top-level ``exclude`` keys.

    ``exclude`` exists so a document can carry its own digest: the digest is computed over
    everything except the digest field and any identifier derived from it.
    """
    excluded = frozenset(exclude)
    if excluded:
        if not isinstance(document, Mapping):
            raise CanonicalJsonError("exclude is only meaningful for object documents")
        payload: Any = {key: value for key, value in document.items() if key not in excluded}
    else:
        payload = document
    return digest_of(canonical_bytes(payload))


def verify_digest(document: object, expected: str, *, exclude: Iterable[str] = ()) -> bool:
    """Return whether ``document`` still hashes to ``expected``.

    Comparison is a plain equality check on lowercase hex: these digests are integrity
    markers for non-secret artifacts, not authentication tags, so constant-time comparison
    would imply a guarantee this function does not provide.
    """
    if not isinstance(expected, str) or not expected.startswith(_DIGEST_PREFIX):
        return False
    try:
        actual = content_digest(document, exclude=exclude)
    except CanonicalJsonError:
        return False
    return actual == expected
