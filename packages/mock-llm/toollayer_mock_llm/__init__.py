"""A deterministic, offline stand-in for a model provider.

The whole demo and the whole test suite run against this, with no API key and no network
egress. That is deliberate: the parts of the system worth demonstrating are the validation,
authorization, and execution boundaries, and those are only testable if the component that
proposes tool calls behaves identically on every run.

A real provider drops into the same three-method seam.
"""

from __future__ import annotations

from toollayer_mock_llm.provider import (
    ArgumentProposal,
    LLMProvider,
    MockLLMProvider,
    ToolSelection,
)

__all__ = ["ArgumentProposal", "LLMProvider", "MockLLMProvider", "ToolSelection"]
