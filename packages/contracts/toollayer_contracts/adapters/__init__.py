"""Provider adapters: canonical tool definitions projected into provider tool formats."""

from __future__ import annotations

from toollayer_contracts.adapters.anthropic_adapter import AnthropicToolAdapter
from toollayer_contracts.adapters.base import (
    AdapterDiagnostic,
    AdapterError,
    ProviderAdapter,
    ProviderProjection,
)
from toollayer_contracts.adapters.openai_adapter import (
    OpenAIToolAdapter,
    normalize_provider_arguments,
)

__all__ = [
    "ADAPTERS",
    "SUPPORTED_PROVIDERS",
    "AdapterDiagnostic",
    "AdapterError",
    "AnthropicToolAdapter",
    "OpenAIToolAdapter",
    "ProviderAdapter",
    "ProviderProjection",
    "get_adapter",
    "normalize_provider_arguments",
]

ADAPTERS: dict[str, ProviderAdapter] = {
    OpenAIToolAdapter.provider: OpenAIToolAdapter(),
    AnthropicToolAdapter.provider: AnthropicToolAdapter(),
}

SUPPORTED_PROVIDERS: tuple[str, ...] = tuple(sorted(ADAPTERS))


def get_adapter(provider: str) -> ProviderAdapter:
    """Return the adapter for ``provider``.

    Raises ``KeyError`` for an unknown provider rather than falling back to a default,
    because silently projecting into the wrong format would produce tools that a model
    accepts and then calls incorrectly.
    """
    try:
        return ADAPTERS[provider]
    except KeyError:
        raise KeyError(f"unsupported provider: {provider!r}") from None
