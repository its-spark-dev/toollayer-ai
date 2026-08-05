"""Execution policy: who may call what, where it may go, and what may come back.

This package owns every rule that can refuse a tool call. It is deliberately independent of
both services: the Control Plane imports it to show a reviewer what policy a tool will be
subject to, and the Runtime imports it to enforce that policy. Neither owns it, so the two
cannot end up enforcing different rules.

The governing principle is default deny. An empty destination allowlist permits nothing, an
unreadable access policy denies, an unvalidated argument never reaches request construction,
and every bound is finite.
"""

from __future__ import annotations

from toollayer_policy.arguments import (
    ArgumentValidationError,
    PreparedRequest,
    prepare_request,
    validate_arguments,
)
from toollayer_policy.authorization import (
    DENIED_MESSAGE,
    DENY_REASONS,
    AuthorizationDecision,
    CallerIdentity,
    ToolAudiencePolicy,
    ToolPolicyError,
    authorize_stored_policy,
    authorize_tool,
    parse_audience_policy,
)
from toollayer_policy.destinations import (
    DEFAULT_ALLOWED_METHODS,
    DestinationPolicy,
    DnsResolver,
    ResolvedDestination,
    SystemDnsResolver,
    normalize_origin,
)
from toollayer_policy.executor import ExecutionLimits, HttpTransport, ToolExecutor
from toollayer_policy.redaction import REDACTED, redact_headers, redact_mapping, redact_url

__all__ = [
    "DEFAULT_ALLOWED_METHODS",
    "DENIED_MESSAGE",
    "DENY_REASONS",
    "REDACTED",
    "ArgumentValidationError",
    "AuthorizationDecision",
    "CallerIdentity",
    "DestinationPolicy",
    "DnsResolver",
    "ExecutionLimits",
    "HttpTransport",
    "PreparedRequest",
    "ResolvedDestination",
    "SystemDnsResolver",
    "ToolAudiencePolicy",
    "ToolExecutor",
    "ToolPolicyError",
    "authorize_stored_policy",
    "authorize_tool",
    "normalize_origin",
    "parse_audience_policy",
    "prepare_request",
    "redact_headers",
    "redact_mapping",
    "redact_url",
    "validate_arguments",
]
