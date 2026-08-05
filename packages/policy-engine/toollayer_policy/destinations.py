"""Where the runtime is allowed to send a request.

A connector's ``base_url`` is *reviewed configuration*, not authorization. It says where the
administrator intended the tools to go. This module says where this deployment is willing to
go, and it wins. Two independent statements have to agree before a request leaves the
process, so compromising the Control Plane's stored base URL is not by itself enough to make
the runtime call an attacker's host.

The controls here are the ones that matter for a system where the *arguments* come from a
language model and the *destination* comes from a stored artifact:

* **Default deny.** An empty allowlist permits nothing. There is no wildcard.
* **Exact origin matching.** Scheme, host, and port must all match an allowlisted origin.
  Suffix matching (``endswith(".example.org")``) is not used, because
  ``notexample.org.attacker.test`` passes that test.
* **Address-family checks after resolution.** A hostname is resolved and every returned
  address is checked, so a public name that resolves to ``169.254.169.254`` is refused. This
  is the SSRF case that a URL-shaped allowlist alone does not catch.
* **No redirects.** A 3xx is a failure, not a hop. Following one would move the request to a
  destination that was never checked.

Loopback is refused by default and can be enabled only by explicit configuration, because
the local demo genuinely does call ``localhost``. That flag is off in the production-shaped
configuration and a test proves loopback is refused when it is off.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from toollayer_contracts.errors import ErrorCode, PolicyDenied

__all__ = [
    "DEFAULT_ALLOWED_METHODS",
    "DestinationPolicy",
    "ResolvedDestination",
    "SystemDnsResolver",
    "normalize_origin",
]

DEFAULT_ALLOWED_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE"}
)

_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}


class DnsResolver:
    """Resolve a hostname to addresses. Injected so tests can be hermetic."""

    def resolve(self, host: str, port: int) -> tuple[str, ...]:  # pragma: no cover - interface
        raise NotImplementedError


class SystemDnsResolver(DnsResolver):
    """Resolve through the operating system resolver."""

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError:
            raise PolicyDenied(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "the destination host could not be resolved",
            ) from None
        addresses: list[str] = []
        for info in infos:
            # getaddrinfo returns sockaddr tuples whose first element is the address for
            # both IPv4 and IPv6; anything else is not something this policy can check.
            address = info[4][0]
            if isinstance(address, str) and address not in addresses:
                addresses.append(address)
        if not addresses:
            raise PolicyDenied(
                ErrorCode.UPSTREAM_UNAVAILABLE, "the destination host resolved to no address"
            )
        return tuple(addresses)


def normalize_origin(value: str) -> str:
    """Reduce a URL to a comparable ``scheme://host:port`` origin.

    The port is always made explicit so that ``https://api.example.org`` and
    ``https://api.example.org:443`` compare equal, and the host is lowercased so that
    ``API.Example.org`` does not slip past an allowlist entry written in lowercase.
    """
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError("an origin must use http or https")
    if not parsed.hostname:
        raise ValueError("an origin must name a host")
    if parsed.username or parsed.password:
        raise ValueError("an origin must not contain userinfo")
    port = parsed.port or _DEFAULT_PORTS[scheme]
    return f"{scheme}://{parsed.hostname.lower()}:{port}"


@dataclass(frozen=True, slots=True)
class ResolvedDestination:
    """A destination that has passed every check and may be contacted."""

    origin: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    """The set of destinations this deployment is willing to contact."""

    allowed_origins: frozenset[str] = frozenset()
    allowed_methods: frozenset[str] = DEFAULT_ALLOWED_METHODS
    allow_plaintext_http: bool = False
    allow_loopback: bool = False
    allow_private_addresses: bool = False

    @classmethod
    def from_origins(
        cls,
        origins: tuple[str, ...] | list[str],
        *,
        allowed_methods: frozenset[str] | None = None,
        allow_plaintext_http: bool = False,
        allow_loopback: bool = False,
        allow_private_addresses: bool = False,
    ) -> DestinationPolicy:
        """Build a policy from configured origin strings, normalizing each one."""
        normalized: set[str] = set()
        for origin in origins:
            try:
                normalized.add(normalize_origin(origin))
            except ValueError as error:
                raise ValueError(f"invalid allowlist entry: {error}") from None
        return cls(
            allowed_origins=frozenset(normalized),
            allowed_methods=allowed_methods or DEFAULT_ALLOWED_METHODS,
            allow_plaintext_http=allow_plaintext_http,
            allow_loopback=allow_loopback,
            allow_private_addresses=allow_private_addresses,
        )

    def check_method(self, method: str) -> None:
        """Refuse a method this deployment does not permit."""
        if method.upper() not in self.allowed_methods:
            raise PolicyDenied(
                ErrorCode.METHOD_NOT_ALLOWED,
                f"the {method.upper()} method is not permitted by this deployment",
            )

    def check(self, url: str, *, resolver: DnsResolver | None = None) -> ResolvedDestination:
        """Authorize one destination, or raise.

        The order matters. Cheap structural checks run first so a malformed or obviously
        disallowed URL never causes a DNS lookup, which is itself an outbound side effect an
        attacker could use to probe.
        """
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise PolicyDenied(
                ErrorCode.DESTINATION_NOT_ALLOWED, "the destination URL is not parseable"
            ) from None

        scheme = parsed.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            raise PolicyDenied(
                ErrorCode.DESTINATION_NOT_ALLOWED, "the destination must use http or https"
            )
        if scheme == "http" and not self.allow_plaintext_http:
            raise PolicyDenied(
                ErrorCode.DESTINATION_NOT_ALLOWED,
                "plaintext http destinations are disabled for this deployment",
            )
        if parsed.username or parsed.password:
            raise PolicyDenied(
                ErrorCode.DESTINATION_NOT_ALLOWED,
                "the destination URL must not contain credentials",
            )
        host = parsed.hostname
        if not host:
            raise PolicyDenied(
                ErrorCode.DESTINATION_NOT_ALLOWED, "the destination URL must name a host"
            )

        port = parsed.port or _DEFAULT_PORTS[scheme]
        origin = f"{scheme}://{host.lower()}:{port}"
        if not self.allowed_origins:
            raise PolicyDenied(
                ErrorCode.DESTINATION_NOT_ALLOWED,
                "this deployment has no allowlisted destinations",
            )
        if origin not in self.allowed_origins:
            raise PolicyDenied(
                ErrorCode.DESTINATION_NOT_ALLOWED,
                "the destination origin is not on this deployment's allowlist",
            )

        addresses = self._resolved_addresses(host, port, resolver)
        for address in addresses:
            self._check_address(address)

        return ResolvedDestination(
            origin=origin,
            scheme=scheme,
            host=host.lower(),
            port=port,
            addresses=addresses,
        )

    @staticmethod
    def _resolved_addresses(host: str, port: int, resolver: DnsResolver | None) -> tuple[str, ...]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return (str(literal),)
        return (resolver or SystemDnsResolver()).resolve(host, port)

    def _check_address(self, address: str) -> None:
        """Refuse an address family this deployment must not reach.

        Every resolved address is checked, not just the first. A name that returns one
        public and one link-local address is refused, because which one a connection
        actually uses is not something this layer controls.
        """
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise PolicyDenied(
                ErrorCode.PRIVATE_ADDRESS_BLOCKED,
                "the destination resolved to an address that could not be parsed",
            ) from None

        if parsed.is_loopback:
            if not self.allow_loopback:
                raise PolicyDenied(
                    ErrorCode.PRIVATE_ADDRESS_BLOCKED,
                    "the destination resolves to a loopback address",
                )
            return

        if parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified:
            raise PolicyDenied(
                ErrorCode.PRIVATE_ADDRESS_BLOCKED,
                "the destination resolves to a reserved address",
            )
        if parsed.is_link_local:
            # Link-local covers 169.254.0.0/16, which is where cloud instance metadata
            # services live. This is refused unconditionally: there is no configuration
            # that makes reaching it from a tool call correct.
            raise PolicyDenied(
                ErrorCode.PRIVATE_ADDRESS_BLOCKED,
                "the destination resolves to a link-local address",
            )
        # The final check is "is this globally routable", not "is this in RFC 1918". Framing
        # it positively catches carrier-grade NAT, the documentation ranges, and anything
        # else a future address allocation makes non-routable, without this module needing
        # to be taught about each one.
        if not parsed.is_global and not self.allow_private_addresses:
            raise PolicyDenied(
                ErrorCode.PRIVATE_ADDRESS_BLOCKED,
                "the destination resolves to an address that is not globally routable",
            )
