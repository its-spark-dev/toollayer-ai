"""Projection to an Anthropic-compatible tool format.

Shape produced::

    {"name": ..., "description": ..., "input_schema": {...}}

This adapter exists to keep the provider-neutrality claim honest. A single adapter proves
nothing: the canonical format could simply be a rename of that provider's format. Two
adapters with genuinely different requirements force the canonical layer to be the source of
truth, and they surface where the providers actually diverge.

The concrete divergence this project hits is optionality. Anthropic's tool schema is ordinary
JSON Schema, so optional properties stay optional and nothing has to be normalized — unlike
the OpenAI strict-mode projection, which must widen optional properties to nullable. The
canonical definition is unchanged in both cases; only the projections differ.

The dialect marker is retained here because the target accepts a plain JSON Schema object.
"""

from __future__ import annotations

from typing import Any

from toollayer_contracts.adapters.base import ProviderAdapter
from toollayer_contracts.models import ToolDefinition

__all__ = ["AnthropicToolAdapter"]


class AnthropicToolAdapter(ProviderAdapter):
    """Project canonical tools into an Anthropic-compatible tool payload."""

    provider = "anthropic"

    def project_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        schema = self._prepared_schema(tool)
        return {
            "name": tool.tool_name,
            "description": tool.description,
            "input_schema": schema,
        }
