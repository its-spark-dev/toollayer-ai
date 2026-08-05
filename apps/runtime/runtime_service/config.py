"""Runtime configuration.

The runtime is deliberately hard to misconfigure into something permissive. Its destination
allowlist starts empty, its loopback and plaintext escape hatches are off, and both bounds on
outbound requests have finite defaults. Turning any of them on requires an explicit
environment variable, which means an insecure runtime is a decision someone made rather than
a default they inherited.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Final

from toollayer_policy import DEFAULT_ALLOWED_METHODS, DestinationPolicy, ExecutionLimits

__all__ = ["RuntimeSettings", "get_settings", "reset_settings_cache"]

_DEV_SERVICE_TOKEN: Final = "dev-service-token-change-me"


class ConfigurationError(ValueError):
    """The environment does not describe a usable runtime."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be a number") from None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer") from None


def _env_tuple(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Validated, immutable runtime settings."""

    control_plane_url: str = "http://localhost:8080"
    service_token: str = _DEV_SERVICE_TOKEN
    deployment_key: str = "demo-workspace"

    allowed_origins: tuple[str, ...] = ()
    allow_plaintext_http: bool = False
    allow_loopback: bool = False
    allow_private_addresses: bool = False

    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 10.0
    max_response_bytes: int = 1024 * 1024

    snapshot_refresh_seconds: int = 60
    cors_origins: tuple[str, ...] = field(default=("http://localhost:5173",))

    def destination_policy(self) -> DestinationPolicy:
        return DestinationPolicy.from_origins(
            list(self.allowed_origins),
            allowed_methods=DEFAULT_ALLOWED_METHODS,
            allow_plaintext_http=self.allow_plaintext_http,
            allow_loopback=self.allow_loopback,
            allow_private_addresses=self.allow_private_addresses,
        )

    def execution_limits(self) -> ExecutionLimits:
        return ExecutionLimits(
            connect_timeout_seconds=self.connect_timeout_seconds,
            read_timeout_seconds=self.read_timeout_seconds,
            max_response_bytes=self.max_response_bytes,
        )

    @property
    def relaxed_for_local_development(self) -> bool:
        """Whether any destination escape hatch is enabled.

        Surfaced by ``/healthz`` so that a relaxed runtime says so about itself instead of
        looking identical to a locked-down one.
        """
        return self.allow_plaintext_http or self.allow_loopback or self.allow_private_addresses


@lru_cache(maxsize=1)
def get_settings() -> RuntimeSettings:
    """Read and validate settings from the environment, once per process."""
    return RuntimeSettings(
        control_plane_url=os.environ.get("TOOLLAYER_CONTROL_PLANE_URL", "http://localhost:8080"),
        service_token=os.environ.get("TOOLLAYER_SERVICE_TOKEN") or _DEV_SERVICE_TOKEN,
        deployment_key=os.environ.get("TOOLLAYER_DEPLOYMENT_KEY", "demo-workspace"),
        allowed_origins=_env_tuple("TOOLLAYER_ALLOWED_ORIGINS"),
        allow_plaintext_http=_env_bool("TOOLLAYER_ALLOW_PLAINTEXT_HTTP"),
        allow_loopback=_env_bool("TOOLLAYER_ALLOW_LOOPBACK_DESTINATIONS"),
        allow_private_addresses=_env_bool("TOOLLAYER_ALLOW_PRIVATE_ADDRESSES"),
        connect_timeout_seconds=_env_float("TOOLLAYER_CONNECT_TIMEOUT_SECONDS", 3.0),
        read_timeout_seconds=_env_float("TOOLLAYER_READ_TIMEOUT_SECONDS", 10.0),
        max_response_bytes=_env_int("TOOLLAYER_MAX_RESPONSE_BYTES", 1024 * 1024),
        snapshot_refresh_seconds=_env_int("TOOLLAYER_SNAPSHOT_REFRESH_SECONDS", 60),
        cors_origins=_env_tuple("TOOLLAYER_RUNTIME_CORS_ORIGINS") or ("http://localhost:5173",),
    )


def reset_settings_cache() -> None:
    """Clear the cached settings. Used by tests that change the environment."""
    get_settings.cache_clear()
