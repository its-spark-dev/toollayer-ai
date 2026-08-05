"""Analyzing a whole API description into a reviewable set of candidate tools.

Analysis is the step between "a file was uploaded" and "a human reviews a proposal". Its job
is to produce, for every operation in the document, either a converted tool or a diagnostic
explaining why not — and never to fail the whole document because one operation is awkward.

The result is deliberately a *proposal*. Nothing here publishes, persists, or executes
anything, and nothing here reaches the network. A reviewer decides what becomes a tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

from toollayer_contracts.models import ToolDefinition
from toollayer_openapi.converter import SUPPORTED_METHODS, convert_operation, operation_key
from toollayer_openapi.errors import ConversionError, Diagnostic, InvalidDocumentError
from toollayer_openapi.loader import LoadedDocument, SourceLimits
from toollayer_openapi.references import ReferenceResolver

__all__ = ["ANALYZER_VERSION", "AnalysisResult", "AnalyzedOperation", "analyze_document"]

#: Bumped whenever conversion output changes for an unchanged input. Recorded on every
#: analysis so a stored draft always says which converter produced it — otherwise a draft
#: reviewed under old rules could be published under new ones without anyone noticing.
ANALYZER_VERSION: Final = "toollayer-openapi-analyzer/1"

_HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


@dataclass(frozen=True, slots=True)
class AnalyzedOperation:
    """One operation from the source document and what analysis made of it."""

    key: str
    path: str
    method: str
    pointer: str
    source_operation: dict[str, Any]
    tool: ToolDefinition | None
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def convertible(self) -> bool:
        return self.tool is not None


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Everything a reviewer needs to decide what to publish."""

    analyzer_version: str
    spec_version: str
    api_title: str
    api_summary: str
    base_url: str | None
    operations: tuple[AnalyzedOperation, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = field(default=())

    @property
    def convertible_operations(self) -> tuple[AnalyzedOperation, ...]:
        return tuple(operation for operation in self.operations if operation.convertible)

    @property
    def tools(self) -> tuple[ToolDefinition, ...]:
        return tuple(
            operation.tool for operation in self.operations if operation.tool is not None
        )

    @property
    def all_diagnostics(self) -> tuple[Diagnostic, ...]:
        nested = tuple(
            diagnostic for operation in self.operations for diagnostic in operation.diagnostics
        )
        return self.diagnostics + nested


def analyze_document(
    loaded: LoadedDocument,
    *,
    limits: SourceLimits | None = None,
    base_url_override: str | None = None,
) -> AnalysisResult:
    """Analyze a loaded document into candidate tools plus diagnostics."""
    limits = limits or SourceLimits()
    document = loaded.document
    resolver = ReferenceResolver(document)

    info = document.get("info")
    title = "Untitled API"
    summary = "An API registered with the Tool Control Plane."
    if isinstance(info, dict):
        if isinstance(info.get("title"), str) and info["title"].strip():
            title = info["title"].strip()[:128]
        for candidate_field in ("summary", "description"):
            value = info.get(candidate_field)
            if isinstance(value, str) and value.strip():
                summary = value.strip()[:1024]
                break

    base_url, base_url_diagnostics = _resolve_base_url(document, base_url_override)

    paths = document["paths"]
    operations: list[AnalyzedOperation] = []
    document_diagnostics: list[Diagnostic] = list(base_url_diagnostics)
    seen_tool_names: dict[str, str] = {}
    analyzed_count = 0

    for path in sorted(paths):
        raw_path_item = paths[path]
        path_pointer = f"/paths/{path.replace('~', '~0').replace('/', '~1')}"
        if not isinstance(path, str) or not path.startswith("/"):
            document_diagnostics.append(
                Diagnostic(
                    code="invalid_path",
                    message="a path key must be a string beginning with '/'",
                    pointer=path_pointer,
                )
            )
            continue
        if not isinstance(raw_path_item, dict):
            document_diagnostics.append(
                Diagnostic(
                    code="invalid_path_item",
                    message="a path item must be an object",
                    pointer=path_pointer,
                )
            )
            continue

        try:
            path_item = resolver.resolve(raw_path_item, pointer=path_pointer)
        except ConversionError as error:
            document_diagnostics.append(error.as_diagnostic())
            continue

        for method in SUPPORTED_METHODS:
            operation = path_item.get(method)
            if operation is None:
                continue
            if analyzed_count >= limits.max_operations:
                document_diagnostics.append(
                    Diagnostic(
                        code="operation_limit_exceeded",
                        message=f"only the first {limits.max_operations} operations are analyzed",
                        pointer=path_pointer,
                    )
                )
                break
            analyzed_count += 1
            operations.append(
                _analyze_operation(
                    path=path,
                    method=method,
                    path_item=path_item,
                    operation=operation,
                    pointer=f"{path_pointer}/{method}",
                    seen_tool_names=seen_tool_names,
                )
            )

        unsupported = sorted((_HTTP_METHODS - set(SUPPORTED_METHODS)) & set(path_item))
        for method in unsupported:
            document_diagnostics.append(
                Diagnostic(
                    code="unsupported_method",
                    message=f"the {method.upper()} method is not converted into a tool",
                    pointer=f"{path_pointer}/{method}",
                    severity="warning",
                    operation_key=operation_key(path, method),
                )
            )

    if not operations:
        document_diagnostics.append(
            Diagnostic(
                code="no_convertible_operations",
                message="the document contains no operation this converter can turn into a tool",
                pointer="/paths",
            )
        )

    return AnalysisResult(
        analyzer_version=ANALYZER_VERSION,
        spec_version=loaded.spec_version,
        api_title=title,
        api_summary=summary,
        base_url=base_url,
        operations=tuple(operations),
        diagnostics=tuple(document_diagnostics),
    )


def _analyze_operation(
    *,
    path: str,
    method: str,
    path_item: dict[str, Any],
    operation: object,
    pointer: str,
    seen_tool_names: dict[str, str],
) -> AnalyzedOperation:
    key = operation_key(path, method)

    if not isinstance(operation, dict):
        return AnalyzedOperation(
            key=key,
            path=path,
            method=method,
            pointer=pointer,
            source_operation={},
            tool=None,
            diagnostics=(
                Diagnostic(
                    code="invalid_operation",
                    message="an operation must be an object",
                    pointer=pointer,
                    operation_key=key,
                ),
            ),
        )

    try:
        tool = convert_operation(
            path=path, method=method, path_item=path_item, operation=operation
        )
    except ConversionError as error:
        return AnalyzedOperation(
            key=key,
            path=path,
            method=method,
            pointer=pointer,
            source_operation=operation,
            tool=None,
            diagnostics=(
                Diagnostic(
                    code=error.code,
                    message=error.message,
                    pointer=error.pointer or pointer,
                    operation_key=key,
                ),
            ),
        )

    # A collision is reported against the *second* operation to claim the name, and both
    # remain visible in the console. Auto-renaming one of them would produce a tool name
    # that appears nowhere in the source document.
    previous = seen_tool_names.get(tool.tool_name)
    if previous is not None:
        return AnalyzedOperation(
            key=key,
            path=path,
            method=method,
            pointer=pointer,
            source_operation=operation,
            tool=None,
            diagnostics=(
                Diagnostic(
                    code="tool_name_collision",
                    message=(
                        f"this operation normalizes to the same tool name as {previous!r}; "
                        "give one of them a distinct operationId"
                    ),
                    pointer=pointer,
                    operation_key=key,
                ),
            ),
        )
    seen_tool_names[tool.tool_name] = key

    diagnostics: list[Diagnostic] = []
    if tool.provenance.description_origin == "generated":
        diagnostics.append(
            Diagnostic(
                code="description_generated",
                message=(
                    "the source operation has no summary or description, so a placeholder was "
                    "generated; write a real description before publishing"
                ),
                pointer=pointer,
                severity="warning",
                operation_key=key,
            )
        )
    if tool.provenance.deprecated:
        diagnostics.append(
            Diagnostic(
                code="operation_deprecated",
                message="the source operation is marked deprecated",
                pointer=pointer,
                severity="warning",
                operation_key=key,
            )
        )

    return AnalyzedOperation(
        key=key,
        path=path,
        method=method,
        pointer=pointer,
        source_operation=operation,
        tool=tool,
        diagnostics=tuple(diagnostics),
    )


def _resolve_base_url(
    document: dict[str, Any], override: str | None
) -> tuple[str | None, tuple[Diagnostic, ...]]:
    """Choose the connector's base URL from the document, or accept an explicit override.

    An override wins because the document's ``servers`` entry describes where the API's
    author publishes it, which is frequently not where this deployment should call it. When
    neither is usable the connector cannot be published until a reviewer supplies one, and
    that is reported rather than guessed.
    """
    if override:
        if _usable_base_url(override):
            return override.rstrip("/") or override, ()
        return None, (
            Diagnostic(
                code="invalid_base_url",
                message="the supplied base URL is not a plain http or https origin",
                pointer="/servers",
            ),
        )

    servers = document.get("servers")
    if not isinstance(servers, list) or not servers:
        return None, (
            Diagnostic(
                code="base_url_missing",
                message="the document declares no server; supply a base URL before publishing",
                pointer="/servers",
                severity="warning",
            ),
        )

    first = servers[0]
    if not isinstance(first, dict):
        raise InvalidDocumentError("'servers' entries must be objects", pointer="/servers/0")
    url = first.get("url")
    if "variables" in first:
        return None, (
            Diagnostic(
                code="server_variables_unsupported",
                message=(
                    "the declared server uses template variables; supply an explicit base URL"
                ),
                pointer="/servers/0",
                severity="warning",
            ),
        )
    if not isinstance(url, str) or not _usable_base_url(url):
        return None, (
            Diagnostic(
                code="base_url_missing",
                message=(
                    "the declared server is not an absolute http or https URL; supply a base "
                    "URL before publishing"
                ),
                pointer="/servers/0/url",
                severity="warning",
            ),
        )
    return url.rstrip("/") or url, ()


def _usable_base_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and "@" not in parsed.netloc
        and not parsed.query
        and not parsed.fragment
    )
