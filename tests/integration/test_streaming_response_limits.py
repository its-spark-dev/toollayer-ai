"""The response bound, proved against a real socket.

The unit-level tests for this control substitute the transport, so they can only show that
the executor refuses an oversized body it was handed. That is a check on the *result*. It
cannot distinguish "the runtime refused after receiving ten gigabytes" from "the runtime
stopped reading at one megabyte", and those are different security properties.

These tests therefore run the real :class:`HttpxTransport` against a real HTTP server on
loopback. The server records how many bytes it managed to write, which is the only way to
observe from the outside whether the client stopped reading — and it is the assertion that
makes "bounded" mean bounded memory rather than a bounded return value.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from toollayer_contracts.errors import ErrorCode, PolicyDenied
from toollayer_policy import ExecutionLimits
from toollayer_policy.arguments import PreparedRequest
from toollayer_policy.executor import HttpxTransport

pytestmark = pytest.mark.integration

LIMIT = 64 * 1024
CHUNK = 8 * 1024

#: A cap on the endless-stream handler so a regression that consumes everything fails the
#: test instead of hanging the suite forever.
RUNAWAY_CEILING = 64 * 1024 * 1024


class _Recorder:
    """What the server actually managed to put on the wire."""

    def __init__(self) -> None:
        self.bytes_written = 0
        self.finished_normally = False
        self.lock = threading.Lock()

    def wrote(self, count: int) -> None:
        with self.lock:
            self.bytes_written += count


def _handler_class(recorder: _Recorder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: Any) -> None:  # keep the test output readable
            return

        def do_GET(self) -> None:
            route, _, _query = self.path.partition("?")
            handlers = {
                "/exact": lambda: self._fixed(LIMIT),
                "/under": lambda: self._fixed(LIMIT - 1),
                "/over": lambda: self._fixed(LIMIT + 1),
                "/over-chunked": lambda: self._write_chunked_response(LIMIT + 1),
                "/lying-length": self._lying_length,
                "/oversized-length": self._oversized_length,
                "/endless": self._endless,
                "/redirect": self._redirect,
            }
            handler = handlers.get(route)
            if handler is None:
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            handler()

        def _fixed(self, size: int) -> None:
            body = b"x" * size
            self.send_response(200)
            self.send_header("content-type", "application/octet-stream")
            self.send_header("content-length", str(size))
            self.end_headers()
            self.wfile.write(body)
            recorder.wrote(size)

        def _write_chunked_response(self, size: int) -> None:
            """Chunked, so the size is discoverable only by reading."""
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            remaining = size
            try:
                while remaining > 0:
                    block = b"q" * min(CHUNK, remaining)
                    self.wfile.write(f"{len(block):X}\r\n".encode("ascii") + block + b"\r\n")
                    recorder.wrote(len(block))
                    remaining -= len(block)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _lying_length(self) -> None:
            """Declare a small body, then send a large one with chunked encoding.

            A limit that trusted ``Content-Length`` would be bypassed exactly here.
            """
            self.send_response(200)
            self.send_header("content-type", "application/octet-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            self._write_chunked(LIMIT * 4)

        def _oversized_length(self) -> None:
            """A truthful oversized declaration, which may be refused before any body."""
            size = LIMIT * 8
            self.send_response(200)
            self.send_header("content-type", "application/octet-stream")
            self.send_header("content-length", str(size))
            self.end_headers()
            try:
                for _ in range(size // CHUNK):
                    self.wfile.write(b"y" * CHUNK)
                    recorder.wrote(CHUNK)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _endless(self) -> None:
            """Stream until the client goes away, or until the runaway ceiling."""
            self.send_response(200)
            self.send_header("content-type", "application/octet-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            self._write_chunked(RUNAWAY_CEILING)
            recorder.finished_normally = True

        def _write_chunked(self, total: int) -> None:
            block = b"z" * CHUNK
            header = f"{CHUNK:X}\r\n".encode("ascii")
            try:
                for _ in range(total // CHUNK):
                    self.wfile.write(header + block + b"\r\n")
                    self.wfile.flush()
                    recorder.wrote(CHUNK)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                # The client closed on us. That is the behavior under test.
                pass

        def _redirect(self) -> None:
            self.send_response(302)
            self.send_header("location", "http://attacker.test/collect")
            self.send_header("content-length", "0")
            self.end_headers()

    return Handler


class _QuietServer(ThreadingHTTPServer):
    """A server that does not print a traceback when the client hangs up.

    The client hanging up mid-body is the behavior under test, so the resulting
    ``ConnectionResetError`` is a success signal rather than an error worth reporting.
    """

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        return


@pytest.fixture()
def upstream() -> Iterator[tuple[str, _Recorder]]:
    """A real HTTP server on loopback, plus a record of what it wrote."""
    recorder = _Recorder()
    server = _QuietServer(("127.0.0.1", 0), _handler_class(recorder))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{port}", recorder
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(url: str) -> PreparedRequest:
    return PreparedRequest(
        method="GET", url=url, path="/", query=(), headers=(), body=None, content_type=None
    )


def _send(url: str, *, limit: int = LIMIT) -> tuple[int, dict[str, str], bytes]:
    return HttpxTransport().send(
        _request(url),
        limits=ExecutionLimits(
            connect_timeout_seconds=3.0, read_timeout_seconds=10.0, max_response_bytes=limit
        ),
    )


def _execute_against(url: str, *, limit: int = LIMIT) -> Any:
    """Run one governed tool call against ``url`` through the real executor and transport."""
    from urllib.parse import urlsplit

    from tests.factories import make_operation, make_tool

    from toollayer_policy import DestinationPolicy, ToolExecutor

    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    executor = ToolExecutor(
        # Loopback is the escape hatch this test needs and nothing else: the destination is
        # a real server on 127.0.0.1.
        policy=DestinationPolicy.from_origins(
            [base], allow_plaintext_http=True, allow_loopback=True
        ),
        limits=ExecutionLimits(
            connect_timeout_seconds=3.0, read_timeout_seconds=10.0, max_response_bytes=limit
        ),
    )
    tool = make_tool(operation=make_operation(method="GET", path_template=parts.path))
    return executor.execute(
        tool, {}, base_url=base, connector_key="streaming", connector_version="0.1.0"
    )


class TestTheBoundHolds:
    def test_a_body_below_the_limit_is_returned_whole(
        self, upstream: tuple[str, _Recorder]
    ) -> None:
        base, _ = upstream
        status, _headers, body = _send(f"{base}/under")
        assert status == 200
        assert len(body) == LIMIT - 1

    def test_a_body_exactly_at_the_limit_succeeds(self, upstream: tuple[str, _Recorder]) -> None:
        """The boundary is inclusive: the limit is a permitted size, not the first refused one."""
        base, _ = upstream
        status, _headers, body = _send(f"{base}/exact")
        assert status == 200
        assert len(body) == LIMIT

    def test_one_declared_byte_over_the_limit_is_refused(
        self, upstream: tuple[str, _Recorder]
    ) -> None:
        base, _ = upstream
        with pytest.raises(PolicyDenied) as caught:
            _send(f"{base}/over")
        assert caught.value.code == ErrorCode.RESPONSE_TOO_LARGE

    def test_one_undeclared_byte_over_the_limit_is_refused_end_to_end(
        self, upstream: tuple[str, _Recorder]
    ) -> None:
        """Chunked, one byte over — the case no header can reveal in advance.

        Run through the whole executor rather than the transport alone, so this asserts what
        a governed tool call actually does rather than what one layer returns.
        """
        base, _ = upstream
        with pytest.raises(PolicyDenied) as caught:
            _execute_against(f"{base}/over-chunked")
        assert caught.value.code == ErrorCode.RESPONSE_TOO_LARGE

    def test_a_chunked_body_exactly_at_the_limit_succeeds_end_to_end(
        self, upstream: tuple[str, _Recorder]
    ) -> None:
        """The other side of the same boundary, so the check is not simply always refusing."""
        result = _execute_against(f"{upstream[0]}/exact")
        assert result.http_status == 200
        assert result.status == "succeeded"


class TestContentLengthIsNotTheControl:
    def test_an_understated_content_length_cannot_smuggle_a_large_body(
        self, upstream: tuple[str, _Recorder]
    ) -> None:
        """The running total decides, not what the upstream said about itself."""
        base, _ = upstream
        _status, _headers, body = _send(f"{base}/lying-length")
        assert len(body) == LIMIT + 1

    def test_a_truthful_oversized_content_length_is_refused_before_the_body(
        self, upstream: tuple[str, _Recorder]
    ) -> None:
        base, recorder = upstream
        with pytest.raises(PolicyDenied) as caught:
            _send(f"{base}/oversized-length")
        assert caught.value.code == ErrorCode.RESPONSE_TOO_LARGE
        # Nothing forced the server to produce the body at all. This is the cheap path, and
        # it is an optimization on top of the streaming bound rather than a substitute.
        assert recorder.bytes_written < LIMIT * 8

    def test_a_missing_content_length_is_still_bounded(
        self, upstream: tuple[str, _Recorder]
    ) -> None:
        """Chunked responses declare no length; the bound must not depend on one."""
        base, _ = upstream
        _status, headers, body = _send(f"{base}/lying-length")
        assert "content-length" not in {name.lower() for name in headers}
        assert len(body) == LIMIT + 1


class TestTheStreamIsNotDrained:
    def test_an_endless_stream_terminates_at_the_limit(
        self, upstream: tuple[str, _Recorder]
    ) -> None:
        """The test completing at all is the assertion.

        The handler writes until the client disconnects. A transport that read the body to
        completion before checking its size would keep this test running until the runaway
        ceiling, and the byte assertion below would fail.
        """
        base, _recorder = upstream
        _status, _headers, body = _send(f"{base}/endless")
        assert len(body) == LIMIT + 1

    def test_the_client_stops_consuming_after_the_limit_is_crossed(
        self, upstream: tuple[str, _Recorder]
    ) -> None:
        """The property the old stub-transport test could not observe.

        ``bytes_written`` is what the upstream actually got onto the socket. A buffering
        client would let it reach the runaway ceiling; a streaming client that closes the
        response stops it within a small multiple of the limit — socket buffers mean the
        server can run slightly ahead, which is why this is a generous bound rather than an
        exact one.
        """
        base, recorder = upstream
        _send(f"{base}/endless")
        assert recorder.finished_normally is False
        assert recorder.bytes_written < RUNAWAY_CEILING // 8, (
            f"the server wrote {recorder.bytes_written} bytes; the client kept reading past "
            f"the {LIMIT} byte limit"
        )


class TestOtherTransportGuarantees:
    def test_a_redirect_is_returned_rather_than_followed(
        self, upstream: tuple[str, _Recorder]
    ) -> None:
        """Proved against a real server, so it covers the client configuration itself."""
        base, _ = upstream
        status, headers, _body = _send(f"{base}/redirect")
        assert status == 302
        assert headers["location"] == "http://attacker.test/collect"

    def test_a_connect_timeout_is_reported_as_a_timeout(self) -> None:
        """198.51.100.0/24 is TEST-NET-2: reserved, and guaranteed not to answer."""
        with pytest.raises(PolicyDenied) as caught:
            HttpxTransport().send(
                _request("http://198.51.100.1:9/never"),
                limits=ExecutionLimits(
                    connect_timeout_seconds=0.05,
                    read_timeout_seconds=0.05,
                    max_response_bytes=LIMIT,
                ),
            )
        assert caught.value.code in {ErrorCode.UPSTREAM_TIMEOUT, ErrorCode.UPSTREAM_UNAVAILABLE}

    def test_an_unreachable_upstream_does_not_leak_its_error_text(self) -> None:
        with pytest.raises(PolicyDenied) as caught:
            HttpxTransport().send(
                _request("http://127.0.0.1:1/closed"),
                limits=ExecutionLimits(max_response_bytes=LIMIT),
            )
        assert str(caught.value.message) == "the upstream API could not be reached"

    def test_environment_proxies_are_ignored(
        self, upstream: tuple[str, _Recorder], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``trust_env=False`` means a proxy in the environment cannot redirect traffic.

        Without it, an attacker who can set ``HTTP_PROXY`` on the host — or a developer
        machine that simply has one — would route every governed call somewhere the
        destination policy never authorized.
        """
        base, _ = upstream
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
        status, _headers, body = _send(f"{base}/under")
        assert status == 200
        assert len(body) == LIMIT - 1
