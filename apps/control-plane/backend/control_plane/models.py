"""The Control Plane's persistence model.

The model is organized around one distinction: **what may change, and what may not.**

Mutable rows (``Connector``, ``ConnectorDraft``) are the authoring workspace. They carry a
``revision`` and are updated in place under optimistic concurrency.

Immutable rows (``PublishedVersion``, ``DeploymentSnapshot``) are artifacts. They store a
complete, self-contained contract document plus its digest, and they are never updated after
insert. "Changing" a published version means publishing a new one; "changing" a snapshot
means creating the next revision. Every uniqueness rule that protects that is a database
constraint rather than an application check, because an application check does not survive
two concurrent requests.

Storing the whole artifact as a JSON document rather than as normalized rows is deliberate.
A published version has to be byte-reproducible to be digest-verifiable, and reassembling it
from a dozen tables would make the digest depend on the schema of the day.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = [
    "Base",
    "Connector",
    "ConnectorDraft",
    "Deployment",
    "DeploymentSnapshot",
    "PublishedVersion",
    "utc_now",
]


def utc_now() -> datetime:
    """Return the current UTC time, truncated to whole seconds.

    Truncated because these timestamps are serialized into artifacts whose digests must
    match across processes, and sub-second precision differs between database backends.
    """
    return datetime.now(UTC).replace(microsecond=0)


class Base(DeclarativeBase):
    pass


class Connector(Base):
    """A named integration. Its versions are separate, immutable rows."""

    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    draft: Mapped[ConnectorDraft | None] = relationship(
        back_populates="connector", uselist=False, cascade="all, delete-orphan"
    )
    versions: Mapped[list[PublishedVersion]] = relationship(
        back_populates="connector", cascade="all, delete-orphan", order_by="PublishedVersion.id"
    )


class ConnectorDraft(Base):
    """The mutable authoring workspace for one connector.

    Exactly one draft per connector, enforced by a unique constraint. Publication consumes
    the draft, so a connector with no draft is one whose last proposal was published.
    """

    __tablename__ = "connector_drafts"
    __table_args__ = (UniqueConstraint("connector_id", name="uq_draft_per_connector"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False
    )

    #: Incremented on every accepted mutation. A client sends the revision it read, and a
    #: mismatch is rejected — so two reviewers editing the same draft cannot silently
    #: overwrite each other.
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    proposed_version: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    auth_profile_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)

    #: The original uploaded bytes, kept verbatim. Analysis is reproducible from them, and a
    #: reviewer can always see exactly what was submitted rather than a reserialization.
    source_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    source_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    source_byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    source_format: Mapped[str] = mapped_column(String(8), nullable=False)
    spec_version: Mapped[str] = mapped_column(String(16), nullable=False)

    analyzer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    review: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    connector: Mapped[Connector] = relationship(back_populates="draft")


class PublishedVersion(Base):
    """One immutable published connector version.

    ``document`` holds the complete contract document. It is written once and never updated;
    the only mutable field is ``disabled_at``, and that changes *availability*, not content,
    which is why the digest still verifies after a version is disabled.
    """

    __tablename__ = "published_versions"
    __table_args__ = (
        UniqueConstraint("connector_id", "version", name="uq_connector_version"),
        Index("ix_published_versions_connector", "connector_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)

    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    document_digest: Mapped[str] = mapped_column(String(80), nullable=False)

    source_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_count: Mapped[int] = mapped_column(Integer, nullable=False)

    published_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    published_by: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Disablement is the one lifecycle change a published version permits. It is recorded
    #: rather than applied by deletion, so an artifact that was once served can still be
    #: identified after it stops being servable.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    connector: Mapped[Connector] = relationship(back_populates="versions")

    @property
    def disabled(self) -> bool:
        return self.disabled_at is not None


class Deployment(Base):
    """A named runtime deployment that snapshots are created for."""

    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deployment_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    snapshots: Mapped[list[DeploymentSnapshot]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
        order_by="DeploymentSnapshot.revision",
    )


class DeploymentSnapshot(Base):
    """One immutable snapshot of what a deployment may serve.

    Revisions are unique per deployment and never reused, so a runtime that reports
    ``revision 4`` is describing exactly one artifact for all time.
    """

    __tablename__ = "deployment_snapshots"
    __table_args__ = (
        UniqueConstraint("deployment_id", "revision", name="uq_deployment_revision"),
        UniqueConstraint("snapshot_id", name="uq_snapshot_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)

    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    connector_count: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The active snapshot is the one the internal read API serves. Exactly one snapshot per
    #: deployment is active; activating a new one deactivates the previous, and the
    #: deactivated revision stays queryable.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)

    deployment: Mapped[Deployment] = relationship(back_populates="snapshots")
