"""The Sample Support API: a synthetic upstream service for the demo.

This exists so the runtime has something real to call. It is intentionally ordinary — a
small REST API with list, filter, retrieve, and mutate operations — because the interesting
engineering is in the layers around it, not in it.

Its OpenAPI document is hand-authored at ``examples/support-api.openapi.yaml`` rather than
generated from this app. The document is the *input* to the Control Plane, so writing it by
hand keeps the pipeline honest: the converter is exercised against a specification a person
wrote, with the descriptions, enums, and optionality a real one has, instead of against a
serialization of the very code it is meant to describe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from demo_api.data import TicketStore, seed_state

__all__ = ["app", "create_app"]

TicketStatus = Literal["open", "in_progress", "waiting_on_customer", "resolved", "closed"]
TicketPriority = Literal["low", "medium", "high", "urgent"]

#: Transitions the desk allows. A ticket cannot jump from `closed` back to `open`; it has to
#: be reopened through `resolved` first. The rule exists so the demo has a genuine
#: business-rule rejection (HTTP 409) alongside the policy rejections in the runtime.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"in_progress", "waiting_on_customer", "resolved", "closed"}),
    "in_progress": frozenset({"waiting_on_customer", "resolved", "open"}),
    "waiting_on_customer": frozenset({"in_progress", "resolved", "closed"}),
    "resolved": frozenset({"closed", "open"}),
    "closed": frozenset({"resolved"}),
}


class AssignRequest(BaseModel):
    model_config = {"extra": "forbid"}

    assignee_id: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=280)


class StatusRequest(BaseModel):
    model_config = {"extra": "forbid"}

    status: TicketStatus
    resolution_note: str | None = Field(default=None, max_length=280)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _problem(status_code: int, code: str, message: str) -> HTTPException:
    """Return a failure in the same shape for every endpoint."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def create_app(store: TicketStore | None = None) -> FastAPI:
    """Build the demo API. A caller may inject a store to get deterministic state."""
    state = store if store is not None else seed_state()

    app = FastAPI(
        title="Sample Support API",
        version="1.0.0",
        summary="A synthetic support-ticket API used to demonstrate ToolLayer AI.",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    @app.exception_handler(HTTPException)
    async def _handle_http_exception(_: Any, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": "request_failed", "message": str(detail)},
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/tickets", tags=["tickets"], summary="List support tickets")
    async def list_tickets(
        status: Annotated[TicketStatus | None, Query()] = None,
        priority: Annotated[TicketPriority | None, Query()] = None,
        team_id: Annotated[str | None, Query(max_length=64)] = None,
        assignee_id: Annotated[str | None, Query(max_length=64)] = None,
        unassigned_only: Annotated[bool | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        matches = [
            ticket
            for ticket in state.tickets
            if (status is None or ticket["status"] == status)
            and (priority is None or ticket["priority"] == priority)
            and (team_id is None or ticket["team_id"] == team_id)
            and (assignee_id is None or ticket["assignee_id"] == assignee_id)
            and (not unassigned_only or ticket["assignee_id"] is None)
        ]
        matches.sort(key=lambda ticket: ticket["created_at"], reverse=True)
        window = matches[:limit]
        return {
            "items": [_summary(ticket) for ticket in window],
            "total_matched": len(matches),
            "returned": len(window),
        }

    @app.get("/v1/tickets/{ticket_id}", tags=["tickets"], summary="Retrieve one support ticket")
    async def get_ticket(
        ticket_id: Annotated[str, Path(max_length=64)],
    ) -> dict[str, Any]:
        ticket = state.ticket(ticket_id)
        if ticket is None:
            raise _problem(404, "ticket_not_found", "No ticket exists with that identifier.")
        return dict(ticket)

    @app.post("/v1/tickets/{ticket_id}/assignment", tags=["tickets"], summary="Assign a ticket")
    async def assign_ticket(
        ticket_id: Annotated[str, Path(max_length=64)],
        payload: AssignRequest,
    ) -> dict[str, Any]:
        ticket = state.ticket(ticket_id)
        if ticket is None:
            raise _problem(404, "ticket_not_found", "No ticket exists with that identifier.")
        member = state.member(payload.assignee_id)
        if member is None:
            raise _problem(404, "member_not_found", "No team member exists with that identifier.")
        if member["team_id"] != ticket["team_id"]:
            raise _problem(
                409,
                "member_not_on_team",
                "The assignee does not belong to the team that owns this ticket.",
            )
        ticket["assignee_id"] = payload.assignee_id
        ticket["updated_at"] = _now()
        if ticket["status"] == "open":
            ticket["status"] = "in_progress"
        return dict(ticket)

    @app.post("/v1/tickets/{ticket_id}/status", tags=["tickets"], summary="Change ticket status")
    async def change_status(
        ticket_id: Annotated[str, Path(max_length=64)],
        payload: StatusRequest,
    ) -> dict[str, Any]:
        ticket = state.ticket(ticket_id)
        if ticket is None:
            raise _problem(404, "ticket_not_found", "No ticket exists with that identifier.")
        current = str(ticket["status"])
        if payload.status == current:
            return dict(ticket)
        if payload.status not in _ALLOWED_TRANSITIONS[current]:
            raise _problem(
                409,
                "invalid_status_transition",
                f"A ticket cannot move from {current} to {payload.status}.",
            )
        ticket["status"] = payload.status
        ticket["updated_at"] = _now()
        if payload.resolution_note:
            ticket["resolution_note"] = payload.resolution_note
        return dict(ticket)

    @app.get("/v1/teams", tags=["teams"], summary="List support teams")
    async def list_teams() -> dict[str, Any]:
        return {"items": [dict(team) for team in state.teams]}

    @app.get(
        "/v1/teams/{team_id}/members",
        tags=["teams"],
        summary="List the members of one support team",
    )
    async def list_team_members(
        team_id: Annotated[str, Path(max_length=64)],
        available_only: Annotated[bool | None, Query()] = None,
    ) -> dict[str, Any]:
        if state.team(team_id) is None:
            raise _problem(404, "team_not_found", "No team exists with that identifier.")
        members = [
            dict(member)
            for member in state.members
            if member["team_id"] == team_id and (not available_only or member["available"])
        ]
        return {"items": members}

    return app


def _summary(ticket: dict[str, Any]) -> dict[str, Any]:
    """Project the list view. Bodies are omitted so list responses stay small."""
    return {
        key: ticket[key]
        for key in (
            "ticket_id",
            "subject",
            "status",
            "priority",
            "team_id",
            "assignee_id",
            "created_at",
            "updated_at",
        )
    }


app = create_app()
