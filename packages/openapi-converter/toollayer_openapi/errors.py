"""Failures raised while turning an API description into tool definitions.

Every failure carries a stable code and an RFC 6901 pointer into the *source document*, so
a reviewer can jump straight to the operation that caused it. No failure message ever
contains a value read out of the source document: these documents are uploaded by users and
their contents end up in logs, consoles, and API responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ConversionError",
    "Diagnostic",
    "DocumentTooLargeError",
    "InvalidDocumentError",
    "Severity",
    "UnsupportedFeatureError",
]

Severity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One thing that is wrong, or worth knowing, about one part of a document.

    Diagnostics are how the converter reports a *partial* result. A document with twelve
    operations where two cannot be converted yields ten tools and two diagnostics — not an
    exception. Refusing the whole document because one operation uses an unsupported
    feature would make the tool useless against real-world API descriptions.
    """

    code: str
    message: str
    pointer: str = ""
    severity: Severity = "error"
    operation_key: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "pointer": self.pointer,
            "severity": self.severity,
            "operation_key": self.operation_key,
        }


class ConversionError(ValueError):
    """Base class for failures that stop conversion entirely."""

    code = "invalid_source_document"

    def __init__(self, message: str, *, pointer: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.pointer = pointer

    def as_diagnostic(self, operation_key: str | None = None) -> Diagnostic:
        return Diagnostic(
            code=self.code,
            message=self.message,
            pointer=self.pointer,
            severity="error",
            operation_key=operation_key,
        )


class InvalidDocumentError(ConversionError):
    """The document is not a usable OpenAPI description."""

    code = "invalid_source_document"


class UnsupportedFeatureError(ConversionError):
    """The document uses a feature this converter deliberately refuses to guess about.

    Refusing is the point. A converter that quietly approximates ``oneOf`` or invents a
    serialization style produces a tool that a model will call and that will then fail, or
    worse, succeed with the wrong request. An explicit refusal is reviewable; a silent
    approximation is not.
    """

    code = "unsupported_spec_feature"


class DocumentTooLargeError(ConversionError):
    """The document exceeds a configured ingestion limit."""

    code = "source_too_large"
