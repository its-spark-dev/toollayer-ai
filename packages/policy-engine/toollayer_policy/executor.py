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
6. A response read as a stream and cut off at the byte limit, then decoded defensively.
7. A result explicitly marked as untrusted content.

Step 6 is a *streaming* bound, not a truncation applied afterwards. The distinction is the
difference between capping what the runtime processes and capping what it receives; only the
second one survives an upstream that answers with an unbounded body.

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
    #: Enforced while the body is being read, so this bounds memory as well as the result.
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

    Two properties are enforced here rather than checked afterwards.

    **Redirects are disabled at the client.** A redirect would send the request to a
    destination the policy never authorized, so it is a failure rather than a hop — and
    disabling it at the client means no code path can forget.

    **The body is bounded while it is being read, not after.** The response is consumed as a
    stream in fixed-size chunks and the connection is closed the moment the accumulated
    length exceeds the limit. Reading ``response.content`` would have loaded the whole body
    into memory first, which turns "the result is capped" into "the memory is not" — an
    upstream returning ten gigabytes would have been fully received before anything refused
    it. ``Content-Length`` is used as an early-exit hint only; a missing or lying header
    changes nothing, because the running total is what decides.
    """

    #: Read granularity. Large enough that a normal response costs few iterations, small
    #: enough that the overshoot past the limit before the check fires stays trivial.
    _CHUNK_BYTES: Final = 64 * 1024

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

        # One byte past the limit is enough for the executor to detect the overrun. Nothing
        # beyond that is ever collected, so the peak memory a hostile upstream can cause is
        # the limit plus one chunk.
        ceiling = limits.max_response_bytes + 1

        try:
            with (
                httpx.Client(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport(),
                ) as client,
                client.stream(
                    request.method,
                    request.url,
                    headers=headers,
                    content=request.body,
                ) as response,
            ):
                if _declared_length_exceeds(response.headers, limits.max_response_bytes):
                    # A truthful oversized Content-Length lets the connection close
                    # before a single body byte is read. This is an optimization, never
                    # the control: a chunked response declares no length at all, and a
                    # lying one is caught by the loop below.
                    raise PolicyDenied(
                        ErrorCode.RESPONSE_TOO_LARGE,
                        f"the upstream response exceeds the {limits.max_response_bytes} byte limit",
                    )

                collected = bytearray()
                for chunk in response.iter_bytes(self._CHUNK_BYTES):
                    collected.extend(chunk)
                    if len(collected) >= ceiling:
                        # Leaving the `with` block closes the response, which tears down
                        # the connection without draining the remainder. An endless
                        # stream therefore ends here rather than being consumed to
                        # completion for nothing.
                        del collected[ceiling:]
                        return response.status_code, dict(response.headers), bytes(collected)
                return response.status_code, dict(response.headers), bytes(collected)
        except httpx.TimeoutException:
            raise PolicyDenied(
                ErrorCode.UPSTREAM_TIMEOUT, "the upstream API did not respond in time"
            ) from None
        except httpx.HTTPError:
            # The upstream's own error text is never surfaced. It is attacker-influenceable
            # and would end up in logs.
            raise PolicyDenied(
                ErrorCode.UPSTREAM_UNAVAILABLE, "the upstream API could not be reached"
            ) from None

    def _transport(self) -> httpx.BaseTransport:
        """Build the underlying transport with retries disabled.

        Explicit rather than inherited: a retry would repeat a call that may have already
        had an effect upstream, and for a governed write that is worse than failing.
        """
        return httpx.HTTPTransport(retries=0)


def _declared_length_exceeds(headers: httpx.Headers, maximum: int) -> bool:
    """Whether a well-formed ``Content-Length`` already puts the body over the limit.

    A malformed, absent, or understated value returns ``False`` and the streaming loop
    decides. The header is a hint from the same party the limit exists to constrain, so it
    can only ever make the runtime refuse *earlier*, never accept more.
    """
    raw = headers.get("content-length")
    if raw is None:
        return False
    try:
        return int(raw) > maximum
    except ValueError:
        return False


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
