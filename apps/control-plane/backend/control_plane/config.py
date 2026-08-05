"""Control Plane configuration.

Configuration is read from the environment once, validated, and then frozen. Reading
``os.environ`` from inside request handling would make behavior depend on when a value was
looked up, and it makes tests order-dependent.

Tokens are compared with a constant-time comparison and are never logged, never echoed in an
error, and never returned by any endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

__all__ = ["ControlPlaneSettings", "get_settings", "reset_settings_cache"]

_MIN_TOKEN_LENGTH: Final = 16

#: Development defaults. They are obviously non-secret on purpose: a placeholder that looks
#: like a real credential is one that ends up in production by accident.
_DEV_ADMIN_TOKEN: Final = "dev-admin-token-change-me"
_DEV_SERVICE_TOKEN: Final = "dev-service-token-change-me"


class ConfigurationError(ValueError):
    """The environment does not describe a usable Control Plane."""


@dataclass(frozen=True, slots=True)
class ControlPlaneSettings:
    """Validated, immutable Control Plane settings."""

    database_url: str = "sqlite:///./data/control-plane.db"
    admin_token: str = _DEV_ADMIN_TOKEN
    service_token: str = _DEV_SERVICE_TOKEN
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)
    max_source_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if len(self.admin_token) < _MIN_TOKEN_LENGTH:
            raise ConfigurationError(
                f"the admin token must be at least {_MIN_TOKEN_LENGTH} characters"
            )
        if len(self.service_token) < _MIN_TOKEN_LENGTH:
            raise ConfigurationError(
                f"the service token must be at least {_MIN_TOKEN_LENGTH} characters"
            )
        if self.admin_token == self.service_token:
            # One credential for two audiences means the runtime's service token would also
            # authorize authoring. They are separate roles, so they get separate tokens.
            raise ConfigurationError("the admin and service tokens must differ")
        if not self.database_url:
            raise ConfigurationError("a database URL is required")

    @property
    def uses_development_tokens(self) -> bool:
        """Whether either credential is still the shipped placeholder."""
        return self.admin_token == _DEV_ADMIN_TOKEN or self.service_token == _DEV_SERVICE_TOKEN


def _env_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer") from None


@lru_cache(maxsize=1)
def get_settings() -> ControlPlaneSettings:
    """Read and validate settings from the environment, once per process."""
    return ControlPlaneSettings(
        database_url=os.environ.get("TOOLLAYER_CONTROL_PLANE_DATABASE_URL")
        or "sqlite:///./data/control-plane.db",
        admin_token=os.environ.get("TOOLLAYER_ADMIN_TOKEN") or _DEV_ADMIN_TOKEN,
        service_token=os.environ.get("TOOLLAYER_SERVICE_TOKEN") or _DEV_SERVICE_TOKEN,
        cors_origins=_env_tuple("TOOLLAYER_CONTROL_PLANE_CORS_ORIGINS", ("http://localhost:5173",)),
        max_source_bytes=_env_int("TOOLLAYER_MAX_SOURCE_BYTES", 2 * 1024 * 1024),
    )


def reset_settings_cache() -> None:
    """Clear the cached settings. Used by tests that change the environment."""
    get_settings.cache_clear()
