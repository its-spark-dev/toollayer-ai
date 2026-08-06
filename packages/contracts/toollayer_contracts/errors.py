"""The single failure shape used across every ToolLayer service.

Two rules govern everything here.

First, callers branch on ``code``, never on ``message``. Codes are stable; messages are
free to improve.

Second, a message describes the *rule* that failed and *where*, and never the value that
failed it. Echoing rejected input into an error is how secrets, tokens, and internal
hostnames end up in logs and in a user's browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "ErrorCode",
    "ErrorDetail",
    "ErrorEnvelope",
    "ToolLayerError",
    "error_response",
]

Severity = Literal["error", "warning"]


class ErrorCode:
    """Stable error codes. Grouped by the boundary that raises them."""

    # Request shape and contract conformance
    INVALID_REQUEST: Final = "invalid_request"
    CONTRACT_VIOLATION: Final = "contract_violation"
    UNSUPPORTED_CONTRACT_VERSION: Final = "unsupported_contract_version"

    # Source document ingestion
    INVALID_SOURCE_DOCUMENT: Final = "invalid_source_document"
    UNSUPPORTED_SPEC_FEATURE: Final = "unsupported_spec_feature"
    SOURCE_TOO_LARGE: Final = "source_too_large"

    # Lifecycle
    NOT_FOUND: Final = "not_found"
    ALREADY_EXISTS: Final = "already_exists"
    REVISION_CONFLICT: Final = "revision_conflict"
    IMMUTABLE_VERSION: Final = "immutable_version"
    INVALID_LIFECYCLE_TRANSITION: Final = "invalid_lifecycle_transition"
    NOT_READY_FOR_PUBLICATION: Final = "not_ready_for_publication"

    # Authentication and authorization
    UNAUTHENTICATED: Final = "unauthenticated"
    FORBIDDEN: Final = "forbidden"
    ROLE_NOT_PERMITTED: Final = "role_not_permitted"

    # Runtime execution
    SNAPSHOT_UNAVAILABLE: Final = "snapshot_unavailable"
    SNAPSHOT_INTEGRITY_FAILED: Final = "snapshot_integrity_failed"
    #: The content matched its digest but the producer could not be authenticated. Distinct
    #: from SNAPSHOT_INTEGRITY_FAILED because the two say different things to an operator:
    #: one means "these bytes are damaged", the other means "these bytes are not ours".
    SNAPSHOT_SIGNATURE_INVALID: Final = "snapshot_signature_invalid"
    UNKNOWN_TOOL: Final = "unknown_tool"
    ARGUMENT_VALIDATION_FAILED: Final = "argument_validation_failed"
    NO_TOOL_SELECTED: Final = "no_tool_selected"
    CONFIRMATION_REQUIRED: Final = "confirmation_required"

    # Execution policy
    DESTINATION_NOT_ALLOWED: Final = "destination_not_allowed"
    METHOD_NOT_ALLOWED: Final = "method_not_allowed"
    PRIVATE_ADDRESS_BLOCKED: Final = "private_address_blocked"
    REDIRECT_NOT_ALLOWED: Final = "redirect_not_allowed"
    RESPONSE_TOO_LARGE: Final = "response_too_large"
    UPSTREAM_TIMEOUT: Final = "upstream_timeout"
    UPSTREAM_UNAVAILABLE: Final = "upstream_unavailable"

    INTERNAL_ERROR: Final = "internal_error"


#: HTTP status for each code. Anything unlisted is reported as 500, because an
#: unmapped code means the caller learned about a failure this module did not model.
_STATUS_BY_CODE: Final[dict[str, int]] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.CONTRACT_VIOLATION: 422,
    ErrorCode.UNSUPPORTED_CONTRACT_VERSION: 409,
    ErrorCode.INVALID_SOURCE_DOCUMENT: 422,
    ErrorCode.UNSUPPORTED_SPEC_FEATURE: 422,
    ErrorCode.SOURCE_TOO_LARGE: 413,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.ALREADY_EXISTS: 409,
    ErrorCode.REVISION_CONFLICT: 409,
    ErrorCode.IMMUTABLE_VERSION: 409,
    ErrorCode.INVALID_LIFECYCLE_TRANSITION: 409,
    ErrorCode.NOT_READY_FOR_PUBLICATION: 422,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.ROLE_NOT_PERMITTED: 403,
    ErrorCode.SNAPSHOT_UNAVAILABLE: 503,
    ErrorCode.SNAPSHOT_INTEGRITY_FAILED: 502,
    ErrorCode.SNAPSHOT_SIGNATURE_INVALID: 502,
    ErrorCode.UNKNOWN_TOOL: 404,
    ErrorCode.ARGUMENT_VALIDATION_FAILED: 422,
    ErrorCode.NO_TOOL_SELECTED: 422,
    ErrorCode.CONFIRMATION_REQUIRED: 409,
    ErrorCode.DESTINATION_NOT_ALLOWED: 403,
    ErrorCode.METHOD_NOT_ALLOWED: 403,
    ErrorCode.PRIVATE_ADDRESS_BLOCKED: 403,
    ErrorCode.REDIRECT_NOT_ALLOWED: 502,
    ErrorCode.RESPONSE_TOO_LARGE: 502,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
    ErrorCode.UPSTREAM_UNAVAILABLE: 502,
    ErrorCode.INTERNAL_ERROR: 500,
}


def status_for(code: str) -> int:
    """Return the HTTP status that represents ``code``."""
    return _STATUS_BY_CODE.get(code, 500)


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """One diagnostic inside a batch failure, such as a per-operation analysis issue."""

    code: str
    message: str
    pointer: str | None = None
    severity: Severity = "error"

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "pointer": self.pointer,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    """The wire representation of a failure."""

    code: str
    message: str
    pointer: str | None = None
    details: tuple[ErrorDetail, ...] = ()
    request_id: str | None = None

    @property
    def http_status(self) -> int:
        return status_for(self.code)

    def to_dict(self) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.pointer is not None:
            error["pointer"] = self.pointer
        if self.details:
            error["details"] = [detail.to_dict() for detail in self.details]
        if self.request_id is not None:
            error["request_id"] = self.request_id
        return {"error": error}


class ToolLayerError(Exception):
    """Base class for failures that map cleanly onto an error envelope.

    Every service raises subclasses of this and converts them at exactly one place, so no
    handler has to remember which status code belongs to which failure.
    """

    code: str = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        pointer: str | None = None,
        details: tuple[ErrorDetail, ...] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.pointer = pointer
        self.details = details

    @property
    def http_status(self) -> int:
        return status_for(self.code)

    def to_envelope(self, request_id: str | None = None) -> ErrorEnvelope:
        return ErrorEnvelope(
            code=self.code,
            message=self.message,
            pointer=self.pointer,
            details=self.details,
            request_id=request_id,
        )


def error_response(
    code: str,
    message: str,
    *,
    pointer: str | None = None,
    details: tuple[ErrorDetail, ...] = (),
    request_id: str | None = None,
) -> tuple[int, dict[str, object]]:
    """Build ``(http_status, body)`` for a failure without needing an exception."""
    envelope = ErrorEnvelope(
        code=code,
        message=message,
        pointer=pointer,
        details=details,
        request_id=request_id,
    )
    return envelope.http_status, envelope.to_dict()


# Concrete exception types the services raise. Each exists so a caller can catch a
# category rather than string-matching a code.


class ValidationError(ToolLayerError):
    code = ErrorCode.INVALID_REQUEST


class ContractViolationError(ToolLayerError):
    code = ErrorCode.CONTRACT_VIOLATION


class NotFoundError(ToolLayerError):
    code = ErrorCode.NOT_FOUND


class ConflictError(ToolLayerError):
    code = ErrorCode.ALREADY_EXISTS


class RevisionConflictError(ToolLayerError):
    code = ErrorCode.REVISION_CONFLICT


class ImmutabilityError(ToolLayerError):
    code = ErrorCode.IMMUTABLE_VERSION


class AuthorizationError(ToolLayerError):
    code = ErrorCode.FORBIDDEN


class PolicyDenied(ToolLayerError):
    """Execution policy refused the call. The code names which rule refused it."""

    code = ErrorCode.DESTINATION_NOT_ALLOWED

    def __init__(self, code: str, message: str, *, pointer: str | None = None) -> None:
        super().__init__(message, code=code, pointer=pointer)
