"""Fetching, verifying, and holding a deployment snapshot.

The runtime holds an immutable snapshot in memory and refreshes it on a schedule. Four
properties make that safe rather than merely fast.

**Its content is checked.** Every fetch is validated against the contract schema and its
SHA-256 digest is recomputed. That catches corruption in transit or storage, and a payload
edited without its digest being updated. It does not catch an attacker who rewrites both,
because computing SHA-256 requires no secret.

**Its producer is authenticated.** In the default ``required`` verification mode the snapshot
must also carry an Ed25519 signature made by a key this runtime was configured to trust. That
is the control that holds against an active attacker: substituting the payload *and* its
digest still leaves a signature that will not verify. Unsigned operation exists for the
offline demonstration, must be asked for explicitly, and is reported by ``/healthz``.

**It is replaced, never mutated.** A refresh builds a new object and swaps the reference under
a lock. The tool index is a read-only mapping, so a caller holding a reference cannot alter
what a later request will see. A request that started with revision 4 finishes with revision
4, so a tool cannot change definition halfway through the call that is executing it.

**Its freshness is checked, not assumed.** Refresh uses ``If-None-Match``, so the common case
costs one ``304`` and the runtime learns that nothing changed rather than guessing.

None of this replaces transport security. TLS authenticates the *service* and protects
confidentiality on the wire; the signature authenticates the *artifact* and keeps holding
after it has been cached, mirrored, or relayed. A real deployment needs both.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

import httpx

from toollayer_contracts import (
    SNAPSHOT_DIGEST_EXCLUDED,
    ConnectorDefinition,
    DeploymentSnapshot,
    ToolDefinition,
    validate_deployment_snapshot,
    verify_digest,
)
from toollayer_contracts.errors import ErrorCode, NotFoundError, ToolLayerError
from toollayer_contracts.signing import (
    EMPTY_KEY_RING,
    SignatureVerificationError,
    TrustedKeyRing,
    verify_document,
)
from toollayer_contracts.version import IncompatibleContractVersionError, require_supported

__all__ = [
    "BoundTool",
    "LoadedSnapshot",
    "SnapshotClient",
    "SnapshotStore",
    "SnapshotVerification",
    "VerificationMode",
    "load_snapshot_document",
]

logger = logging.getLogger("toollayer.runtime.snapshot")

#: ``required`` authenticates the producer and is the default. ``disabled`` accepts an
#: unsigned snapshot and exists for the offline demonstration; it has to be asked for by name,
#: and a runtime in that mode says so in its health output.
VerificationMode = Literal["required", "disabled"]


class SnapshotError(ToolLayerError):
    code = ErrorCode.SNAPSHOT_UNAVAILABLE


class SnapshotIntegrityError(ToolLayerError):
    code = ErrorCode.SNAPSHOT_INTEGRITY_FAILED


class SnapshotSignatureError(ToolLayerError):
    """The content was intact but the producer could not be authenticated."""

    code = ErrorCode.SNAPSHOT_SIGNATURE_INVALID


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    """How this runtime decides whether a snapshot's producer is acceptable.

    There is no permissive fallback. ``required`` with an empty key ring is a
    misconfiguration that fails every load rather than quietly degrading to ``disabled`` —
    a fallback that turns itself off when it is hardest to configure is not a control.
    """

    mode: VerificationMode = "required"
    trusted_keys: TrustedKeyRing = EMPTY_KEY_RING

    @property
    def enforced(self) -> bool:
        return self.mode == "required"

    def check(self, document: dict[str, Any]) -> str | None:
        """Authenticate the producer, returning the key id that signed, or ``None``.

        ``None`` is returned only in ``disabled`` mode, and only after recording that an
        unauthenticated artifact was accepted.
        """
        if not self.enforced:
            if document.get("signature") is not None:
                # Still verified when a signature happens to be present and the key is
                # known: being lenient about *absence* is not a reason to ignore a signature
                # that is there and wrong.
                try:
                    return verify_document(document, self.trusted_keys)
                except SignatureVerificationError as error:
                    raise SnapshotSignatureError(str(error)) from None
            logger.warning(
                "accepting an unsigned deployment snapshot: signature verification is "
                "disabled for this runtime"
            )
            return None
        try:
            return verify_document(document, self.trusted_keys)
        except SignatureVerificationError as error:
            # The message names the failure and, at most, a key id. It never contains the
            # signature bytes or any part of the payload.
            raise SnapshotSignatureError(str(error)) from None


@dataclass(frozen=True, slots=True)
class BoundTool:
    """One tool plus everything needed to execute it.

    Dispatch identity is the triple ``(connector_key, connector_version, tool_name)``. The
    tool name alone is not identity: two connectors may both publish ``list_tickets``, and
    two versions of one connector may define the same name differently.
    """

    connector_key: str
    connector_version: str
    base_url: str
    tool: ToolDefinition

    @property
    def tool_name(self) -> str:
        return self.tool.tool_name

    @property
    def qualified_name(self) -> str:
        return f"{self.connector_key}:{self.tool.tool_name}"


@dataclass(frozen=True, slots=True)
class LoadedSnapshot:
    """An immutable, verified snapshot indexed for dispatch.

    Immutability here is deep, not just frozen at the top level. ``tools_by_name`` is a
    read-only mapping, and the contract models it holds are themselves frozen — so a caller
    that keeps a reference to a snapshot, or to its index, cannot change what any other
    request sees through it. A frozen dataclass wrapping a plain ``dict`` would have given
    the appearance of that guarantee without the substance.
    """

    snapshot: DeploymentSnapshot
    etag: str | None
    loaded_at: float
    tools_by_name: Mapping[str, BoundTool]
    #: The key that authenticated this snapshot's producer, or ``None`` when it was accepted
    #: unsigned in an explicitly configured demonstration mode.
    signing_key_id: str | None = None

    @property
    def revision(self) -> int:
        return self.snapshot.revision

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id

    @property
    def digest(self) -> str:
        return self.snapshot.snapshot_digest

    @property
    def signed(self) -> bool:
        return self.signing_key_id is not None

    @property
    def tools(self) -> tuple[BoundTool, ...]:
        return tuple(self.tools_by_name.values())

    def resolve(self, tool_name: str) -> BoundTool:
        """Resolve a tool name against this snapshot, or refuse.

        A name that is not in the snapshot is refused outright. This is the check that stops
        a fabricated or hallucinated tool name from reaching anything — there is no fallback
        lookup, no fuzzy match, and no path that treats an unknown name as a request to
        construct something.
        """
        bound = self.tools_by_name.get(tool_name)
        if bound is None:
            raise NotFoundError(
                "the requested tool is not present in this deployment snapshot",
                code=ErrorCode.UNKNOWN_TOOL,
            )
        return bound


def _index(snapshot: DeploymentSnapshot) -> Mapping[str, BoundTool]:
    """Build the read-only dispatch index, refusing an ambiguous tool name.

    Refusing is deliberate. Silently keeping one of two same-named tools would make dispatch
    depend on connector ordering, and a caller asking for ``list_tickets`` would reach a
    different API depending on which connector happened to be first.
    """
    index: dict[str, BoundTool] = {}
    for connector in snapshot.connectors:
        for tool in connector.tools:
            if tool.tool_name in index:
                raise SnapshotIntegrityError(
                    "the snapshot defines the same tool name in more than one connector"
                )
            index[tool.tool_name] = BoundTool(
                connector_key=connector.connector_key,
                connector_version=connector.version,
                base_url=connector.runtime.base_url,
                tool=tool,
            )
    return MappingProxyType(index)


def load_snapshot_document(
    document: Any,
    *,
    etag: str | None = None,
    verification: SnapshotVerification | None = None,
) -> LoadedSnapshot:
    """Validate, verify, authenticate, and index a snapshot document.

    The order is deliberate: shape, then contract version, then schema, then content digest,
    then producer signature. Each step is cheaper than the next and each one narrows what the
    following step has to reason about, so a malformed payload never reaches the
    cryptographic layer.
    """
    if not isinstance(document, dict):
        raise SnapshotIntegrityError("the snapshot payload is not a JSON object")

    try:
        require_supported(document.get("contract_version"))
    except IncompatibleContractVersionError as error:
        raise ToolLayerError(
            str(error), code=ErrorCode.UNSUPPORTED_CONTRACT_VERSION, pointer="/contract_version"
        ) from None

    validate_deployment_snapshot(document)

    declared = str(document["snapshot_digest"])
    if not verify_digest(document, declared, exclude=SNAPSHOT_DIGEST_EXCLUDED):
        # The digest is recomputed rather than taken on faith. This proves the bytes are the
        # ones the digest was taken over — not who produced them; that is the next step.
        raise SnapshotIntegrityError("the snapshot content does not match the digest it declares")

    signing_key_id = (verification or SnapshotVerification()).check(document)

    snapshot = DeploymentSnapshot.model_validate(document)
    for connector in snapshot.connectors:
        ConnectorDefinition.model_validate(connector.model_dump(mode="json"))

    return LoadedSnapshot(
        snapshot=snapshot,
        etag=etag,
        loaded_at=time.monotonic(),
        tools_by_name=_index(snapshot),
        signing_key_id=signing_key_id,
    )


class SnapshotClient:
    """Fetches deployment snapshots from the Control Plane's internal API."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
        verification: SnapshotVerification | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout = timeout_seconds
        self._client = client
        self._verification = verification or SnapshotVerification()

    @property
    def verification(self) -> SnapshotVerification:
        return self._verification

    def fetch(
        self, deployment_key: str, *, if_none_match: str | None = None
    ) -> LoadedSnapshot | None:
        """Fetch the active snapshot, or return ``None`` when it is unchanged."""
        url = f"{self._base_url}/internal/v1/deployments/{deployment_key}/snapshot"
        headers = {"x-toollayer-service-token": self._service_token}
        if if_none_match:
            headers["if-none-match"] = if_none_match

        client = self._client or httpx.Client(
            timeout=self._timeout, follow_redirects=False, trust_env=False
        )
        try:
            response = client.get(url, headers=headers)
        except httpx.HTTPError:
            raise SnapshotError("the control plane could not be reached") from None
        finally:
            if self._client is None:
                client.close()

        if response.status_code == 304:
            return None
        if response.status_code == 404:
            raise SnapshotError("the deployment has no snapshot to serve")
        if response.status_code in (401, 403):
            raise ToolLayerError(
                "the runtime is not authorized to read this deployment's snapshot",
                code=ErrorCode.UNAUTHENTICATED,
            )
        if response.status_code >= 400:
            raise SnapshotError("the control plane rejected the snapshot request")

        return load_snapshot_document(
            response.json(),
            etag=response.headers.get("etag"),
            verification=self._verification,
        )


class SnapshotStore:
    """Holds the current snapshot and refreshes it on demand.

    Reads are lock-free: the current snapshot is an immutable object referenced by one
    attribute, and refresh replaces the reference. A reader therefore either sees the old
    object or the new one, and never a half-updated index.
    """

    def __init__(self, client: SnapshotClient, *, deployment_key: str, refresh_seconds: int = 60):
        self._client = client
        self._deployment_key = deployment_key
        self._refresh_seconds = max(refresh_seconds, 1)
        self._current: LoadedSnapshot | None = None
        self._refresh_lock = threading.Lock()

    @property
    def current(self) -> LoadedSnapshot:
        """Return the loaded snapshot, refusing to serve if there is none."""
        snapshot = self._current
        if snapshot is None:
            raise SnapshotError("the runtime has no deployment snapshot; it cannot serve any tool")
        return snapshot

    @property
    def loaded(self) -> bool:
        return self._current is not None

    def ensure_fresh(self) -> LoadedSnapshot:
        """Refresh if the held snapshot is older than the refresh interval.

        Call this **once** per logical request and pass the result down. Calling it again
        mid-request can hand a later step a different revision from the one an earlier step
        reasoned about, which is how a request ends up authorizing against one policy and
        executing against another.
        """
        snapshot = self._current
        if snapshot is not None and (time.monotonic() - snapshot.loaded_at) < self._refresh_seconds:
            return snapshot
        return self.refresh()

    def refresh(self) -> LoadedSnapshot:
        """Re-read the snapshot, keeping the current one if the new one is unusable."""
        with self._refresh_lock:
            existing = self._current
            try:
                fetched = self._client.fetch(
                    self._deployment_key,
                    if_none_match=existing.etag if existing else None,
                )
            except ToolLayerError:
                if existing is None:
                    raise
                # A refresh failure is not a serving failure. The held snapshot is immutable
                # and was verified when it was loaded, so continuing to serve it is strictly
                # better than refusing every request because the control plane is briefly
                # unavailable. This covers a rejected signature too: a snapshot that fails
                # authentication never replaces one that passed it.
                logger.warning(
                    "snapshot refresh failed; continuing to serve revision %s",
                    existing.revision,
                )
                return existing

            if fetched is None:
                assert existing is not None  # a 304 requires a prior ETag
                refreshed = LoadedSnapshot(
                    snapshot=existing.snapshot,
                    etag=existing.etag,
                    loaded_at=time.monotonic(),
                    tools_by_name=existing.tools_by_name,
                    signing_key_id=existing.signing_key_id,
                )
                self._current = refreshed
                return refreshed

            logger.info(
                "loaded snapshot revision %s with %s tool(s), signed by %s",
                fetched.revision,
                len(fetched.tools_by_name),
                fetched.signing_key_id or "nobody (unsigned)",
            )
            self._current = fetched
            return fetched

    def set(self, snapshot: LoadedSnapshot) -> None:
        """Install a snapshot directly. Used by tests and by the offline CLI."""
        with self._refresh_lock:
            self._current = snapshot
