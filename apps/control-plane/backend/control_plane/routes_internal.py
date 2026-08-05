"""Internal API: the read-only surface the Runtime consumes.

This is the entire contact surface between the two services. It is read-only, it is
versioned in its path, and it serves exactly one thing: the active deployment snapshot.

Conditional requests are supported because the runtime holds a snapshot for a long time and
polls to find out whether it is still current. A strong ETag over the snapshot digest lets
that poll cost one small ``304`` instead of re-transferring every connector definition — and
it lets the runtime prove nothing changed, rather than assume it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.orm import Session

from control_plane import service
from control_plane.db import get_session
from control_plane.dependencies import ServiceDep

router = APIRouter(prefix="/internal/v1", tags=["internal"])

SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/deployments/{deployment_key}/snapshot")
def read_snapshot(
    deployment_key: str,
    response: Response,
    session: SessionDep,
    _: ServiceDep,
    if_none_match: Annotated[str | None, Header(alias="if-none-match")] = None,
) -> Any:
    """Return the active snapshot for a deployment, or ``304`` when it is unchanged."""
    snapshot = service.get_active_snapshot(session, deployment_key)

    # The digest already identifies the exact content, so it *is* the entity tag. Deriving
    # the tag from anything else — a row id, a timestamp — would let two different documents
    # share a tag, which is the one thing an ETag must never do.
    etag = f'"{snapshot.snapshot_digest}"'
    response.headers["etag"] = etag
    response.headers["cache-control"] = "no-cache"

    if if_none_match is not None and _matches(if_none_match, etag):
        response.status_code = 304
        return Response(status_code=304, headers=dict(response.headers))

    return snapshot.document


def _matches(header_value: str, etag: str) -> bool:
    """Compare an ``If-None-Match`` header against one strong entity tag.

    ``*`` matches any existing representation, per HTTP semantics. Weak comparison is not
    used: a weak match would mean "semantically equivalent", and for an integrity-checked
    artifact only byte equality is meaningful.
    """
    candidates = [candidate.strip() for candidate in header_value.split(",")]
    return any(candidate == "*" or candidate == etag for candidate in candidates)
