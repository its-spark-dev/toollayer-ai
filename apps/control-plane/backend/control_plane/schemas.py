"""Request and response models for the Control Plane HTTP API.

These are separate from the contract models on purpose. The contract models describe
artifacts that cross a service boundary and must stay stable; these describe this service's
own HTTP surface, which is free to change. Collapsing them would tie the published contract
to the shape of an admin console request.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from toollayer_contracts.models import KEY_PATTERN, SEMVER_PATTERN

__all__ = [
    "ConnectorSummary",
    "CreateDeploymentRequest",
    "CreateSnapshotRequest",
    "DeploymentSummary",
    "DraftResponse",
    "PublishRequest",
    "RegisterConnectorRequest",
    "ReviewUpdateRequest",
    "SnapshotSelectionRequest",
    "SnapshotSummary",
    "UpdateDraftRequest",
    "VersionSummary",
]

ResourceKey = Annotated[str, Field(pattern=KEY_PATTERN, min_length=1, max_length=64)]
SemanticVersion = Annotated[str, Field(pattern=SEMVER_PATTERN, max_length=64)]


class _Request(BaseModel):
    """Requests reject unknown fields so a typo fails loudly instead of being ignored."""

    model_config = ConfigDict(extra="forbid")


class RegisterConnectorRequest(_Request):
    connector_key: ResourceKey
    display_name: str | None = Field(default=None, max_length=128)
    summary: str | None = Field(default=None, max_length=1024)
    proposed_version: SemanticVersion = "0.1.0"
    base_url: str | None = Field(default=None, max_length=2048)
    auth_profile_ref: str | None = Field(default=None, max_length=256)
    #: The API description, as UTF-8 text. Sent inline rather than as a multipart upload so
    #: the whole demo can be driven with a plain JSON client.
    document: str = Field(min_length=1)
    document_filename: str = Field(default="openapi.yaml", max_length=256)


class ReviewUpdateRequest(_Request):
    operation_key: str = Field(min_length=1, max_length=2100)
    selection: Literal["included", "excluded"] | None = None
    description: str | None = Field(default=None, max_length=1024)
    description_origin: Literal["source", "generated", "assisted", "human"] | None = None
    effect_class: Literal["read", "write", "destructive"] | None = None
    requires_confirmation: bool | None = None
    access_mode: Literal["public", "restricted"] | None = None
    allowed_roles: list[ResourceKey] | None = Field(default=None, max_length=32)


class UpdateDraftRequest(_Request):
    expected_revision: int = Field(ge=1)
    operations: list[ReviewUpdateRequest] = Field(default_factory=list, max_length=256)
    proposed_version: SemanticVersion | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    auth_profile_ref: str | None = Field(default=None, max_length=256)


class PublishRequest(_Request):
    expected_revision: int = Field(ge=1)
    version: SemanticVersion | None = None
    published_by: str = Field(default="admin", max_length=128)


class DisableVersionRequest(_Request):
    reason: str = Field(min_length=1, max_length=512)


class CreateDeploymentRequest(_Request):
    deployment_key: ResourceKey
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)


class SnapshotSelectionRequest(_Request):
    connector_key: ResourceKey
    version: SemanticVersion


class CreateSnapshotRequest(_Request):
    selections: list[SnapshotSelectionRequest] = Field(min_length=1, max_length=64)
    created_by: str = Field(default="admin", max_length=128)


class ConnectorSummary(BaseModel):
    connector_key: str
    display_name: str
    summary: str
    has_draft: bool
    draft_revision: int | None
    published_versions: list[str]
    latest_version: str | None
    created_at: str
    updated_at: str


class VersionSummary(BaseModel):
    connector_key: str
    version: str
    document_digest: str
    tool_count: int
    tool_names: list[str]
    published_at: str
    published_by: str
    disabled: bool
    disabled_reason: str | None


class DraftResponse(BaseModel):
    connector_key: str
    display_name: str
    summary: str
    revision: int
    proposed_version: str
    base_url: str | None
    auth_profile_ref: str | None
    source: dict[str, Any]
    analyzer_version: str
    analysis: dict[str, Any]
    review: dict[str, Any]
    readiness: dict[str, Any]
    updated_at: str


class DeploymentSummary(BaseModel):
    deployment_key: str
    display_name: str
    description: str
    created_at: str
    snapshot_count: int
    active_revision: int | None
    active_snapshot_id: str | None


class SnapshotSummary(BaseModel):
    deployment_key: str
    revision: int
    snapshot_id: str
    snapshot_digest: str
    connector_count: int
    tool_count: int
    active: bool
    created_at: str
    created_by: str
