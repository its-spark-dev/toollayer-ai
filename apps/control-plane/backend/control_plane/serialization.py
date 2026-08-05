"""Turning an in-memory analysis into the JSON a draft stores.

Analysis is stored rather than recomputed on every read for two reasons. It makes the review
console fast, and — more importantly — it pins the proposal a reviewer actually saw. If
analysis re-ran on read, a converter upgrade could change what a reviewer is looking at
between the moment they approve and the moment publication builds the artifact.
"""

from __future__ import annotations

from typing import Any

from toollayer_openapi import AnalysisResult

__all__ = ["serialize_analysis"]


def serialize_analysis(result: AnalysisResult) -> dict[str, Any]:
    """Serialize an analysis result into a stored JSON document."""
    return {
        "analyzer_version": result.analyzer_version,
        "spec_version": result.spec_version,
        "api_title": result.api_title,
        "api_summary": result.api_summary,
        "base_url": result.base_url,
        "diagnostics": [diagnostic.to_dict() for diagnostic in result.diagnostics],
        "operations": [
            {
                "key": operation.key,
                "path": operation.path,
                "method": operation.method,
                "pointer": operation.pointer,
                # The source operation is kept alongside the generated tool so the console
                # can show them side by side. Seeing the input next to the output is what
                # makes the transformation reviewable rather than a black box.
                "source_operation": operation.source_operation,
                "tool": operation.tool.model_dump(mode="json") if operation.tool else None,
                "diagnostics": [diagnostic.to_dict() for diagnostic in operation.diagnostics],
            }
            for operation in result.operations
        ],
    }
