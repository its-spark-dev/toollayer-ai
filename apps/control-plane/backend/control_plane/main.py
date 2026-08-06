"""The Tool Control Plane application.

Failures are converted to the shared error envelope in exactly one place. Handlers raise
domain exceptions and never build an error body, so every failure from every route has the
same shape, the same code vocabulary, and the same guarantee that it does not echo the input
that caused it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from control_plane.config import get_settings
from control_plane.db import create_schema, get_engine
from control_plane.routes_admin import router as admin_router
from control_plane.routes_internal import router as internal_router
from toollayer_contracts import CONTRACT_VERSION
from toollayer_contracts.errors import ErrorCode, ErrorDetail, ErrorEnvelope, ToolLayerError
from toollayer_openapi import ConversionError

__all__ = ["app", "create_app"]

logger = logging.getLogger("toollayer.control_plane")


def create_app(*, create_tables: bool = True) -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="ToolLayer AI — Tool Control Plane",
        version="0.2.0",
        summary="Turns OpenAPI descriptions into governed, versioned, provider-neutral tools.",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=["content-type", "x-toollayer-admin-token"],
        )

    @app.middleware("http")
    async def _request_id(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        """Attach a request id to every response so a client can quote it in a bug report."""
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(ToolLayerError)
    async def _domain_error(request: Request, exc: ToolLayerError) -> JSONResponse:
        issues: tuple[str, ...] = getattr(exc, "issues", ())
        # Publication readiness reports several blocking issues at once. They are surfaced
        # so the console can list every one instead of discovering them one failed publish
        # at a time.
        details = tuple(ErrorDetail(code="publication.blocked", message=issue) for issue in issues)
        envelope = ErrorEnvelope(
            code=exc.code,
            message=exc.message,
            pointer=exc.pointer,
            details=exc.details or details,
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=exc.http_status, content=envelope.to_dict())

    @app.exception_handler(ConversionError)
    async def _conversion_error(request: Request, exc: ConversionError) -> JSONResponse:
        """Translate the converter's own errors into the shared envelope.

        The converter package is a library with its own error taxonomy and no knowledge of
        HTTP. Translating here keeps that separation while still giving every failure from
        every route the same shape and the same pointer-into-the-document convention.
        """
        envelope = ErrorEnvelope(
            code=exc.code,
            message=exc.message,
            pointer=exc.pointer,
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=envelope.http_status, content=envelope.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Framework validation errors carry the rejected input. It is stripped here rather
        # than passed through, because these responses are logged by clients.
        envelope = ErrorEnvelope(
            code=ErrorCode.INVALID_REQUEST,
            message="the request body does not match the expected shape",
            pointer=_first_pointer(exc),
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=422, content=envelope.to_dict())

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.exception("unhandled control plane error", extra={"request_id": request_id})
        envelope = ErrorEnvelope(
            code=ErrorCode.INTERNAL_ERROR,
            message="the request could not be completed",
            request_id=request_id,
        )
        return JSONResponse(status_code=500, content=envelope.to_dict())

    @app.get("/healthz", tags=["operations"])
    async def healthz() -> dict[str, object]:
        """Liveness: the process is up. Deliberately does not touch the database."""
        return {
            "status": "ok",
            "contract_version": CONTRACT_VERSION,
            # Surfaced so an unsigned Control Plane says so about itself. A deployment that
            # publishes unauthenticated artifacts should be visible from the outside, not
            # something a consumer discovers by inspecting a snapshot.
            "snapshot_signing": "enabled" if settings.signs_snapshots else "disabled",
            "snapshot_signing_key_id": settings.snapshot_signing_key_id or None,
        }

    @app.get("/readyz", tags=["operations"])
    async def readyz() -> JSONResponse:
        """Readiness: the process can serve requests, which means the database answers."""
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:
            logger.warning("readiness check failed: the database is unreachable")
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(status_code=200, content={"status": "ready"})

    app.include_router(admin_router)
    app.include_router(internal_router)

    if create_tables:
        create_schema()

    if settings.uses_development_tokens:
        logger.warning(
            "the control plane is running with shipped development tokens; set "
            "TOOLLAYER_ADMIN_TOKEN and TOOLLAYER_SERVICE_TOKEN before exposing it"
        )

    return app


def _first_pointer(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return ""
    location = [part for part in errors[0].get("loc", ()) if part != "body"]
    return "".join(f"/{str(part).replace('~', '~0').replace('/', '~1')}" for part in location)


app = create_app()
