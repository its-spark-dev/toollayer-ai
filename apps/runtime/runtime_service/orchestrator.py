"""The orchestration sequence.

One request, one fixed order, and every step able to refuse:

1. **Refresh** the snapshot if it is stale, and verify whatever comes back.
2. **Discover** the tools this caller is authorized to see.
3. **Select** one of them, or stop.
4. **Generate** arguments, or stop and say what is missing.
5. **Validate** the arguments against the published JSON Schema.
6. **Authorize** the caller against the selected tool's policy.
7. **Execute** through the governed HTTP boundary.
8. **Format** a response from the result.

Two orderings in that list are load-bearing.

**Authorization runs after selection and before execution**, on the policy carried by the
snapshot the runtime holds. Filtering the discovery list is a usability measure; this is the
step that decides whether the API is reached. It therefore holds against a fabricated call, a
stale client-side tool list, and a policy that changed since discovery.

**Formatting never feeds back into selection.** The result of step 7 is data. It is
summarized in step 8 and the request ends. There is no loop, so text inside a tool result
cannot cause another tool call — which is the concrete mechanism behind the injection tests.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from toollayer_contracts.errors import (
    AuthorizationError,
    ErrorCode,
    ToolLayerError,
    ValidationError,
)
from toollayer_contracts.models import ToolExecutionResult
from toollayer_policy import (
    CallerIdentity,
    ToolExecutor,
    authorize_stored_policy,
    validate_arguments,
)
from toollayer_mock_llm import LLMProvider
from runtime_service.snapshot import BoundTool, SnapshotStore

__all__ = ["Orchestrator", "OrchestrationOutcome", "ToolTrace"]

logger = logging.getLogger("toollayer.runtime.orchestrator")


class NoToolSelected(ToolLayerError):
    code = ErrorCode.NO_TOOL_SELECTED


class ConfirmationRequired(ToolLayerError):
    code = ErrorCode.CONFIRMATION_REQUIRED


@dataclass(frozen=True, slots=True)
class ToolTrace:
    """What the runtime did, in the order it did it.

    Returned with every response so the demo — and a reviewer — can see the decisions rather
    than infer them from the answer. It contains no secret, no header, and no raw upstream
    payload.
    """

    steps: tuple[dict[str, Any], ...] = ()

    def with_step(self, step: str, **detail: Any) -> ToolTrace:
        return ToolTrace(steps=(*self.steps, {"step": step, **detail}))


@dataclass(frozen=True, slots=True)
class OrchestrationOutcome:
    """Everything one orchestrated request produced."""

    request_id: str
    snapshot_revision: int
    snapshot_id: str
    selected_tool: str | None
    connector_key: str | None
    connector_version: str | None
    arguments: dict[str, Any] = field(default_factory=dict)
    result: ToolExecutionResult | None = None
    message: str = ""
    trace: ToolTrace = ToolTrace()


class Orchestrator:
    """Turns a natural-language request into at most one governed tool call."""

    def __init__(
        self,
        *,
        store: SnapshotStore,
        provider: LLMProvider,
        executor: ToolExecutor,
    ) -> None:
        self._store = store
        self._provider = provider
        self._executor = executor

    # ------------------------------------------------------------------ discovery

    def discover(self, caller: CallerIdentity | None) -> tuple[BoundTool, ...]:
        """Return the tools ``caller`` is authorized to see.

        Uses the same authorization function as execution. That is the entire reason the
        function lives in the shared policy package: if discovery had its own copy, the two
        would eventually disagree about who may use what.
        """
        snapshot = self._store.ensure_fresh()
        visible: list[BoundTool] = []
        for bound in snapshot.tools:
            decision = authorize_stored_policy(bound.tool.policy.model_dump(mode="json"), caller)
            if decision.allowed:
                visible.append(bound)
        return tuple(visible)

    # ------------------------------------------------------------------ execution

    def execute_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        caller: CallerIdentity | None,
        request_id: str | None = None,
        confirmed: bool = False,
    ) -> OrchestrationOutcome:
        """Execute one named tool with caller-supplied arguments.

        This is the path a real AI client uses after its model emits a tool call. It repeats
        every check rather than assuming the client already made them, because the client is
        exactly the component whose output cannot be trusted.
        """
        request_id = request_id or uuid.uuid4().hex[:16]
        snapshot = self._store.ensure_fresh()
        trace = ToolTrace().with_step(
            "snapshot_loaded",
            revision=snapshot.revision,
            snapshot_id=snapshot.snapshot_id,
        )

        bound = snapshot.resolve(tool_name)
        trace = trace.with_step(
            "tool_resolved",
            tool=bound.tool_name,
            connector=bound.connector_key,
            version=bound.connector_version,
        )

        validated = validate_arguments(bound.tool, arguments)
        trace = trace.with_step("arguments_validated", argument_names=sorted(validated))

        self._authorize(bound, caller)
        trace = trace.with_step("policy_evaluated", effect=bound.tool.policy.effect_class)

        if bound.tool.policy.requires_confirmation and not confirmed:
            # Confirmation is enforced here, not left to the client's good manners. A tool
            # marked as needing explicit approval does not execute without it, regardless of
            # what the caller's UI did or did not show a user.
            raise ConfirmationRequired(
                "this tool requires explicit confirmation before it can be executed"
            )

        result = self._executor.execute(
            bound.tool,
            validated,
            base_url=bound.base_url,
            connector_key=bound.connector_key,
            connector_version=bound.connector_version,
            request_id=request_id,
        )
        trace = trace.with_step(
            "executed", http_status=result.http_status, duration_ms=result.duration_ms
        )

        message = self._provider.format_response(tool_name, bound.tool, result.content)
        return OrchestrationOutcome(
            request_id=request_id,
            snapshot_revision=snapshot.revision,
            snapshot_id=snapshot.snapshot_id,
            selected_tool=bound.tool_name,
            connector_key=bound.connector_key,
            connector_version=bound.connector_version,
            arguments=validated,
            result=result,
            message=message,
            trace=trace,
        )

    # ------------------------------------------------------------------ full turn

    def handle(
        self,
        utterance: str,
        *,
        caller: CallerIdentity | None,
        request_id: str | None = None,
        confirmed: bool = False,
        dry_run: bool = False,
    ) -> OrchestrationOutcome:
        """Run the whole sequence for one natural-language request."""
        request_id = request_id or uuid.uuid4().hex[:16]
        text = (utterance or "").strip()
        if not text:
            raise ValidationError("the request text may not be empty", pointer="/utterance")
        if len(text) > 4000:
            raise ValidationError("the request text is too long", pointer="/utterance")

        snapshot = self._store.ensure_fresh()
        trace = ToolTrace().with_step(
            "snapshot_loaded",
            revision=snapshot.revision,
            snapshot_id=snapshot.snapshot_id,
            digest=snapshot.digest,
        )

        available = self.discover(caller)
        trace = trace.with_step(
            "tools_discovered",
            visible=[bound.tool_name for bound in available],
            hidden=len(snapshot.tools) - len(available),
        )

        if not available:
            raise NoToolSelected("no tool in this deployment is available to this caller")

        selection = self._provider.select_tool(text, tuple(bound.tool for bound in available))
        if selection is None:
            raise NoToolSelected(
                "the request did not match any available tool closely enough to act on"
            )
        trace = trace.with_step(
            "tool_selected",
            tool=selection.tool_name,
            score=selection.score,
            rationale=selection.rationale,
            runner_up=selection.runner_up,
        )

        # Resolved against the snapshot, not against the provider's answer. A provider that
        # returned a name outside the candidate list is refused here rather than trusted.
        bound = snapshot.resolve(selection.tool_name)

        proposal = self._provider.generate_arguments(text, bound.tool)
        if not proposal.complete:
            raise ValidationError(
                "the request does not supply every required argument: "
                + ", ".join(proposal.missing_required),
                code=ErrorCode.ARGUMENT_VALIDATION_FAILED,
                pointer="/utterance",
            )
        trace = trace.with_step("arguments_generated", argument_names=sorted(proposal.arguments))

        validated = validate_arguments(bound.tool, proposal.arguments)
        trace = trace.with_step("arguments_validated", arguments=validated)

        self._authorize(bound, caller)
        trace = trace.with_step(
            "policy_evaluated",
            effect=bound.tool.policy.effect_class,
            requires_confirmation=bound.tool.policy.requires_confirmation,
        )

        if bound.tool.policy.requires_confirmation and not confirmed:
            raise ConfirmationRequired(
                "this tool requires explicit confirmation before it can be executed"
            )

        if dry_run:
            prepared = self._executor.prepare(bound.tool, validated, base_url=bound.base_url)
            return OrchestrationOutcome(
                request_id=request_id,
                snapshot_revision=snapshot.revision,
                snapshot_id=snapshot.snapshot_id,
                selected_tool=bound.tool_name,
                connector_key=bound.connector_key,
                connector_version=bound.connector_version,
                arguments=validated,
                message=f"{prepared.method} {prepared.path} (not executed)",
                trace=trace.with_step("dry_run", method=prepared.method, path=prepared.path),
            )

        result = self._executor.execute(
            bound.tool,
            validated,
            base_url=bound.base_url,
            connector_key=bound.connector_key,
            connector_version=bound.connector_version,
            request_id=request_id,
        )
        trace = trace.with_step(
            "executed", http_status=result.http_status, duration_ms=result.duration_ms
        )

        # The result is summarized and the turn ends. Nothing in `result.content` is read as
        # an instruction, and nothing re-enters selection.
        message = self._provider.format_response(text, bound.tool, result.content)
        trace = trace.with_step("response_formatted", untrusted_content=True)

        return OrchestrationOutcome(
            request_id=request_id,
            snapshot_revision=snapshot.revision,
            snapshot_id=snapshot.snapshot_id,
            selected_tool=bound.tool_name,
            connector_key=bound.connector_key,
            connector_version=bound.connector_version,
            arguments=validated,
            result=result,
            message=message,
            trace=trace,
        )

    def _authorize(self, bound: BoundTool, caller: CallerIdentity | None) -> None:
        decision = authorize_stored_policy(bound.tool.policy.model_dump(mode="json"), caller)
        if decision.denied:
            logger.info(
                "denied tool call",
                extra={
                    "tool": bound.tool_name,
                    "connector": bound.connector_key,
                    "reason": decision.reason_code,
                },
            )
            # The reason code is logged for operators but not returned. Telling a caller
            # *which* role would have worked turns every denial into a probe of the
            # permission model.
            raise AuthorizationError(
                "this caller is not permitted to use the requested tool",
                code=ErrorCode.ROLE_NOT_PERMITTED,
            )
