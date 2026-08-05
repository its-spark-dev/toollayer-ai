"""Authentication and shared request dependencies.

Two audiences reach this service and they get two different credentials.

An **administrator** authors connectors, reviews proposals, publishes versions, and creates
snapshots. A **runtime service** reads snapshots and does nothing else. Splitting the
credentials means a leaked runtime token cannot publish anything, which is the whole reason
the runtime is not simply given the admin token.

Token comparison is constant-time. Static bearer tokens are a simplification appropriate for
a demonstration, and `docs/threat-model.md` says so plainly rather than implying that a
shared secret is an identity system.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header

from toollayer_contracts.errors import ErrorCode, ToolLayerError
from control_plane.config import ControlPlaneSettings, get_settings

__all__ = ["require_admin", "require_service", "settings_dependency"]

ADMIN_HEADER = "x-toollayer-admin-token"
SERVICE_HEADER = "x-toollayer-service-token"


class Unauthenticated(ToolLayerError):
    code = ErrorCode.UNAUTHENTICATED


def settings_dependency() -> ControlPlaneSettings:
    return get_settings()


SettingsDep = Annotated[ControlPlaneSettings, Depends(settings_dependency)]


def require_admin(
    settings: SettingsDep,
    token: Annotated[str | None, Header(alias=ADMIN_HEADER)] = None,
) -> str:
    """Authenticate an administrator and return a subject for the audit trail."""
    if token is None or not secrets.compare_digest(token, settings.admin_token):
        raise Unauthenticated("a valid administrator token is required")
    return "admin"


def require_service(
    settings: SettingsDep,
    token: Annotated[str | None, Header(alias=SERVICE_HEADER)] = None,
) -> str:
    """Authenticate a runtime service and return a subject for logging."""
    if token is None or not secrets.compare_digest(token, settings.service_token):
        raise Unauthenticated("a valid service token is required")
    return "runtime"


AdminDep = Annotated[str, Depends(require_admin)]
ServiceDep = Annotated[str, Depends(require_service)]
