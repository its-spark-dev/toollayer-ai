"""The Control Plane's use cases.

Everything the API can do lives here as a function that takes a session and returns domain
objects. Handlers stay thin: they translate HTTP into arguments, call one of these, and
translate the result back. Business rules never live in a route, so they can be tested
without a client and reused by the demo script and the CLI.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from toollayer_contracts import (
    CONTRACT_VERSION,
    ConnectorDefinition,
    canonical_json,
    content_digest,
    parse_version,
    validate_deployment_snapshot,
)
from toollayer_contracts.errors import (
    ConflictError,
    ErrorCode,
    ImmutabilityError,
    NotFoundError,
    RevisionConflictError,
    ValidationError,
)
from toollayer_contracts.version import compare_precedence
from toollayer_openapi import SourceLimits, analyze_document, load_document
from control_plane.models import (
    Connector,
    ConnectorDraft,
    Deployment,
    DeploymentSnapshot,
    PublishedVersion,
    utc_now,
)
from control_plane.publication import build_connector_document
from control_plane.review import ReviewState, ReviewUpdate, apply_update, build_initial_review
from control_plane.serialization import serialize_analysis

__all__ = [
    "SnapshotSelection",
    "create_deployment",
    "create_snapshot",
    "disable_version",
    "get_active_snapshot",
    "get_connector",
    "get_draft",
    "get_version",
    "list_connectors",
    "list_deployments",
    "list_versions",
    "publish_draft",
    "register_connector",
    "update_review",
]


# --------------------------------------------------------------------------------------
# Connectors and drafts
# --------------------------------------------------------------------------------------


def register_connector(
    session: Session,
    *,
    connector_key: str,
    display_name: str | None,
    summary: str | None,
    source_bytes: bytes,
    source_filename: str,
    proposed_version: str = "0.1.0",
    base_url_override: str | None = None,
    auth_profile_ref: str | None = None,
    max_source_bytes: int = 2 * 1024 * 1024,
) -> ConnectorDraft:
    """Ingest an API description and produce a reviewable draft.

    Registering the same connector twice replaces its draft rather than failing. Re-uploading
    a corrected specification is the normal way to fix an analysis problem, and forcing the
    reviewer to delete the old draft first would add a step that only ever has one answer.
    Published versions are untouched by this — they are immutable and live in separate rows.
    """
    parse_version(proposed_version)

    loaded = load_document(
        source_bytes,
        filename=source_filename,
        limits=SourceLimits(max_bytes=max_source_bytes),
    )
    analysis = analyze_document(loaded, base_url_override=base_url_override)

    connector = session.scalar(
        select(Connector).where(Connector.connector_key == connector_key)
    )
    if connector is None:
        connector = Connector(
            connector_key=connector_key,
            display_name=display_name or analysis.api_title,
            summary=summary or analysis.api_summary,
        )
        session.add(connector)
        session.flush()
    else:
        if display_name:
            connector.display_name = display_name
        if summary:
            connector.summary = summary
        connector.updated_at = utc_now()

    _reject_version_regression(session, connector, proposed_version)

    serialized = serialize_analysis(analysis)
    review = build_initial_review(serialized)

    if connector.draft is not None:
        session.delete(connector.draft)
        session.flush()

    draft = ConnectorDraft(
        connector_id=connector.id,
        revision=1,
        proposed_version=proposed_version,
        base_url=analysis.base_url,
        auth_profile_ref=auth_profile_ref,
        source_bytes=source_bytes,
        source_filename=loaded.filename,
        source_digest=loaded.digest,
        source_byte_length=loaded.byte_length,
        source_format=loaded.source_format,
        spec_version=loaded.spec_version,
        analyzer_version=analysis.analyzer_version,
        analysis=serialized,
        review=review.to_dict(),
    )
    session.add(draft)
    session.flush()
    return draft


def list_connectors(session: Session) -> list[Connector]:
    return list(session.scalars(select(Connector).order_by(Connector.connector_key)))


def get_connector(session: Session, connector_key: str) -> Connector:
    connector = session.scalar(
        select(Connector).where(Connector.connector_key == connector_key)
    )
    if connector is None:
        raise NotFoundError("no connector exists with that key")
    return connector


def get_draft(session: Session, connector_key: str) -> ConnectorDraft:
    connector = get_connector(session, connector_key)
    if connector.draft is None:
        raise NotFoundError("the connector has no draft; register a source document first")
    return connector.draft


def update_review(
    session: Session,
    *,
    connector_key: str,
    expected_revision: int,
    updates: tuple[ReviewUpdate, ...],
    proposed_version: str | None = None,
    base_url: str | None = None,
    auth_profile_ref: str | None = None,
) -> ConnectorDraft:
    """Apply review decisions under optimistic concurrency.

    The caller sends the revision it read. A mismatch is rejected rather than merged: two
    reviewers editing the same draft would otherwise silently overwrite each other, and the
    loser would never learn that their decision was discarded.
    """
    draft = get_draft(session, connector_key)
    if expected_revision != draft.revision:
        raise RevisionConflictError(
            "the draft changed since it was read; reload it and reapply the change",
            pointer="/expected_revision",
        )

    state = ReviewState.from_dict(draft.review)
    for update in updates:
        state = apply_update(state, update)
    draft.review = state.to_dict()

    if proposed_version is not None:
        parse_version(proposed_version)
        _reject_version_regression(session, draft.connector, proposed_version)
        draft.proposed_version = proposed_version
    if base_url is not None:
        draft.base_url = base_url
    if auth_profile_ref is not None:
        draft.auth_profile_ref = auth_profile_ref or None

    draft.revision += 1
    draft.updated_at = utc_now()
    session.flush()
    return draft


# --------------------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------------------


def publish_draft(
    session: Session,
    *,
    connector_key: str,
    expected_revision: int,
    published_by: str,
    version: str | None = None,
) -> PublishedVersion:
    """Publish the reviewed draft as an immutable version, then consume the draft."""
    draft = get_draft(session, connector_key)
    if expected_revision != draft.revision:
        raise RevisionConflictError(
            "the draft changed since it was read; reload it and publish again",
            pointer="/expected_revision",
        )

    target_version = version or draft.proposed_version
    parse_version(target_version)
    _reject_version_regression(session, draft.connector, target_version)

    existing = session.scalar(
        select(PublishedVersion).where(
            PublishedVersion.connector_id == draft.connector_id,
            PublishedVersion.version == target_version,
        )
    )
    if existing is not None:
        raise ImmutabilityError(
            "that version is already published; publish a new version instead",
            pointer="/version",
        )

    published_at = utc_now()
    built = build_connector_document(
        connector_key=draft.connector.connector_key,
        display_name=draft.connector.display_name,
        summary=draft.connector.summary,
        version=target_version,
        analysis=draft.analysis,
        review=ReviewState.from_dict(draft.review),
        base_url=draft.base_url,
        auth_profile_ref=draft.auth_profile_ref,
        source_filename=draft.source_filename,
        source_digest=draft.source_digest,
        source_byte_length=draft.source_byte_length,
        spec_version=draft.spec_version,
        created_at=draft.created_at,
        published_at=published_at,
    )

    version_row = PublishedVersion(
        connector_id=draft.connector_id,
        version=target_version,
        document=built.document,
        document_digest=built.digest,
        source_digest=draft.source_digest,
        analyzer_version=draft.analyzer_version,
        tool_count=built.tool_count,
        published_at=published_at,
        published_by=published_by,
    )
    session.add(version_row)

    # The draft is consumed. Keeping it would leave an editable copy of something that has
    # already been published, and the next edit would look like it was changing the
    # published version when it was not.
    session.delete(draft)

    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ImmutabilityError(
            "that version is already published; publish a new version instead",
            pointer="/version",
        ) from None

    return version_row


def list_versions(session: Session, connector_key: str) -> list[PublishedVersion]:
    connector = get_connector(session, connector_key)
    return list(
        session.scalars(
            select(PublishedVersion)
            .where(PublishedVersion.connector_id == connector.id)
            .order_by(PublishedVersion.published_at)
        )
    )


def get_version(session: Session, connector_key: str, version: str) -> PublishedVersion:
    connector = get_connector(session, connector_key)
    row = session.scalar(
        select(PublishedVersion).where(
            PublishedVersion.connector_id == connector.id,
            PublishedVersion.version == version,
        )
    )
    if row is None:
        raise NotFoundError("no published version exists with that number")
    return row


def disable_version(
    session: Session, *, connector_key: str, version: str, reason: str
) -> PublishedVersion:
    """Stop a published version being served without altering its content.

    Disablement changes availability, not bytes, so the artifact's digest still verifies
    afterwards. A snapshot that already pins the version keeps working until it is rebuilt —
    stopping in-flight deployments is a runtime revalidation concern, not something this
    endpoint can promise.
    """
    row = get_version(session, connector_key, version)
    if row.disabled:
        return row
    row.disabled_at = utc_now()
    row.disabled_reason = reason[:512]
    session.flush()
    return row


def _reject_version_regression(session: Session, connector: Connector, version: str) -> None:
    """Refuse a version that does not exceed everything already published.

    Publishing 0.1.0 after 0.2.0 would make "latest" ambiguous and would let a snapshot pin a
    version number that means something different from what a reader expects. Semantic
    Versioning only helps if the sequence is actually monotonic.
    """
    if connector.id is None:
        return
    candidate = parse_version(version)
    published = session.scalars(
        select(PublishedVersion.version).where(PublishedVersion.connector_id == connector.id)
    ).all()
    for existing in published:
        if compare_precedence(candidate, parse_version(existing)) <= 0:
            raise ValidationError(
                f"version must be greater than the published version {existing}",
                pointer="/version",
            )


# --------------------------------------------------------------------------------------
# Deployments and snapshots
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotSelection:
    """One connector version a snapshot should pin."""

    connector_key: str
    version: str


def create_deployment(
    session: Session, *, deployment_key: str, display_name: str, description: str = ""
) -> Deployment:
    existing = session.scalar(
        select(Deployment).where(Deployment.deployment_key == deployment_key)
    )
    if existing is not None:
        raise ConflictError("a deployment already exists with that key", pointer="/deployment_key")
    deployment = Deployment(
        deployment_key=deployment_key,
        display_name=display_name,
        description=description,
    )
    session.add(deployment)
    session.flush()
    return deployment


def list_deployments(session: Session) -> list[Deployment]:
    return list(session.scalars(select(Deployment).order_by(Deployment.deployment_key)))


def get_deployment(session: Session, deployment_key: str) -> Deployment:
    deployment = session.scalar(
        select(Deployment).where(Deployment.deployment_key == deployment_key)
    )
    if deployment is None:
        raise NotFoundError("no deployment exists with that key")
    return deployment


def create_snapshot(
    session: Session,
    *,
    deployment_key: str,
    selections: tuple[SnapshotSelection, ...],
    created_by: str,
) -> DeploymentSnapshot:
    """Build the next immutable snapshot for a deployment.

    Each selection is resolved to an exact published version and embedded whole. The runtime
    therefore needs one request to know everything it may serve, and it can verify what it
    received without trusting the transport — the digest covers the entire document
    including every embedded connector.
    """
    deployment = get_deployment(session, deployment_key)

    seen: set[str] = set()
    connectors: list[dict[str, Any]] = []
    for selection in selections:
        if selection.connector_key in seen:
            raise ValidationError(
                "a snapshot may pin only one version per connector", pointer="/selections"
            )
        seen.add(selection.connector_key)

        row = get_version(session, selection.connector_key, selection.version)
        if row.disabled:
            raise ValidationError(
                f"the version {selection.connector_key} {selection.version} is disabled and "
                "cannot be added to a snapshot",
                pointer="/selections",
            )
        # Re-validated on the way out. The row was validated when it was written, but a
        # snapshot is what a different service consumes, and checking here means a
        # corrupted row is caught before it is distributed rather than after.
        ConnectorDefinition.model_validate(row.document)
        connectors.append(row.document)

    connectors.sort(key=lambda document: str(document["connector_key"]))

    revision = 1 + (
        session.scalar(
            select(DeploymentSnapshot.revision)
            .where(DeploymentSnapshot.deployment_id == deployment.id)
            .order_by(DeploymentSnapshot.revision.desc())
            .limit(1)
        )
        or 0
    )
    created_at = utc_now()

    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "deployment_key": deployment.deployment_key,
        "revision": revision,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "connectors": connectors,
    }
    # The identifier is derived from the content, not allocated: two snapshots with the same
    # content and revision are the same snapshot, and the id says so.
    digest = content_digest(payload)
    snapshot_id = "snap_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:32]
    document = {**payload, "snapshot_id": snapshot_id, "snapshot_digest": digest}
    validate_deployment_snapshot(document)

    for previous in deployment.snapshots:
        previous.active = False

    snapshot = DeploymentSnapshot(
        deployment_id=deployment.id,
        revision=revision,
        snapshot_id=snapshot_id,
        document=document,
        snapshot_digest=digest,
        connector_count=len(connectors),
        active=True,
        created_at=created_at,
        created_by=created_by,
    )
    session.add(snapshot)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ConflictError(
            "another snapshot was created concurrently; retry the request"
        ) from None
    return snapshot


def get_active_snapshot(session: Session, deployment_key: str) -> DeploymentSnapshot:
    deployment = get_deployment(session, deployment_key)
    snapshot = session.scalar(
        select(DeploymentSnapshot)
        .where(
            DeploymentSnapshot.deployment_id == deployment.id,
            DeploymentSnapshot.active.is_(True),
        )
        .order_by(DeploymentSnapshot.revision.desc())
        .limit(1)
    )
    if snapshot is None:
        raise NotFoundError(
            "the deployment has no snapshot yet",
            code=ErrorCode.SNAPSHOT_UNAVAILABLE,
        )
    return snapshot


def list_snapshots(session: Session, deployment_key: str) -> list[DeploymentSnapshot]:
    deployment = get_deployment(session, deployment_key)
    return list(
        session.scalars(
            select(DeploymentSnapshot)
            .where(DeploymentSnapshot.deployment_id == deployment.id)
            .order_by(DeploymentSnapshot.revision)
        )
    )
