"""Administrator API: the authoring, review, publication, and deployment surface.

Every route here is thin. It decodes a request, calls one service function, and projects the
result — so the rules that matter are testable without an HTTP client, and no route can
quietly grow a rule of its own.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from control_plane import service
from control_plane.db import get_session
from control_plane.dependencies import AdminDep, SettingsDep
from control_plane.models import Connector, ConnectorDraft, Deployment, DeploymentSnapshot
from control_plane.review import ReviewState, ReviewUpdate, review_readiness
from control_plane.schemas import (
    ConnectorSummary,
    CreateDeploymentRequest,
    CreateSnapshotRequest,
    DisableVersionRequest,
    DraftResponse,
    PublishRequest,
    RegisterConnectorRequest,
    UpdateDraftRequest,
    VersionSummary,
)
from toollayer_contracts.adapters import SUPPORTED_PROVIDERS, get_adapter
from toollayer_contracts.errors import NotFoundError, ValidationError
from toollayer_contracts.models import ConnectorDefinition

router = APIRouter(prefix="/admin/v1", tags=["admin"])

SessionDep = Annotated[Session, Depends(get_session)]


# --------------------------------------------------------------------------------------
# Connectors
# --------------------------------------------------------------------------------------


@router.get("/connectors", response_model=list[ConnectorSummary])
def list_connectors(session: SessionDep, _: AdminDep) -> list[ConnectorSummary]:
    return [_connector_summary(connector) for connector in service.list_connectors(session)]


@router.post("/connectors", response_model=DraftResponse, status_code=201)
def register_connector(
    payload: RegisterConnectorRequest,
    session: SessionDep,
    settings: SettingsDep,
    _: AdminDep,
) -> DraftResponse:
    """Register an API description and analyze it into a reviewable draft."""
    draft = service.register_connector(
        session,
        connector_key=payload.connector_key,
        display_name=payload.display_name,
        summary=payload.summary,
        source_bytes=payload.document.encode("utf-8"),
        source_filename=payload.document_filename,
        proposed_version=payload.proposed_version,
        base_url_override=payload.base_url,
        auth_profile_ref=payload.auth_profile_ref,
        max_source_bytes=settings.max_source_bytes,
    )
    return _draft_response(draft)


@router.get("/connectors/{connector_key}", response_model=ConnectorSummary)
def get_connector(connector_key: str, session: SessionDep, _: AdminDep) -> ConnectorSummary:
    return _connector_summary(service.get_connector(session, connector_key))


@router.get("/connectors/{connector_key}/draft", response_model=DraftResponse)
def get_draft(connector_key: str, session: SessionDep, _: AdminDep) -> DraftResponse:
    return _draft_response(service.get_draft(session, connector_key))


@router.patch("/connectors/{connector_key}/draft", response_model=DraftResponse)
def update_draft(
    connector_key: str,
    payload: UpdateDraftRequest,
    session: SessionDep,
    _: AdminDep,
) -> DraftResponse:
    """Apply review decisions to a draft under optimistic concurrency."""
    updates = tuple(
        ReviewUpdate(
            operation_key=entry.operation_key,
            selection=entry.selection,
            description=entry.description,
            description_origin=entry.description_origin,
            effect_class=entry.effect_class,
            requires_confirmation=entry.requires_confirmation,
            access_mode=entry.access_mode,
            allowed_roles=tuple(entry.allowed_roles) if entry.allowed_roles is not None else None,
        )
        for entry in payload.operations
    )
    draft = service.update_review(
        session,
        connector_key=connector_key,
        expected_revision=payload.expected_revision,
        updates=updates,
        proposed_version=payload.proposed_version,
        base_url=payload.base_url,
        auth_profile_ref=payload.auth_profile_ref,
    )
    return _draft_response(draft)


# --------------------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------------------


@router.post(
    "/connectors/{connector_key}/publish", response_model=VersionSummary, status_code=201
)
def publish(
    connector_key: str,
    payload: PublishRequest,
    session: SessionDep,
    _: AdminDep,
) -> VersionSummary:
    """Publish the reviewed draft as an immutable version."""
    version = service.publish_draft(
        session,
        connector_key=connector_key,
        expected_revision=payload.expected_revision,
        published_by=payload.published_by,
        version=payload.version,
    )
    return _version_summary(version)


@router.get("/connectors/{connector_key}/versions", response_model=list[VersionSummary])
def list_versions(connector_key: str, session: SessionDep, _: AdminDep) -> list[VersionSummary]:
    return [_version_summary(row) for row in service.list_versions(session, connector_key)]


@router.get("/connectors/{connector_key}/versions/{version}")
def get_version(
    connector_key: str, version: str, session: SessionDep, _: AdminDep
) -> dict[str, Any]:
    """Return the exact published contract document."""
    row = service.get_version(session, connector_key, version)
    return {
        "summary": _version_summary(row).model_dump(),
        "document": row.document,
    }


@router.post(
    "/connectors/{connector_key}/versions/{version}/disable", response_model=VersionSummary
)
def disable_version(
    connector_key: str,
    version: str,
    payload: DisableVersionRequest,
    session: SessionDep,
    _: AdminDep,
) -> VersionSummary:
    row = service.disable_version(
        session, connector_key=connector_key, version=version, reason=payload.reason
    )
    return _version_summary(row)


@router.get("/connectors/{connector_key}/versions/{version}/adapters/{provider}")
def preview_adapter(
    connector_key: str,
    version: str,
    provider: str,
    session: SessionDep,
    _: AdminDep,
) -> dict[str, Any]:
    """Show what a published version looks like in one provider's tool format.

    This exists to make the provider-neutrality claim inspectable rather than asserted. A
    reviewer can see the canonical definition and both projections of it side by side, and
    can see which tools a given provider cannot represent and why.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise ValidationError(
            f"unsupported provider; expected one of {', '.join(SUPPORTED_PROVIDERS)}",
            pointer="/provider",
        )
    row = service.get_version(session, connector_key, version)
    definition = ConnectorDefinition.model_validate(row.document)
    projection = get_adapter(provider).project(definition.tools)
    return {
        "provider": projection.provider,
        "connector_key": connector_key,
        "version": version,
        "complete": projection.complete,
        "tools": list(projection.tools),
        "diagnostics": [
            {
                "tool_name": diagnostic.tool_name,
                "code": diagnostic.code,
                "message": diagnostic.message,
                "pointer": diagnostic.pointer,
            }
            for diagnostic in projection.diagnostics
        ],
    }


# --------------------------------------------------------------------------------------
# Deployments
# --------------------------------------------------------------------------------------


@router.get("/deployments")
def list_deployments(session: SessionDep, _: AdminDep) -> list[dict[str, Any]]:
    return [_deployment_summary(deployment) for deployment in service.list_deployments(session)]


@router.post("/deployments", status_code=201)
def create_deployment(
    payload: CreateDeploymentRequest, session: SessionDep, _: AdminDep
) -> dict[str, Any]:
    deployment = service.create_deployment(
        session,
        deployment_key=payload.deployment_key,
        display_name=payload.display_name,
        description=payload.description,
    )
    return _deployment_summary(deployment)


@router.post("/deployments/{deployment_key}/snapshots", status_code=201)
def create_snapshot(
    deployment_key: str,
    payload: CreateSnapshotRequest,
    session: SessionDep,
    _: AdminDep,
) -> dict[str, Any]:
    """Create the next immutable snapshot for a deployment."""
    snapshot = service.create_snapshot(
        session,
        deployment_key=deployment_key,
        selections=tuple(
            service.SnapshotSelection(
                connector_key=selection.connector_key, version=selection.version
            )
            for selection in payload.selections
        ),
        created_by=payload.created_by,
    )
    return _snapshot_summary(snapshot)


@router.get("/deployments/{deployment_key}/snapshots")
def list_snapshots(
    deployment_key: str,
    session: SessionDep,
    _: AdminDep,
) -> list[dict[str, Any]]:
    snapshots = service.list_snapshots(session, deployment_key)
    return [_snapshot_summary(snapshot) for snapshot in snapshots]


@router.get("/deployments/{deployment_key}/snapshots/{revision}")
def get_snapshot(
    deployment_key: str,
    revision: int,
    session: SessionDep,
    _: AdminDep,
    include_document: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    for snapshot in service.list_snapshots(session, deployment_key):
        if snapshot.revision == revision:
            body = _snapshot_summary(snapshot)
            if include_document:
                body["document"] = snapshot.document
            return body
    raise NotFoundError("no snapshot exists with that revision")


# --------------------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connector_summary(connector: Connector) -> ConnectorSummary:
    versions = [row.version for row in connector.versions]
    return ConnectorSummary(
        connector_key=connector.connector_key,
        display_name=connector.display_name,
        summary=connector.summary,
        has_draft=connector.draft is not None,
        draft_revision=connector.draft.revision if connector.draft else None,
        published_versions=versions,
        latest_version=versions[-1] if versions else None,
        created_at=_iso(connector.created_at),
        updated_at=_iso(connector.updated_at),
    )


def _draft_response(draft: ConnectorDraft) -> DraftResponse:
    state = ReviewState.from_dict(draft.review)
    readiness = review_readiness(draft.analysis, state, base_url=draft.base_url)
    return DraftResponse(
        connector_key=draft.connector.connector_key,
        display_name=draft.connector.display_name,
        summary=draft.connector.summary,
        revision=draft.revision,
        proposed_version=draft.proposed_version,
        base_url=draft.base_url,
        auth_profile_ref=draft.auth_profile_ref,
        source={
            "filename": draft.source_filename,
            "digest": draft.source_digest,
            "byte_length": draft.source_byte_length,
            "format": draft.source_format,
            "spec_version": draft.spec_version,
        },
        analyzer_version=draft.analyzer_version,
        analysis=draft.analysis,
        review=draft.review,
        readiness={"ready": readiness.ready, "issues": list(readiness.issues)},
        updated_at=_iso(draft.updated_at),
    )


def _version_summary(row: Any) -> VersionSummary:
    document = row.document
    return VersionSummary(
        connector_key=str(document["connector_key"]),
        version=row.version,
        document_digest=row.document_digest,
        tool_count=row.tool_count,
        tool_names=[str(tool["tool_name"]) for tool in document.get("tools", [])],
        published_at=_iso(row.published_at),
        published_by=row.published_by,
        disabled=row.disabled,
        disabled_reason=row.disabled_reason,
    )


def _deployment_summary(deployment: Deployment) -> dict[str, Any]:
    active = next((snapshot for snapshot in deployment.snapshots if snapshot.active), None)
    return {
        "deployment_key": deployment.deployment_key,
        "display_name": deployment.display_name,
        "description": deployment.description,
        "created_at": _iso(deployment.created_at),
        "snapshot_count": len(deployment.snapshots),
        "active_revision": active.revision if active else None,
        "active_snapshot_id": active.snapshot_id if active else None,
    }


def _snapshot_summary(snapshot: DeploymentSnapshot) -> dict[str, Any]:
    document = snapshot.document
    tool_count = sum(
        len(connector.get("tools", [])) for connector in document.get("connectors", [])
    )
    return {
        "deployment_key": str(document["deployment_key"]),
        "revision": snapshot.revision,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_digest": snapshot.snapshot_digest,
        "connector_count": snapshot.connector_count,
        "tool_count": tool_count,
        "active": snapshot.active,
        "created_at": _iso(snapshot.created_at),
        "created_by": snapshot.created_by,
    }
