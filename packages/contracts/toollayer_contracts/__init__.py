"""ToolLayer shared contracts.

This package owns the provider-neutral representation that the Tool Control Plane produces
and the LLM Orchestration Runtime consumes. It contains the normative JSON Schemas, typed
models, deterministic serialization, the shared error shape, and the provider adapters.

It contains **no business logic**. The Control Plane and the Runtime never import each
other; this package is the only thing they share, and it stays small on purpose. Putting
service behavior here would recreate the coupling the boundary exists to prevent.

The contract defined here is specific to this project. It is not an industry standard, and
no interoperability is claimed with anything outside this repository.
"""

from __future__ import annotations

from toollayer_contracts.canonical_json import (
    canonical_bytes,
    canonical_json,
    content_digest,
    digest_of,
    verify_digest,
)
from toollayer_contracts.errors import (
    ErrorCode,
    ErrorDetail,
    ErrorEnvelope,
    ToolLayerError,
    error_response,
    status_for,
)
from toollayer_contracts.models import (
    ArgumentBinding,
    AuditTimestamps,
    ConnectorDefinition,
    DeploymentSnapshot,
    RuntimeBinding,
    SourceProvenance,
    ToolAccessPolicy,
    ToolDefinition,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolOperation,
    ToolPolicy,
    ToolProvenance,
)
from toollayer_contracts.validation import (
    load_schema,
    schema_names,
    validate_connector_definition,
    validate_deployment_snapshot,
    validate_error_envelope,
    validate_tool_definition,
    validate_tool_input_schema,
)
from toollayer_contracts.version import (
    CONTRACT_VERSION,
    ContractVersion,
    IncompatibleContractVersionError,
    compare_precedence,
    is_supported,
    parse_version,
    require_supported,
)

__all__ = [
    "CONTRACT_VERSION",
    "ArgumentBinding",
    "AuditTimestamps",
    "ConnectorDefinition",
    "ContractVersion",
    "DeploymentSnapshot",
    "ErrorCode",
    "ErrorDetail",
    "ErrorEnvelope",
    "IncompatibleContractVersionError",
    "RuntimeBinding",
    "SourceProvenance",
    "ToolAccessPolicy",
    "ToolDefinition",
    "ToolExecutionRequest",
    "ToolExecutionResult",
    "ToolLayerError",
    "ToolOperation",
    "ToolPolicy",
    "ToolProvenance",
    "canonical_bytes",
    "canonical_json",
    "compare_precedence",
    "content_digest",
    "digest_of",
    "error_response",
    "is_supported",
    "load_schema",
    "parse_version",
    "require_supported",
    "schema_names",
    "status_for",
    "validate_connector_definition",
    "validate_deployment_snapshot",
    "validate_error_envelope",
    "validate_tool_definition",
    "validate_tool_input_schema",
    "verify_digest",
]
