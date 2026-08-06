"""The LLM Orchestration Runtime application.

The runtime is provided as a reference implementation rather than a full chatbot product. Its
job is to prove that what the Control Plane publishes is actually usable, and that the
governance survives contact with model output. It has no conversation memory, no streaming,
and no user interface beyond a small playground.

Caller identity arrives one of two ways, and which one is in force is never implicit.
``asserted_header`` reads ``x-toollayer-caller`` and ``x-toollayer-roles`` and believes them:
a demonstration mode, not authentication. ``verified_token`` requires a signed caller token
and derives the subject and roles from verified claims. ``/healthz`` reports the mode, so a
runtime that is merely trusting its caller says so about itself.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from runtime_service.config import RuntimeSettings, get_settings
from runtime_service.orchestrator import OrchestrationOutcome, Orchestrator
from runtime_service.snapshot import SnapshotClient, SnapshotStore
from toollayer_contracts import CONTRACT_VERSION
from toollayer_contracts.errors import ErrorCode, ErrorEnvelope, ToolLayerError
from toollayer_mock_llm import MockLLMProvider
from toollayer_policy import CallerIdentity, ToolExecutor

__all__ = ["app", "build_orchestrator", "create_app"]

logger = logging.getLogger("toollayer.runtime")

CALLER_HEADER = "x-toollayer-caller"
ROLES_HEADER = "x-toollayer-roles"


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    utterance: str = Field(min_length=1, max_length=4000)
    confirmed: bool = False
    dry_run: bool = False


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False


def build_orchestrator(settings: RuntimeSettings) -> Orchestrator:
    """Assemble the runtime's collaborators from settings."""
    client = SnapshotClient(
        base_url=settings.control_plane_url,
        service_token=settings.service_token,
        timeout_seconds=settings.read_timeout_seconds,
        verification=settings.snapshot_verification_policy(),
    )
    store = SnapshotStore(
        client,
        deployment_key=settings.deployment_key,
        refresh_seconds=settings.snapshot_refresh_seconds,
    )
    executor = ToolExecutor(
        policy=settings.destination_policy(),
        limits=settings.execution_limits(),
    )
    return Orchestrator(store=store, provider=MockLLMProvider(), executor=executor)


def caller_identity(
    subject: Annotated[str | None, Header(alias=CALLER_HEADER)] = None,
    roles: Annotated[str | None, Header(alias=ROLES_HEADER)] = None,
    authorization: Annotated[str | None, Header(alias="authorization")] = None,
) -> CallerIdentity | None:
    """Establish the caller's identity under whichever mode is configured.

    In ``asserted_header`` mode the host application is trusted to have authenticated the
    user before calling, and the runtime enforces what it asserts. That is a trust boundary,
    not an authentication step, and it is stated in ``docs/threat-model.md`` and reported by
    ``/healthz`` rather than hidden behind a header that looks authoritative.

    In ``verified_token`` mode the bearer token is verified — signature, issuer, audience,
    expiry — and the assertion headers are refused outright.
    """
    authenticator = get_settings().caller_authenticator()
    return authenticator.identify(
        bearer_token=_bearer(authorization),
        asserted_subject=subject,
        asserted_roles=roles,
    )


def _bearer(authorization: str | None) -> str | None:
    """Extract a bearer token, without ever putting the header value in a message."""
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    return token.strip() or None


CallerDep = Annotated[CallerIdentity | None, Depends(caller_identity)]


def create_app(orchestrator: Orchestrator | None = None) -> FastAPI:
    settings = get_settings()
    engine = orchestrator or build_orchestrator(settings)

    app = FastAPI(
        title="ToolLayer AI — LLM Orchestration Runtime",
        version="0.2.0",
        summary=(
            "Consumes a governed deployment snapshot and executes validated, authorized tool "
            "calls. A reference implementation, not a chatbot product."
        ),
    )
    app.state.orchestrator = engine
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["content-type", "authorization", CALLER_HEADER, ROLES_HEADER],
        )

    @app.middleware("http")
    async def _request_id(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(ToolLayerError)
    async def _domain_error(request: Request, exc: ToolLayerError) -> JSONResponse:
        envelope = exc.to_envelope(getattr(request.state, "request_id", None))
        return JSONResponse(status_code=exc.http_status, content=envelope.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, _: RequestValidationError) -> JSONResponse:
        envelope = ErrorEnvelope(
            code=ErrorCode.INVALID_REQUEST,
            message="the request body does not match the expected shape",
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=422, content=envelope.to_dict())

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.exception("unhandled runtime error", extra={"request_id": request_id})
        envelope = ErrorEnvelope(
            code=ErrorCode.INTERNAL_ERROR,
            message="the request could not be completed",
            request_id=request_id,
        )
        return JSONResponse(status_code=500, content=envelope.to_dict())

    @app.get("/healthz", tags=["operations"])
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "contract_version": CONTRACT_VERSION,
            "provider": "mock",
            # Every relaxation this runtime operates under, so a permissive deployment
            # identifies itself instead of looking the same as a locked-down one. That
            # includes whether caller identity is verified or merely asserted, and whether
            # deployment snapshots are required to be signed.
            **settings.security_posture,
        }

    @app.get("/readyz", tags=["operations"])
    async def readyz() -> JSONResponse:
        """Ready means a verified snapshot is loaded. Without one, no tool can be served."""
        store: SnapshotStore = engine._store
        if not store.loaded:
            try:
                store.refresh()
            except ToolLayerError:
                return JSONResponse(
                    status_code=503,
                    content={"status": "unavailable", "reason": "no deployment snapshot"},
                )
        snapshot = store.current
        return JSONResponse(
            status_code=200,
            content={
                "status": "ready",
                "snapshot_revision": snapshot.revision,
                "snapshot_id": snapshot.snapshot_id,
                "tool_count": len(snapshot.tools),
                # Which key authenticated the artifact being served, or null when it was
                # accepted unsigned. Readiness that does not say this would let a runtime
                # look identical whether or not it verified anything.
                "snapshot_signed": snapshot.signed,
                "snapshot_signing_key_id": snapshot.signing_key_id,
            },
        )

    @app.get("/v1/tools", tags=["runtime"])
    async def list_tools(caller: CallerDep) -> dict[str, Any]:
        """List the tools this caller may use."""
        visible = engine.discover(caller)
        return {
            "caller": caller.subject if caller else None,
            "roles": sorted(caller.roles) if caller else [],
            "tools": [
                {
                    "tool_name": bound.tool_name,
                    "display_name": bound.tool.display_name,
                    "description": bound.tool.description,
                    "connector_key": bound.connector_key,
                    "connector_version": bound.connector_version,
                    "effect_class": bound.tool.policy.effect_class,
                    "requires_confirmation": bound.tool.policy.requires_confirmation,
                    "input_schema": bound.tool.input_schema,
                }
                for bound in visible
            ],
        }

    @app.post("/v1/chat", tags=["runtime"])
    async def chat(payload: ChatRequest, request: Request, caller: CallerDep) -> dict[str, Any]:
        """Run one full orchestration turn for a natural-language request."""
        outcome = engine.handle(
            payload.utterance,
            caller=caller,
            request_id=getattr(request.state, "request_id", None),
            confirmed=payload.confirmed,
            dry_run=payload.dry_run,
        )
        return _outcome_body(outcome)

    @app.post("/v1/tools/{tool_name}/execute", tags=["runtime"])
    async def execute(
        tool_name: str,
        payload: ExecuteRequest,
        request: Request,
        caller: CallerDep,
    ) -> dict[str, Any]:
        """Execute one named tool with explicit arguments."""
        outcome = engine.execute_tool(
            tool_name=tool_name,
            arguments=payload.arguments,
            caller=caller,
            request_id=getattr(request.state, "request_id", None),
            confirmed=payload.confirmed,
        )
        return _outcome_body(outcome)

    @app.post("/v1/snapshot/refresh", tags=["operations"])
    async def refresh_snapshot() -> dict[str, Any]:
        """Force a snapshot refresh instead of waiting for the interval."""
        store: SnapshotStore = engine._store
        snapshot = store.refresh()
        return {
            "snapshot_revision": snapshot.revision,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.digest,
            "tool_count": len(snapshot.tools),
            "snapshot_signed": snapshot.signed,
            "snapshot_signing_key_id": snapshot.signing_key_id,
        }

    return app


def _outcome_body(outcome: OrchestrationOutcome) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": outcome.request_id,
        "snapshot": {
            "revision": outcome.snapshot_revision,
            "snapshot_id": outcome.snapshot_id,
        },
        "selected_tool": outcome.selected_tool,
        "connector_key": outcome.connector_key,
        "connector_version": outcome.connector_version,
        "arguments": outcome.arguments,
        "message": outcome.message,
        "trace": list(outcome.trace.steps),
    }
    if outcome.result is not None:
        body["result"] = {
            "status": outcome.result.status,
            "http_status": outcome.result.http_status,
            "duration_ms": outcome.result.duration_ms,
            "content": outcome.result.content,
            # Repeated on the wire so a client cannot forget. Anything under `content` came
            # from an upstream API and is attacker-influenceable; it is data, not
            # instructions, and a client must not feed it back as a prompt.
            "untrusted": True,
        }
    return body


app = create_app()
