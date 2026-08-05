"""Same-document ``$ref`` resolution.

Only pointers of the form ``#/path/to/node`` are resolved. Every other reference — remote
URLs, sibling files, absolute paths — is rejected, and there is no code here that could
fetch one even if it wanted to. That is a deliberate structural choice: a converter that
dereferences a URL found inside an uploaded document is a server-side request forgery
primitive with extra steps.

Resolution is depth- and cycle-bounded. A self-referential schema (``Node.children:
[Node]``) is legal OpenAPI, and inlining it would not terminate, so it is reported as an
unsupported feature for the operation that uses it rather than being allowed to hang the
request.
"""

from __future__ import annotations

from typing import Any, Final

from toollayer_openapi.errors import InvalidDocumentError, UnsupportedFeatureError

__all__ = ["ReferenceResolver", "decode_pointer_token", "encode_pointer_token"]

_MAX_RESOLUTION_DEPTH: Final = 32


def encode_pointer_token(token: str) -> str:
    """Escape one RFC 6901 pointer token."""
    return token.replace("~", "~0").replace("/", "~1")


def decode_pointer_token(token: str) -> str:
    """Unescape one RFC 6901 pointer token."""
    return token.replace("~1", "/").replace("~0", "~")


class ReferenceResolver:
    """Resolve and inline same-document references within one loaded document."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document

    def resolve(self, node: object, *, pointer: str = "") -> Any:
        """Return ``node`` with every nested same-document reference inlined."""
        return self._resolve(node, pointer=pointer, seen=(), depth=0)

    def _resolve(
        self,
        node: object,
        *,
        pointer: str,
        seen: tuple[str, ...],
        depth: int,
    ) -> Any:
        if depth > _MAX_RESOLUTION_DEPTH:
            raise UnsupportedFeatureError(
                "the referenced schema nests deeper than the resolver supports",
                pointer=pointer,
            )

        if isinstance(node, dict):
            reference = node.get("$ref")
            if reference is not None:
                target_pointer = self._reference_pointer(reference, pointer)
                if target_pointer in seen:
                    raise UnsupportedFeatureError(
                        "recursive schema references are not supported", pointer=pointer
                    )
                target = self._dereference(target_pointer, pointer)
                resolved = self._resolve(
                    target,
                    pointer=target_pointer,
                    seen=(*seen, target_pointer),
                    depth=depth + 1,
                )
                # A sibling of `$ref` in an OpenAPI 3.1 document is an annotation overlay.
                # Applying the siblings on top of the resolved target keeps `description`
                # overrides working without letting a sibling replace a validation keyword
                # the target asserted.
                siblings = {key: value for key, value in node.items() if key != "$ref"}
                if siblings and isinstance(resolved, dict):
                    merged = dict(resolved)
                    for key, value in siblings.items():
                        merged[key] = self._resolve(
                            value,
                            pointer=f"{pointer}/{encode_pointer_token(key)}",
                            seen=seen,
                            depth=depth + 1,
                        )
                    return merged
                return resolved

            return {
                key: self._resolve(
                    value,
                    pointer=f"{pointer}/{encode_pointer_token(key)}",
                    seen=seen,
                    depth=depth + 1,
                )
                for key, value in node.items()
            }

        if isinstance(node, list):
            return [
                self._resolve(item, pointer=f"{pointer}/{index}", seen=seen, depth=depth + 1)
                for index, item in enumerate(node)
            ]

        return node

    @staticmethod
    def _reference_pointer(reference: object, pointer: str) -> str:
        if not isinstance(reference, str) or not reference:
            raise InvalidDocumentError("a $ref value must be a non-empty string", pointer=pointer)
        if not reference.startswith("#/"):
            raise UnsupportedFeatureError(
                "only same-document references of the form '#/...' are supported",
                pointer=pointer,
            )
        return reference[1:]

    def _dereference(self, target_pointer: str, origin: str) -> Any:
        current: Any = self._document
        for token in target_pointer.split("/")[1:]:
            key = decode_pointer_token(token)
            if isinstance(current, dict):
                if key not in current:
                    raise InvalidDocumentError(
                        "a $ref points at a node the document does not contain", pointer=origin
                    )
                current = current[key]
            elif isinstance(current, list):
                try:
                    index = int(key)
                except ValueError:
                    raise InvalidDocumentError(
                        "a $ref uses a non-numeric index into an array", pointer=origin
                    ) from None
                if index < 0 or index >= len(current):
                    raise InvalidDocumentError(
                        "a $ref points past the end of an array", pointer=origin
                    )
                current = current[index]
            else:
                raise InvalidDocumentError(
                    "a $ref traverses into a value that is not an object or array",
                    pointer=origin,
                )
        return current
