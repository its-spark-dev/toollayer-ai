"""Deterministic OpenAPI-to-tool conversion.

This package turns an uploaded OpenAPI 3.0 or 3.1 description into provider-neutral tool
definitions. It is pure: no network access, no filesystem access, no clock, no database, and
no global state. The same bytes always produce the same tools, which is what makes a
published artifact's digest meaningful.

Typical use::

    loaded = load_document(raw_bytes, filename="support-api.yaml")
    result = analyze_document(loaded)
    for operation in result.operations:
        ...  # operation.tool is a ToolDefinition, or None with diagnostics explaining why
"""

from __future__ import annotations

from toollayer_openapi.analyzer import (
    ANALYZER_VERSION,
    AnalysisResult,
    AnalyzedOperation,
    analyze_document,
)
from toollayer_openapi.converter import (
    BODY_ARGUMENT,
    SUPPORTED_METHODS,
    convert_operation,
    operation_key,
)
from toollayer_openapi.errors import (
    ConversionError,
    Diagnostic,
    DocumentTooLargeError,
    InvalidDocumentError,
    UnsupportedFeatureError,
)
from toollayer_openapi.loader import LoadedDocument, SourceLimits, load_document
from toollayer_openapi.naming import derive_tool_name, normalize_tool_name
from toollayer_openapi.references import ReferenceResolver
from toollayer_openapi.schema_conversion import convert_schema

__all__ = [
    "ANALYZER_VERSION",
    "BODY_ARGUMENT",
    "SUPPORTED_METHODS",
    "AnalysisResult",
    "AnalyzedOperation",
    "ConversionError",
    "Diagnostic",
    "DocumentTooLargeError",
    "InvalidDocumentError",
    "LoadedDocument",
    "ReferenceResolver",
    "SourceLimits",
    "UnsupportedFeatureError",
    "analyze_document",
    "convert_operation",
    "convert_schema",
    "derive_tool_name",
    "load_document",
    "normalize_tool_name",
    "operation_key",
]
