"""Typed models for the ToolLayer contract family.

The JSON Schemas in ``schemas/`` are the normative contract: they are what a consumer in
another language would validate against, and they are what the contract tests check. These
Pydantic models are the *in-process* representation used by the services, and they are kept
deliberately in step with the schemas — ``tests/contract`` fails if they drift.

Both representations exist because they do different jobs. The schema is portable and can
reject an untrusted document without instantiating anything. The model gives the services
attribute access, static typing, and constructor-time invariants.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from toollayer_contracts.version import CONTRACT_VERSION

__all__ = [
    "ArgumentBinding",
    "AuditTimestamps",
    "ConnectorDefinition",
    "DeploymentSnapshot",
    "ErrorBody",
    "ErrorEnvelopeModel",
    "RuntimeBinding",
    "SourceProvenance",
    "ToolAccessPolicy",
    "ToolDefinition",
    "ToolExecutionRequest",
    "ToolExecutionResult",
    "ToolOperation",
    "ToolPolicy",
    "ToolProvenance",
]

SEMVER_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
KEY_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
JSON_POINTER_PATTERN = r"^(?:/(?:[^~/]|~[01])*)*$"
DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"

SemanticVersion = Annotated[str, Field(pattern=SEMVER_PATTERN, max_length=64)]
ResourceKey = Annotated[str, Field(pattern=KEY_PATTERN, min_length=1, max_length=64)]
ToolName = Annotated[str, Field(pattern=TOOL_NAME_PATTERN, min_length=1, max_length=64)]
JsonPointer = Annotated[str, Field(pattern=JSON_POINTER_PATTERN)]
Digest = Annotated[str, Field(pattern=DIGEST_PATTERN)]

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
SourceMethod = Literal["get", "post", "put", "patch", "delete"]
EffectClass = Literal["read", "write", "destructive"]
BindingTarget = Literal["path", "query", "header", "body"]
DescriptionOrigin = Literal["source", "generated", "assisted", "human"]
LifecycleState = Literal["draft", "published", "disabled"]


class _Strict(BaseModel):
    """Every contract model rejects fields it does not declare.

    An unexpected field is either a producer bug or an attempt to smuggle data past
    review. Neither should be silently dropped.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class ArgumentBinding(_Strict):
    """Where one validated argument goes in the outbound HTTP request."""

    argument_pointer: JsonPointer
    target: BindingTarget
    target_name: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _body_targets_use_pointers(self) -> ArgumentBinding:
        if self.target == "body" and not self.target_name.startswith("/"):
            raise ValueError("a body binding target_name must be an RFC 6901 pointer")
        if self.target != "body" and "\n" in self.target_name:
            raise ValueError("a parameter name may not contain a line break")
        return self


class ToolOperation(_Strict):
    """The reviewed HTTP operation a tool is bound to."""

    protocol: Literal["http"] = "http"
    method: HttpMethod
    path_template: str = Field(min_length=1, max_length=2048, pattern=r"^/(?:[^/?#\s][^?#\s]*)?$")
    bindings: tuple[ArgumentBinding, ...] = ()
    request_body_media_type: Literal["application/json"] | None = None

    @model_validator(mode="after")
    def _bindings_are_consistent(self) -> ToolOperation:
        seen: set[tuple[str, str]] = set()
        for binding in self.bindings:
            key = (binding.target, binding.target_name)
            if key in seen:
                raise ValueError("two arguments bind to the same request location")
            seen.add(key)
        has_body = any(binding.target == "body" for binding in self.bindings)
        if has_body and self.request_body_media_type is None:
            raise ValueError("a body binding requires a request_body_media_type")
        if self.request_body_media_type is not None and not has_body:
            raise ValueError("a request_body_media_type requires at least one body binding")
        return self


class ToolAccessPolicy(_Strict):
    """Which callers may use one tool.

    ``public`` carries no role list at all. Keeping a stale list behind ``public`` would
    make the stored policy disagree with the enforced one the moment somebody flipped the
    mode back, so the ambiguity is rejected rather than ignored.
    """

    access_mode: Literal["public", "restricted"] = "public"
    allowed_roles: tuple[ResourceKey, ...] = ()

    @model_validator(mode="after")
    def _mode_and_roles_agree(self) -> ToolAccessPolicy:
        if self.access_mode == "public" and self.allowed_roles:
            raise ValueError("a public tool cannot carry a role list")
        if self.access_mode == "restricted" and not self.allowed_roles:
            raise ValueError("a restricted tool must allow at least one role")
        if len(set(self.allowed_roles)) != len(self.allowed_roles):
            raise ValueError("allowed_roles contains a duplicate")
        return self

    @field_validator("allowed_roles", mode="after")
    @classmethod
    def _sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # Sorted so that two policies selecting the same roles serialize to the same bytes
        # and therefore produce the same published digest.
        return tuple(sorted(value))

    @property
    def restricted(self) -> bool:
        return self.access_mode == "restricted"


class ToolPolicy(_Strict):
    """What calling a tool does, and who may call it."""

    effect_class: EffectClass = "read"
    requires_confirmation: bool = False
    access: ToolAccessPolicy = ToolAccessPolicy()


class ToolProvenance(_Strict):
    """Where a tool came from, so a reviewer can trace it back to the source document."""

    source_operation_id: str | None = Field(default=None, max_length=256)
    source_path: str = Field(min_length=1, max_length=2048)
    source_method: SourceMethod
    tags: tuple[str, ...] = ()
    deprecated: bool = False
    description_origin: DescriptionOrigin = "source"


class ToolDefinition(_Strict):
    """One provider-neutral tool."""

    tool_name: ToolName
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1024)
    input_schema: dict[str, Any]
    operation: ToolOperation
    policy: ToolPolicy = ToolPolicy()
    provenance: ToolProvenance

    @field_validator("input_schema", mode="after")
    @classmethod
    def _closed_object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("input_schema must describe an object")
        if value.get("additionalProperties") is not False:
            raise ValueError("input_schema must set additionalProperties to false")
        if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("input_schema must declare the Draft 2020-12 dialect")
        if not isinstance(value.get("properties"), dict):
            raise ValueError("input_schema must declare a properties object")
        if not isinstance(value.get("required"), list):
            raise ValueError("input_schema must declare a required array")
        return value

    @model_validator(mode="after")
    def _bindings_reference_declared_arguments(self) -> ToolDefinition:
        properties = self.input_schema.get("properties", {})
        declared = {f"/{name}" for name in properties}
        for binding in self.operation.bindings:
            root = "/" + binding.argument_pointer.lstrip("/").split("/", 1)[0]
            if root not in declared:
                raise ValueError("a binding reads an argument the input schema does not declare")
        return self


class RuntimeBinding(_Strict):
    """Where the tools in a connector are executed."""

    protocol: Literal["http"] = "http"
    base_url: str = Field(max_length=2048)
    auth_profile_ref: str | None = Field(default=None, max_length=256)

    @field_validator("base_url", mode="after")
    @classmethod
    def _reviewed_origin(cls, value: str) -> str:
        """Accept only a plain http(s) origin with an optional base path.

        Userinfo (``https://user:pass@host``) is rejected outright: it is a credential in a
        field that is published in plain text, and it is also the classic way to make a URL
        look like it points somewhere it does not. Query strings and fragments are rejected
        because the operation's own bindings own everything after the path.
        """
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("base_url must use http or https")
        if not parsed.netloc or "@" in parsed.netloc:
            raise ValueError("base_url must name a host and must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query string or fragment")
        return value


class SourceProvenance(_Strict):
    """The API description this connector was derived from."""

    format: Literal["openapi"] = "openapi"
    spec_version: str = Field(min_length=1, max_length=16)
    document_filename: str = Field(min_length=1, max_length=256)
    document_digest: Digest
    byte_length: int = Field(ge=1)


class AuditTimestamps(_Strict):
    created_at: str
    updated_at: str
    published_at: str | None = None


class ConnectorDefinition(_Strict):
    """One versioned bundle of tools derived from one API description."""

    contract_version: SemanticVersion = CONTRACT_VERSION
    connector_key: ResourceKey
    display_name: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=1024)
    version: SemanticVersion
    lifecycle_state: LifecycleState = "draft"
    runtime: RuntimeBinding
    source: SourceProvenance
    tools: tuple[ToolDefinition, ...] = Field(min_length=1)
    labels: tuple[str, ...] = ()
    audit: AuditTimestamps

    @model_validator(mode="after")
    def _tool_names_are_unique(self) -> ConnectorDefinition:
        names = [tool.tool_name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("tool names must be unique within a connector version")
        return self

    def tool(self, tool_name: str) -> ToolDefinition | None:
        for candidate in self.tools:
            if candidate.tool_name == tool_name:
                return candidate
        return None


class DeploymentSnapshot(_Strict):
    """The immutable set of connector versions one deployment may serve."""

    contract_version: SemanticVersion = CONTRACT_VERSION
    snapshot_id: str = Field(pattern=r"^snap_[0-9a-f]{32}$")
    deployment_key: ResourceKey
    revision: int = Field(ge=1)
    created_at: str
    snapshot_digest: Digest
    connectors: tuple[ConnectorDefinition, ...] = ()

    @model_validator(mode="after")
    def _one_version_per_connector(self) -> DeploymentSnapshot:
        keys = [connector.connector_key for connector in self.connectors]
        if len(set(keys)) != len(keys):
            raise ValueError("a snapshot may pin only one version per connector")
        for connector in self.connectors:
            if connector.lifecycle_state != "published":
                raise ValueError("a snapshot may contain only published connector versions")
        return self


class ToolExecutionRequest(_Strict):
    """A request to execute one governed tool."""

    connector_key: ResourceKey
    connector_version: SemanticVersion
    tool_name: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)
    caller_roles: tuple[ResourceKey, ...] = ()
    request_id: str | None = Field(default=None, max_length=64)


class ToolExecutionResult(_Strict):
    """The outcome of an execution that actually reached the upstream API.

    A call rejected before execution never produces one of these; it produces an error
    envelope. That asymmetry is deliberate: a result means the upstream was contacted.
    """

    tool_name: ToolName
    connector_key: ResourceKey
    connector_version: SemanticVersion
    status: Literal["succeeded", "upstream_error"]
    http_status: int = Field(ge=100, le=599)
    duration_ms: int = Field(ge=0)
    content: Any
    untrusted: Literal[True] = True
    request_id: str | None = Field(default=None, max_length=64)


class ErrorBody(_Strict):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    message: str = Field(min_length=1, max_length=512)
    pointer: str | None = None
    details: tuple[dict[str, Any], ...] = ()
    request_id: str | None = None


class ErrorEnvelopeModel(_Strict):
    error: ErrorBody
