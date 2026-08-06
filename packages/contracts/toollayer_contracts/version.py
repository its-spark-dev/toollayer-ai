"""The version of the ToolLayer contract family.

Every document that crosses a service boundary carries ``contract_version``. The Control
Plane stamps it when it publishes; the Runtime checks it when it loads. The two services
agree on a *major* version, so a consumer can accept additive minor changes without a
coordinated deployment but must refuse a document from a different major line.

This is a project-defined contract. It is not an industry standard, and no compatibility is
claimed with anything outside this repository.
"""

from __future__ import annotations

import re
from typing import Final, NamedTuple

CONTRACT_VERSION: Final = "1.1.0"
"""The contract version this build produces.

1.1.0 added the optional ``signature`` block to the deployment snapshot. The field is
optional, so a 1.0.0 document still validates against this build. The snapshot schema is
closed (``additionalProperties: false``), so the reverse is not true: a 1.0.0 consumer
refuses a signed document. Declaring the minor bump makes that refusal say
``unsupported_contract_version`` rather than surfacing as an opaque schema violation.
"""

_SEMVER: Final = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ContractVersion(NamedTuple):
    """A parsed Semantic Versioning 2.0.0 value without build metadata."""

    major: int
    minor: int
    patch: int
    prerelease: str | None = None

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{self.prerelease}" if self.prerelease else base


class IncompatibleContractVersionError(ValueError):
    """A document declares a contract version this build cannot safely consume."""


def parse_version(value: object) -> ContractVersion:
    """Parse a Semantic Versioning string, rejecting anything else.

    The rejected value is never echoed back, so a malformed version in an untrusted
    document cannot become part of an error message that is logged or displayed.
    """
    if not isinstance(value, str):
        raise IncompatibleContractVersionError("version must be a string")
    match = _SEMVER.match(value)
    if match is None:
        raise IncompatibleContractVersionError("version is not a Semantic Versioning value")
    return ContractVersion(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=match.group("prerelease"),
    )


def is_supported(value: object) -> bool:
    """Return whether this build can consume a document declaring ``value``."""
    try:
        require_supported(value)
    except IncompatibleContractVersionError:
        return False
    return True


def require_supported(value: object) -> ContractVersion:
    """Accept a document version, or raise.

    The rule is deliberately conservative: same major line, and not newer in minor than
    this build. A consumer that has not been taught a newer minor version may be missing a
    field the producer considers meaningful, and silently ignoring it is how two services
    drift apart without anyone noticing.
    """
    declared = parse_version(value)
    supported = parse_version(CONTRACT_VERSION)
    if declared.major != supported.major:
        raise IncompatibleContractVersionError(
            f"contract major version {declared.major} is not supported by this build "
            f"(expected {supported.major})"
        )
    if declared.minor > supported.minor:
        raise IncompatibleContractVersionError(
            "contract minor version is newer than this build understands"
        )
    return declared


def compare_precedence(left: ContractVersion, right: ContractVersion) -> int:
    """Order two versions, treating any prerelease as lower than its release.

    Returns a negative number when ``left`` precedes ``right``, zero when they are equal,
    and a positive number otherwise. Prerelease identifiers are compared as a whole rather
    than segment by segment: the Control Plane only needs "is this newer", and full
    Semantic Versioning prerelease ordering would be precision this project never uses.
    """
    left_core = (left.major, left.minor, left.patch)
    right_core = (right.major, right.minor, right.patch)
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    if left.prerelease == right.prerelease:
        return 0
    if left.prerelease is None:
        return 1
    if right.prerelease is None:
        return -1
    return -1 if left.prerelease < right.prerelease else 1
