"""The review model: what a human decided about a machine's proposal.

Analysis produces a *proposal*. Review records the decisions taken about it. Keeping the two
separate is what makes the pipeline auditable: re-running analysis on the same bytes never
alters a decision, and a decision always names the operation it applies to.

The rules a reviewer may change are exactly the ones a human should own:

* whether an operation becomes a tool at all;
* the model-facing description;
* how much effect the tool is allowed to have and whether it needs confirmation;
* which roles may call it.

The rules a reviewer may *not* change are the mechanical ones: the input schema, the path,
the method, and the bindings. Those are derived from the source document, and letting a
reviewer hand-edit them would mean the published tool no longer described the API it claims
to describe. Changing them means changing the source document and re-analyzing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from toollayer_contracts.errors import ValidationError
from toollayer_contracts.models import ToolAccessPolicy

__all__ = [
    "OperationReview",
    "ReviewState",
    "ReviewUpdate",
    "build_initial_review",
    "review_readiness",
]

Selection = Literal["included", "excluded"]
DescriptionOrigin = Literal["source", "generated", "assisted", "human"]
EffectClass = Literal["read", "write", "destructive"]

_MAX_DESCRIPTION = 1024


@dataclass(frozen=True, slots=True)
class OperationReview:
    """One reviewer's decisions about one analyzed operation."""

    operation_key: str
    selection: Selection
    description: str
    description_origin: DescriptionOrigin
    effect_class: EffectClass
    requires_confirmation: bool
    access_mode: Literal["public", "restricted"]
    allowed_roles: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_key": self.operation_key,
            "selection": self.selection,
            "description": self.description,
            "description_origin": self.description_origin,
            "effect_class": self.effect_class,
            "requires_confirmation": self.requires_confirmation,
            "access_mode": self.access_mode,
            "allowed_roles": list(self.allowed_roles),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OperationReview:
        return cls(
            operation_key=str(payload["operation_key"]),
            selection=payload.get("selection", "included"),
            description=str(payload.get("description", "")),
            description_origin=payload.get("description_origin", "source"),
            effect_class=payload.get("effect_class", "read"),
            requires_confirmation=bool(payload.get("requires_confirmation", False)),
            access_mode=payload.get("access_mode", "public"),
            allowed_roles=tuple(payload.get("allowed_roles", ())),
        )

    def access_policy(self) -> ToolAccessPolicy:
        return ToolAccessPolicy(
            access_mode=self.access_mode,
            allowed_roles=tuple(sorted(self.allowed_roles)),
        )


@dataclass(frozen=True, slots=True)
class ReviewState:
    """Every decision recorded for one draft."""

    operations: tuple[OperationReview, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"operations": [operation.to_dict() for operation in self.operations]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReviewState:
        raw = payload.get("operations", [])
        return cls(operations=tuple(OperationReview.from_dict(entry) for entry in raw))

    def get(self, operation_key: str) -> OperationReview | None:
        return next(
            (entry for entry in self.operations if entry.operation_key == operation_key), None
        )

    @property
    def included(self) -> tuple[OperationReview, ...]:
        return tuple(entry for entry in self.operations if entry.selection == "included")


@dataclass(frozen=True, slots=True)
class ReviewUpdate:
    """A requested change to one operation. Every field is optional.

    ``None`` means "leave this alone" rather than "clear this". A partial update is the
    normal case in a console where a reviewer changes one field at a time, and interpreting
    an omitted field as a clear would let a narrow edit silently reset a decision made
    earlier.
    """

    operation_key: str
    selection: Selection | None = None
    description: str | None = None
    description_origin: DescriptionOrigin | None = None
    effect_class: EffectClass | None = None
    requires_confirmation: bool | None = None
    access_mode: Literal["public", "restricted"] | None = None
    allowed_roles: tuple[str, ...] | None = None


def build_initial_review(analysis: dict[str, Any]) -> ReviewState:
    """Seed the review state from a fresh analysis.

    Every convertible operation starts *included* with the converter's defaults, so a
    reviewer's job is to narrow a working proposal rather than to assemble one from nothing.
    Operations that could not be converted get no review entry at all: there is nothing to
    decide about a tool that does not exist.
    """
    entries: list[OperationReview] = []
    for operation in analysis.get("operations", []):
        tool = operation.get("tool")
        if tool is None:
            continue
        policy = tool.get("policy", {})
        access = policy.get("access", {})
        entries.append(
            OperationReview(
                operation_key=str(operation["key"]),
                selection="included",
                description=str(tool["description"]),
                description_origin=tool.get("provenance", {}).get("description_origin", "source"),
                effect_class=policy.get("effect_class", "read"),
                requires_confirmation=bool(policy.get("requires_confirmation", False)),
                access_mode=access.get("access_mode", "public"),
                allowed_roles=tuple(access.get("allowed_roles", ())),
            )
        )
    return ReviewState(operations=tuple(entries))


def apply_update(state: ReviewState, update: ReviewUpdate) -> ReviewState:
    """Return a new review state with ``update`` applied to one operation."""
    existing = state.get(update.operation_key)
    if existing is None:
        raise ValidationError(
            "the draft has no reviewable operation with that key",
            pointer="/operation_key",
        )

    changes: dict[str, Any] = {}
    if update.selection is not None:
        if update.selection not in ("included", "excluded"):
            raise ValidationError(
                "selection must be 'included' or 'excluded'", pointer="/selection"
            )
        changes["selection"] = update.selection

    if update.description is not None:
        text = update.description.strip()
        if not text:
            raise ValidationError("a description may not be empty", pointer="/description")
        if len(text) > _MAX_DESCRIPTION:
            raise ValidationError(
                f"a description may not exceed {_MAX_DESCRIPTION} characters",
                pointer="/description",
            )
        changes["description"] = text
        # A description that changed without an explicit origin was written by the person
        # making the request. Recording it as anything else would misattribute the text.
        changes["description_origin"] = update.description_origin or "human"
    elif update.description_origin is not None:
        changes["description_origin"] = update.description_origin

    if update.effect_class is not None:
        if update.effect_class not in ("read", "write", "destructive"):
            raise ValidationError("effect_class is not a supported value", pointer="/effect_class")
        changes["effect_class"] = update.effect_class

    if update.requires_confirmation is not None:
        changes["requires_confirmation"] = update.requires_confirmation

    access_mode = update.access_mode if update.access_mode is not None else existing.access_mode
    allowed_roles = (
        tuple(update.allowed_roles) if update.allowed_roles is not None else existing.allowed_roles
    )
    if update.access_mode is not None or update.allowed_roles is not None:
        # Validated by constructing the contract model, so the console cannot store a policy
        # the published artifact would reject.
        try:
            policy = ToolAccessPolicy(access_mode=access_mode, allowed_roles=allowed_roles)
        except Exception as error:
            raise ValidationError(str(error).split("\n")[0], pointer="/access") from None
        changes["access_mode"] = policy.access_mode
        changes["allowed_roles"] = policy.allowed_roles

    updated = replace(existing, **changes)
    return ReviewState(
        operations=tuple(
            updated if entry.operation_key == update.operation_key else entry
            for entry in state.operations
        )
    )


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether a draft can be published, and what is blocking it if not."""

    ready: bool
    issues: tuple[str, ...]


def review_readiness(
    analysis: dict[str, Any],
    state: ReviewState,
    *,
    base_url: str | None,
) -> Readiness:
    """Decide whether the reviewed draft can produce a publishable connector.

    Publication is refused rather than repaired. Every issue reported here is one a person
    has to resolve, because each one represents a judgement the pipeline should not make on
    its own: what the tool is for, where it points, and whether there is anything to publish.
    """
    issues: list[str] = []

    if not base_url:
        issues.append("publication.base_url_missing")

    included = state.included
    if not included:
        issues.append("publication.no_operation_selected")

    analyzed_keys = {
        str(operation["key"])
        for operation in analysis.get("operations", [])
        if operation.get("tool") is not None
    }
    reviewed_keys = {entry.operation_key for entry in state.operations}
    if reviewed_keys != analyzed_keys:
        # The review and the analysis have to describe the same set of operations. If they
        # do not, the draft was analyzed again after review and the decisions no longer
        # apply to what would be published.
        issues.append("publication.review_out_of_sync")

    for entry in included:
        if entry.description_origin == "generated":
            issues.append(f"publication.description_placeholder:{entry.operation_key}")
        if entry.access_mode == "restricted" and not entry.allowed_roles:
            issues.append(f"publication.restricted_without_roles:{entry.operation_key}")

    return Readiness(ready=not issues, issues=tuple(issues))
