"""Runtime configuration.

The runtime is deliberately hard to misconfigure into something permissive. Its destination
allowlist starts empty, its loopback and plaintext escape hatches are off, snapshot signature
verification is required, and both bounds on outbound requests have finite defaults. Turning
any of them off requires an explicit environment variable, which means an insecure runtime is
a decision someone made rather than a default they inherited.

Every relaxation is also *visible*: ``/healthz`` reports the destination escape hatches, the
snapshot verification mode, and the caller authentication mode, so a permissive runtime
identifies itself instead of looking the same as a locked-down one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final, get_args

from runtime_service.identity import CallerAuthenticator, CallerAuthMode
from runtime_service.snapshot import SnapshotVerification, VerificationMode
from toollayer_contracts.signing import InvalidKeyMaterial, TrustedKeyRing
from toollayer_policy import DEFAULT_ALLOWED_METHODS, DestinationPolicy, ExecutionLimits

__all__ = ["RuntimeSettings", "get_settings", "reset_settings_cache"]

_DEV_SERVICE_TOKEN: Final = "dev-service-token-change-me"

_VERIFICATION_MODES: Final = frozenset(get_args(VerificationMode))
_CALLER_AUTH_MODES: Final = frozenset(get_args(CallerAuthMode))


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


def _env_mode(name: str, default: str) -> Any:
    """Read a mode name verbatim, leaving validation to ``__post_init__``.

    Not normalized (no lowercasing, no aliasing): a mode is either spelled the way the
    documentation spells it or the process refuses to start. Guessing what
    ``TOOLLAYER_SNAPSHOT_VERIFICATION=Off`` was supposed to mean is how a security control
    ends up disabled by a typo.
    """
    return os.environ.get(name, "").strip() or default


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

    #: ``required`` authenticates the snapshot's producer. ``disabled`` is the offline
    #: demonstration mode and has to be asked for by name.
    snapshot_verification: VerificationMode = "required"
    #: ``key_id:base64url_public_key`` entries this runtime accepts snapshot signatures from.
    #: More than one during a rotation window.
    snapshot_trusted_keys: tuple[str, ...] = ()

    #: ``asserted_header`` believes the caller's headers and is a demonstration mode.
    #: ``verified_token`` requires a signed caller token.
    caller_auth_mode: CallerAuthMode = "asserted_header"
    caller_token_trusted_keys: tuple[str, ...] = ()
    caller_token_issuer: str = ""
    caller_token_audience: str = ""

    def __post_init__(self) -> None:
        if self.snapshot_verification not in _VERIFICATION_MODES:
            raise ConfigurationError(
                "TOOLLAYER_SNAPSHOT_VERIFICATION must be 'required' or 'disabled'"
            )
        if self.caller_auth_mode not in _CALLER_AUTH_MODES:
            raise ConfigurationError(
                "TOOLLAYER_CALLER_AUTH_MODE must be 'asserted_header' or 'verified_token'"
            )
        # Key material is parsed at construction so a typo fails the process at startup
        # rather than failing every request afterwards with a 502 that looks like an
        # upstream problem.
        try:
            snapshot_ring = TrustedKeyRing.parse(self.snapshot_trusted_keys)
            caller_ring = TrustedKeyRing.parse(self.caller_token_trusted_keys)
        except InvalidKeyMaterial as error:
            raise ConfigurationError(f"a configured trusted key is unusable: {error}") from None

        if self.snapshot_verification == "required" and not snapshot_ring:
            raise ConfigurationError(
                "snapshot signature verification is required but no trusted key is configured; "
                "set TOOLLAYER_SNAPSHOT_TRUSTED_KEYS, or set "
                "TOOLLAYER_SNAPSHOT_VERIFICATION=disabled to run the unsigned demonstration"
            )
        if self.caller_auth_mode == "verified_token":
            if not caller_ring:
                raise ConfigurationError(
                    "verified caller authentication needs TOOLLAYER_CALLER_TOKEN_TRUSTED_KEYS"
                )
            if not self.caller_token_issuer or not self.caller_token_audience:
                # An unchecked issuer or audience makes any token minted for any service
                # usable here. Both are mandatory rather than defaulted to "anything".
                raise ConfigurationError(
                    "verified caller authentication needs both "
                    "TOOLLAYER_CALLER_TOKEN_ISSUER and TOOLLAYER_CALLER_TOKEN_AUDIENCE"
                )

    def snapshot_verification_policy(self) -> SnapshotVerification:
        return SnapshotVerification(
            mode=self.snapshot_verification,
            trusted_keys=TrustedKeyRing.parse(self.snapshot_trusted_keys),
        )

    def caller_authenticator(self) -> CallerAuthenticator:
        return CallerAuthenticator(
            mode=self.caller_auth_mode,
            trusted_keys=TrustedKeyRing.parse(self.caller_token_trusted_keys),
            issuer=self.caller_token_issuer,
            audience=self.caller_token_audience,
        )

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

    @property
    def security_posture(self) -> dict[str, object]:
        """Every relaxation this runtime is operating under, for ``/healthz``.

        One place, so that adding an escape hatch without also disclosing it takes a
        deliberate omission rather than a forgotten line.
        """
        return {
            "destination_policy_relaxed": self.relaxed_for_local_development,
            "snapshot_verification": self.snapshot_verification,
            "snapshot_trusted_key_ids": list(
                TrustedKeyRing.parse(self.snapshot_trusted_keys).key_ids
            ),
            "caller_authentication": self.caller_auth_mode,
            # Named unambiguously so nobody reads the demonstration mode as authentication.
            "caller_identity_is_verified": self.caller_auth_mode == "verified_token",
        }


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
        snapshot_verification=_env_mode("TOOLLAYER_SNAPSHOT_VERIFICATION", "required"),
        snapshot_trusted_keys=_env_tuple("TOOLLAYER_SNAPSHOT_TRUSTED_KEYS"),
        caller_auth_mode=_env_mode("TOOLLAYER_CALLER_AUTH_MODE", "asserted_header"),
        caller_token_trusted_keys=_env_tuple("TOOLLAYER_CALLER_TOKEN_TRUSTED_KEYS"),
        caller_token_issuer=os.environ.get("TOOLLAYER_CALLER_TOKEN_ISSUER", "").strip(),
        caller_token_audience=os.environ.get("TOOLLAYER_CALLER_TOKEN_AUDIENCE", "").strip(),
    )


def reset_settings_cache() -> None:
    """Clear the cached settings. Used by tests that change the environment."""
    get_settings.cache_clear()
