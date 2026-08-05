"""The governed HTTP execution boundary.

This is the last place a tool call can be stopped, so it is the place that assumes the least.
It does not trust the tool definition (the destination is re-checked), it does not trust the
arguments (they were validated, and the request was built from bindings rather than from
them), and it does not trust the upstream response (it is bounded, decoded defensively, and
labelled untrusted).

The execution sequence is fixed and every step can refuse:

1. Method allowlist.
2. Argument validation against the published input schema.
3. Request construction from the published bindings.
4. Destination authorization, including post-resolution address checks.
5. A bounded request with no redirects and no retries.
6. A bounded, defensively decoded response.
7. A result explicitly marked as untrusted content.

Authorization by role is *not* here. It runs one layer up, in the runtime, because it needs
the caller identity that this layer deliberately knows nothing about.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx

from toollayer_contracts.errors import ErrorCode, PolicyDenied
from toollayer_contracts.models import ToolDefinition, ToolExecutionResult
from toollayer_policy.arguments import PreparedRequest, prepare_request, validate_arguments
from toollayer_policy.destinations import DestinationPolicy, DnsResolver

__all__ = ["ExecutionLimits", "HttpTransport", "ToolExecutor"]

_MAX_RESPONSE_PREVIEW: Final = 4096


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Bounds applied to every outbound call.

    Every one of these has a finite default. An unbounded read is a denial-of-service
    primitive: a slow or enormous upstream response would otherwise hold a runtime worker
    open indefinitely.
    """

    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 10.0
    max_response_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if not 0 < self.connect_timeout_seconds <= 30:
            raise ValueError("connect_timeout_seconds must be within (0, 30]")
        if not 0 < self.read_timeout_seconds <= 60:
            raise ValueError("read_timeout_seconds must be within (0, 60]")
        if not 0 < self.max_response_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_response_bytes must be within (0, 16 MiB]")


class HttpTransport(Protocol):
    """The narrow outbound interface. Injected so tests never touch a real socket."""

    def send(
        self,
        request: PreparedRequest,
        *,
        limits: ExecutionLimits,
    ) -> tuple[int, dict[str, str], bytes]: ...


class HttpxTransport:
    """The default transport.

    Redirects are disabled at the client rather than handled after the fact. A redirect
    would send the request to a destination the policy never authorized, so it is a failure
    here rather than a hop — and disabling it at the client means no code path can forget.
    """

    def send(
        self,
        request: PreparedRequest,
        *,
        limits: ExecutionLimits,
    ) -> tuple[int, dict[str, str], bytes]:
        timeout = httpx.Timeout(
            connect=limits.connect_timeout_seconds,
            read=limits.read_timeout_seconds,
            write=limits.read_timeout_seconds,
            pool=limits.connect_timeout_seconds,
        )
        headers = dict(request.headers)
        headers.setdefault("user-agent", "toollayer-runtime/0.1")

        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = client.request(
                    request.method,
                    request.url,
                    headers=headers,
                    content=request.body,
                )
                body = response.content[: limits.max_response_bytes + 1]
                return response.status_code, dict(response.headers), body
        except httpx.TimeoutException:
            raise PolicyDenied(
                ErrorCode.UPSTREAM_TIMEOUT, "the upstream API did not respond in time"
            ) from None
        except httpx.HTTPError:
            raise PolicyDenied(
                ErrorCode.UPSTREAM_UNAVAILABLE, "the upstream API could not be reached"
            ) from None


class ToolExecutor:
    """Execute one governed tool call."""

    def __init__(
        self,
        *,
        policy: DestinationPolicy,
        limits: ExecutionLimits | None = None,
        transport: HttpTransport | None = None,
        resolver: DnsResolver | None = None,
    ) -> None:
        self._policy = policy
        self._limits = limits or ExecutionLimits()
        self._transport = transport or HttpxTransport()
        self._resolver = resolver

    @property
    def limits(self) -> ExecutionLimits:
        return self._limits

    def prepare(
        self,
        tool: ToolDefinition,
        arguments: object,
        *,
        base_url: str,
    ) -> PreparedRequest:
        """Run every pre-flight check and return the request that would be sent.

        Exposed separately so the console and the tests can show exactly what a tool call
        becomes without performing it — and so a caller that only wants to validate does not
        have to reach for a mock transport.
        """
        self._policy.check_method(tool.operation.method)
        validated = validate_arguments(tool, arguments)
        request = prepare_request(tool, validated, base_url=base_url)
        self._policy.check(request.url, resolver=self._resolver)
        return request

    def execute(
        self,
        tool: ToolDefinition,
        arguments: object,
        *,
        base_url: str,
        connector_key: str,
        connector_version: str,
        request_id: str | None = None,
    ) -> ToolExecutionResult:
        """Execute a tool call that has passed every check, and bound the response."""
        request = self.prepare(tool, arguments, base_url=base_url)

        started = time.monotonic()
        status_code, headers, body = self._transport.send(request, limits=self._limits)
        duration_ms = int((time.monotonic() - started) * 1000)

        if 300 <= status_code < 400:
            raise PolicyDenied(
                ErrorCode.REDIRECT_NOT_ALLOWED,
                "the upstream API returned a redirect, which is not followed",
            )
        if len(body) > self._limits.max_response_bytes:
            raise PolicyDenied(
                ErrorCode.RESPONSE_TOO_LARGE,
                f"the upstream response exceeds the {self._limits.max_response_bytes} byte limit",
            )

        return ToolExecutionResult(
            tool_name=tool.tool_name,
            connector_key=connector_key,
            connector_version=connector_version,
            status="succeeded" if status_code < 400 else "upstream_error",
            http_status=status_code,
            duration_ms=duration_ms,
            content=_decode(body, headers),
            request_id=request_id,
        )


def _decode(body: bytes, headers: dict[str, str]) -> Any:
    """Decode an upstream body without letting it dictate how it is treated.

    JSON is parsed only when the upstream declared JSON *and* the bytes actually parse.
    Anything else becomes a bounded text preview. An upstream that returns HTML with a JSON
    content type does not get to decide that the runtime will treat it as structured data.
    """
    if not body:
        return None

    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type.endswith("json") or content_type.endswith("+json"):
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            pass

    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return {"encoding": "binary", "byte_length": len(body)}
    return {"content_type": content_type or "unknown", "text": text[:_MAX_RESPONSE_PREVIEW]}
