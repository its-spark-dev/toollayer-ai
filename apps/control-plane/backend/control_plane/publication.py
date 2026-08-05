"""Building an immutable published connector version from a reviewed draft.

Publication is **server-authoritative**. It rebuilds the connector from the stored analysis
and the stored review, and it accepts nothing from the request beyond the version number and
who is publishing. A client cannot submit the document to publish, so a compromised or buggy
console cannot publish a definition that no reviewer ever saw.

The output is a self-contained contract document plus its digest. It embeds no database key,
no deployment configuration, and no credential — everything a runtime needs, and nothing
that ties it to the machine it came from.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from control_plane.review import ReviewState, review_readiness
from toollayer_contracts import CONTRACT_VERSION, content_digest, validate_connector_definition
from toollayer_contracts.errors import ErrorCode, ToolLayerError

__all__ = ["BuiltConnector", "NotReadyForPublication", "build_connector_document"]


class NotReadyForPublication(ToolLayerError):
    """The reviewed draft cannot produce a publishable connector."""

    code = ErrorCode.NOT_READY_FOR_PUBLICATION

    def __init__(self, issues: tuple[str, ...]) -> None:
        super().__init__("the reviewed draft is not ready for publication")
        self.issues = issues


@dataclass(frozen=True, slots=True)
class BuiltConnector:
    """A validated connector document and the digest that identifies it."""

    document: dict[str, Any]
    digest: str
    tool_count: int


def build_connector_document(
    *,
    connector_key: str,
    display_name: str,
    summary: str,
    version: str,
    analysis: dict[str, Any],
    review: ReviewState,
    base_url: str | None,
    auth_profile_ref: str | None,
    source_filename: str,
    source_digest: str,
    source_byte_length: int,
    spec_version: str,
    created_at: datetime,
    published_at: datetime,
    labels: tuple[str, ...] = (),
) -> BuiltConnector:
    """Rebuild, validate, and digest the connector a reviewed draft describes."""
    readiness = review_readiness(analysis, review, base_url=base_url)
    if not readiness.ready:
        raise NotReadyForPublication(readiness.issues)
    assert base_url is not None  # guaranteed by the readiness check

    analyzed = {
        str(operation["key"]): operation
        for operation in analysis.get("operations", [])
        if operation.get("tool") is not None
    }

    tools: list[dict[str, Any]] = []
    for entry in review.included:
        operation = analyzed.get(entry.operation_key)
        if operation is None:
            raise NotReadyForPublication(("publication.review_out_of_sync",))

        # Deep-copied so that building a document never mutates the stored analysis. The
        # same draft can be published more than once (a failed attempt, then a retry), and
        # the second attempt must see the same input as the first.
        tool = copy.deepcopy(operation["tool"])
        tool["description"] = entry.description
        tool["policy"] = {
            "effect_class": entry.effect_class,
            "requires_confirmation": entry.requires_confirmation,
            "access": entry.access_policy().model_dump(mode="json"),
        }
        tool["provenance"]["description_origin"] = entry.description_origin
        tools.append(tool)

    if not tools:
        raise NotReadyForPublication(("publication.no_operation_selected",))

    names = [tool["tool_name"] for tool in tools]
    if len(set(names)) != len(names):
        raise NotReadyForPublication(("publication.tool_name_collision",))

    document: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "connector_key": connector_key,
        "display_name": display_name,
        "summary": summary,
        "version": version,
        "lifecycle_state": "published",
        "runtime": {
            "protocol": "http",
            "base_url": base_url,
            "auth_profile_ref": auth_profile_ref,
        },
        "source": {
            "format": "openapi",
            "spec_version": spec_version,
            "document_filename": source_filename,
            "document_digest": source_digest,
            "byte_length": source_byte_length,
        },
        "tools": tools,
        "labels": list(labels),
        "audit": {
            "created_at": _rfc3339(created_at),
            "updated_at": _rfc3339(published_at),
            "published_at": _rfc3339(published_at),
        },
    }

    # Validated against the schema, not only against the models that built it. The schema is
    # what a consumer in another language would check, so validating here means a document
    # that this service accepts is one any conforming consumer will also accept.
    validate_connector_definition(document)

    return BuiltConnector(
        document=document,
        digest=content_digest(document),
        tool_count=len(tools),
    )


def _rfc3339(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")
