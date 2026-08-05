"""The single authorization decision.

Both boundaries in the runtime call :func:`authorize_tool`: the one that decides which tools
a caller may *see*, and the one that decides which tools a caller may *run*. That is the
whole point of putting it in one function.

If those two boundaries had separate implementations they would drift, and the drift always
goes the same direction — the discovery filter gets a new rule that the execution path does
not. Filtering a tool out of a list is a usability measure, not a security control: a caller
who names the tool anyway must still be stopped. One function, called twice, cannot drift.

Every unreadable input denies. A tool whose stored policy cannot be parsed is treated as
restricted-and-unsatisfiable rather than public, so a corrupted or truncated artifact closes
access instead of opening it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "DENY_REASONS",
    "AuthorizationDecision",
    "CallerIdentity",
    "ToolAudiencePolicy",
    "authorize_tool",
    "parse_audience_policy",
]

_ROLE_KEY: Final = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_MAX_ROLES: Final = 32

DENY_REASONS: Final[frozenset[str]] = frozenset(
    {
        "role_not_permitted",
        "caller_has_no_roles",
        "tool_policy_unreadable",
    }
)

#: The one message a denied caller sees. It states the outcome without naming the roles that
#: would have satisfied the policy, because that turns every denial into a probe that maps
#: the permission model.
DENIED_MESSAGE: Final = "This caller is not permitted to use the requested tool."


class ToolPolicyError(ValueError):
    """A stored tool access policy cannot be read."""


@dataclass(frozen=True, slots=True)
class ToolAudiencePolicy:
    """One tool's role restriction, as published in the artifact."""

    access_mode: str = "public"
    allowed_roles: frozenset[str] = frozenset()

    @property
    def restricted(self) -> bool:
        return self.access_mode == "restricted"


PUBLIC_POLICY: Final = ToolAudiencePolicy()


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """Who is asking.

    ``roles`` is deliberately a plain set of keys resolved by the host application before it
    reaches the runtime. The runtime does not authenticate anyone; it enforces what the host
    asserts. Making that explicit keeps the trust boundary visible instead of implied.
    """

    subject: str
    roles: frozenset[str] = frozenset()

    @classmethod
    def of(cls, subject: str, roles: Iterable[str] = ()) -> CallerIdentity:
        return cls(subject=subject, roles=frozenset(roles))


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """The outcome, plus the metadata that is safe to log."""

    allowed: bool
    reason_code: str | None
    access_mode: str

    @property
    def denied(self) -> bool:
        return not self.allowed


def parse_audience_policy(policy: object) -> ToolAudiencePolicy:
    """Read a tool's ``policy`` object into an audience policy.

    A policy with no ``access`` object is public. Anything present but unreadable raises,
    so the caller denies rather than falling back to a permissive default.
    """
    if not isinstance(policy, Mapping):
        raise ToolPolicyError("the tool policy is not an object")
    access = policy.get("access")
    if access is None:
        return PUBLIC_POLICY
    if not isinstance(access, Mapping):
        raise ToolPolicyError("the tool access policy is not an object")

    mode = access.get("access_mode")
    if mode not in ("public", "restricted"):
        raise ToolPolicyError("the tool access mode is not supported")
    roles = _roles(access.get("allowed_roles"))

    if mode == "restricted" and not roles:
        # A restriction that restricts nothing behaves as public while reading as a
        # restriction. The Control Plane rejects it at authoring time; if one somehow
        # reaches the runtime it denies rather than opens.
        raise ToolPolicyError("a restricted tool allows no role")
    if mode == "public" and roles:
        raise ToolPolicyError("a public tool carries a role list")

    return ToolAudiencePolicy(access_mode=mode, allowed_roles=roles)


def authorize_tool(
    *,
    policy: ToolAudiencePolicy | None,
    caller: CallerIdentity | None,
) -> AuthorizationDecision:
    """Decide whether ``caller`` may use the tool ``policy`` belongs to.

    ``policy`` of ``None`` means the stored policy could not be read, which denies.
    """
    if policy is None:
        return AuthorizationDecision(
            allowed=False, reason_code="tool_policy_unreadable", access_mode="restricted"
        )

    if not policy.restricted:
        return AuthorizationDecision(allowed=True, reason_code=None, access_mode="public")

    if caller is None or not caller.roles:
        # A restricted tool with no resolved roles fails closed. This is the ordinary state
        # for an anonymous or not-yet-provisioned caller, not an exceptional one.
        return AuthorizationDecision(
            allowed=False, reason_code="caller_has_no_roles", access_mode="restricted"
        )

    if caller.roles & policy.allowed_roles:
        return AuthorizationDecision(allowed=True, reason_code=None, access_mode="restricted")

    return AuthorizationDecision(
        allowed=False, reason_code="role_not_permitted", access_mode="restricted"
    )


def authorize_stored_policy(
    tool_policy: object, caller: CallerIdentity | None
) -> AuthorizationDecision:
    """Parse a tool's stored policy and decide, denying on unreadable input."""
    try:
        policy: ToolAudiencePolicy | None = parse_audience_policy(tool_policy)
    except ToolPolicyError:
        policy = None
    return authorize_tool(policy=policy, caller=caller)


def _roles(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ToolPolicyError("the allowed role list is not an array")
    if len(value) > _MAX_ROLES:
        raise ToolPolicyError("the allowed role list is too long")
    roles: set[str] = set()
    for item in value:
        if not isinstance(item, str) or _ROLE_KEY.fullmatch(item) is None:
            raise ToolPolicyError("a role key is not canonical")
        roles.add(item)
    return frozenset(roles)
