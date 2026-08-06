"""How the runtime decides who is calling.

Two modes, named for what they actually do rather than for what they resemble.

``asserted_header`` reads ``x-toollayer-caller`` and ``x-toollayer-roles`` and believes them.
That is **not authentication**. It is a demonstration convenience for a local run, and for a
deployment where a trusted gateway has already authenticated the user and is the only thing
that can reach the runtime. Anything that can open a socket to a runtime in this mode can
claim any role, so the mode is reported by ``/healthz`` rather than left to be discovered.

``verified_token`` requires a signed caller token and derives the subject and roles from
claims it has checked: signature, issuer, audience, and expiry. Assertion headers are ignored
entirely in this mode — accepting them "as a fallback" would mean the verified path could be
skipped by omitting the token, which is the whole attack.

The token format is a JWS compact serialization with ``alg: EdDSA`` over Ed25519 — RFC 8037,
the same shape an identity provider would issue. It is verified offline against a configured
public key, so the tests and the demo need no network and no identity provider. Integrating a
real one is a matter of populating the key ring from a JWKS endpoint; that is deliberately
out of scope here and is not implied to exist.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from toollayer_contracts.errors import ErrorCode, ToolLayerError
from toollayer_contracts.signing import (
    EMPTY_KEY_RING,
    InvalidKeyMaterial,
    SigningKey,
    TrustedKeyRing,
    b64u_decode,
    b64u_encode,
)
from toollayer_policy import CallerIdentity

__all__ = [
    "CallerAuthMode",
    "CallerAuthenticator",
    "InvalidCallerToken",
    "issue_caller_token",
]

logger = logging.getLogger("toollayer.runtime.identity")

CallerAuthMode = Literal["asserted_header", "verified_token"]

#: Tolerance for clock skew between the issuer and this runtime, in seconds. Small and fixed:
#: a generous window is a way to accept expired tokens without admitting to it.
_CLOCK_SKEW_SECONDS = 30

#: A token longer than this is refused before it is parsed. Nothing legitimate approaches it,
#: and parsing is the first place attacker-controlled length turns into work.
_MAX_TOKEN_BYTES = 8192


class InvalidCallerToken(ToolLayerError):
    """The caller token is absent, malformed, expired, or not for this audience."""

    code = ErrorCode.UNAUTHENTICATED


@dataclass(frozen=True, slots=True)
class CallerAuthenticator:
    """Turns whatever the request carried into a caller identity, or refuses.

    ``verified_token`` mode fails closed on every path: no token, wrong shape, unknown key,
    bad signature, wrong issuer, wrong audience, missing or past expiry. There is no branch
    that returns an identity without having checked a signature.
    """

    mode: CallerAuthMode = "asserted_header"
    trusted_keys: TrustedKeyRing = EMPTY_KEY_RING
    issuer: str = ""
    audience: str = ""

    @property
    def verifies_identity(self) -> bool:
        return self.mode == "verified_token"

    def identify(
        self,
        *,
        bearer_token: str | None,
        asserted_subject: str | None,
        asserted_roles: str | None,
    ) -> CallerIdentity | None:
        if self.mode == "verified_token":
            if asserted_subject is not None or asserted_roles is not None:
                # Refused rather than ignored. Silently dropping the headers would let a
                # caller believe it had escalated; saying so makes the boundary legible.
                raise InvalidCallerToken(
                    "this runtime verifies caller identity; asserted identity headers are "
                    "not accepted"
                )
            return self._from_token(bearer_token)

        if asserted_subject is None and asserted_roles is None:
            return None
        roles = tuple(entry.strip() for entry in (asserted_roles or "").split(",") if entry.strip())
        return CallerIdentity.of(asserted_subject or "anonymous", roles)

    def _from_token(self, bearer_token: str | None) -> CallerIdentity:
        if not bearer_token:
            raise InvalidCallerToken("a caller token is required")
        claims = self._verified_claims(bearer_token)

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise InvalidCallerToken("the caller token names no subject")

        raw_roles = claims.get("roles", [])
        if not isinstance(raw_roles, list) or not all(isinstance(role, str) for role in raw_roles):
            raise InvalidCallerToken("the caller token's roles claim is not a list of strings")
        return CallerIdentity.of(subject, tuple(role.strip() for role in raw_roles if role.strip()))

    def _verified_claims(self, token: str) -> dict[str, Any]:
        """Verify the token and return its claims, or raise.

        Deliberately verbose about ordering: the signature is checked before any claim is
        read, so an unsigned or forged token never gets to influence a decision through its
        payload — not even through an error message that reveals what it contained.
        """
        if len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
            raise InvalidCallerToken("the caller token is too large")

        parts = token.split(".")
        if len(parts) != 3:
            raise InvalidCallerToken("the caller token is not a compact JWS")
        encoded_header, encoded_payload, encoded_signature = parts

        try:
            header = json.loads(b64u_decode(encoded_header))
        except (InvalidKeyMaterial, UnicodeDecodeError, ValueError):
            raise InvalidCallerToken("the caller token is malformed") from None
        if not isinstance(header, dict):
            raise InvalidCallerToken("the caller token header is not an object")

        # The algorithm is checked before the signature bytes are even decoded. An
        # ``alg: none`` token carries an empty signature segment, so decoding first would
        # report it as "malformed" and hide the fact that a downgrade was attempted.
        if header.get("alg") != "EdDSA":
            raise InvalidCallerToken("the caller token uses an unsupported signature algorithm")

        key_id = header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            raise InvalidCallerToken("the caller token names no signing key")
        key = self.trusted_keys.find(key_id)
        if key is None:
            raise InvalidCallerToken("the caller token was signed by a key that is not trusted")

        try:
            signature = b64u_decode(encoded_signature)
        except (InvalidKeyMaterial, UnicodeDecodeError, ValueError):
            raise InvalidCallerToken("the caller token signature is malformed") from None
        if not key.verify(f"{encoded_header}.{encoded_payload}".encode("ascii"), signature):
            raise InvalidCallerToken("the caller token signature does not verify")

        try:
            claims = json.loads(b64u_decode(encoded_payload))
        except (InvalidKeyMaterial, UnicodeDecodeError, ValueError):
            raise InvalidCallerToken("the caller token payload is malformed") from None
        if not isinstance(claims, dict):
            raise InvalidCallerToken("the caller token payload is not an object")

        if self.issuer and claims.get("iss") != self.issuer:
            raise InvalidCallerToken("the caller token was issued by an unexpected issuer")
        if self.audience and not _audience_matches(claims.get("aud"), self.audience):
            raise InvalidCallerToken("the caller token is not intended for this runtime")

        now = time.time()
        expiry = claims.get("exp")
        if not isinstance(expiry, (int, float)) or isinstance(expiry, bool):
            # A token with no expiry is a permanent credential. Requiring the claim means an
            # issuer cannot produce one by omission.
            raise InvalidCallerToken("the caller token does not expire")
        if now > float(expiry) + _CLOCK_SKEW_SECONDS:
            raise InvalidCallerToken("the caller token has expired")

        not_before = claims.get("nbf")
        if (
            isinstance(not_before, (int, float))
            and not isinstance(not_before, bool)
            and now + _CLOCK_SKEW_SECONDS < float(not_before)
        ):
            raise InvalidCallerToken("the caller token is not valid yet")

        return claims


def _audience_matches(claim: Any, expected: str) -> bool:
    """Accept the single-string and array forms JWT defines, and nothing else."""
    if isinstance(claim, str):
        return claim == expected
    if isinstance(claim, list):
        return any(isinstance(entry, str) and entry == expected for entry in claim)
    return False


def issue_caller_token(
    key: SigningKey,
    *,
    subject: str,
    roles: tuple[str, ...] | list[str],
    issuer: str,
    audience: str,
    issued_at: float,
    lifetime_seconds: int = 300,
) -> str:
    """Mint a caller token.

    This is the *host application's* job in a real deployment — the runtime verifies, it does
    not issue. It lives here so the demonstration and the tests can exercise the verified path
    end to end without an identity provider, and so the exact token shape the runtime accepts
    is written down in executable form rather than in prose.

    ``issued_at`` is a parameter rather than a call to the clock so that a test can produce an
    already-expired token without sleeping.
    """
    header = {"alg": "EdDSA", "typ": "JWT", "kid": key.key_id}
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "roles": list(roles),
        "iat": int(issued_at),
        "exp": int(issued_at) + lifetime_seconds,
    }
    encoded_header = b64u_encode(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    encoded_claims = b64u_encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    return f"{encoded_header}.{encoded_claims}.{b64u_encode(key.sign(signing_input))}"
