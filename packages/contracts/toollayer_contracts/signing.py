"""Producer authentication for contract artifacts.

A content digest and a producer signature answer two different questions, and conflating
them is the mistake this module exists to prevent.

The SHA-256 digest in :mod:`toollayer_contracts.canonical_json` answers *"is this the same
bytes?"*. It gives content addressing, reproducibility, ETag semantics, and detection of
accidental corruption or of a payload edited without its digest being updated. It does not
answer *"who produced this?"* — an attacker who can rewrite the payload can recompute the
digest, because computing SHA-256 requires no secret.

The Ed25519 signature here answers *"was this produced by a holder of a key this consumer
already trusts?"*. Forging it requires the private key, so an attacker who controls the
transport and replaces both the payload and its digest still cannot produce a signature that
verifies against the consumer's configured public key.

Neither replaces transport security. TLS protects confidentiality and authenticates the
*service*; the signature authenticates the *artifact* and survives being cached, mirrored, or
handed through an intermediary. A deployment needs both.

Key material handling
---------------------

Private keys enter only through configuration and never appear in a document, a response, a
log line, or a repository file. :class:`SigningKey` therefore refuses to render itself, and
the loaders below raise without echoing the value they failed to parse.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from toollayer_contracts.canonical_json import canonical_bytes

__all__ = [
    "EMPTY_KEY_RING",
    "SIGNATURE_ALGORITHM",
    "SIGNATURE_EXCLUDED_FIELD",
    "InvalidKeyMaterial",
    "SignatureVerificationError",
    "SigningKey",
    "TrustedKeyRing",
    "VerifyingKey",
    "b64u_decode",
    "b64u_encode",
    "generate_signing_key",
    "sign_document",
    "signing_input",
    "verify_document",
]

#: The only algorithm this contract version defines. An unrecognized value is refused rather
#: than negotiated: algorithm agility that accepts whatever the document names is how
#: signature schemes get downgraded to "none".
SIGNATURE_ALGORITHM: Final = "ed25519"

#: The document field carrying the signature. Excluded from the signing input, because the
#: signature cannot cover itself.
SIGNATURE_EXCLUDED_FIELD: Final = "signature"

_KEY_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$")
_ED25519_KEY_BYTES: Final = 32
_ED25519_SIGNATURE_BYTES: Final = 64


class InvalidKeyMaterial(ValueError):
    """Configured key material is not a usable Ed25519 key."""


class SignatureVerificationError(ValueError):
    """A signature is absent, malformed, unknown, or does not verify."""


def b64u_encode(raw: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_decode(value: str, *, expected_length: int | None = None) -> bytes:
    """Decode unpadded base64url, refusing anything that is not exactly what was expected.

    The length check is part of parsing rather than a later assertion. A 31-byte "public
    key" is not a key that happens to be short; it is input that must not reach the
    cryptographic layer at all.
    """
    if not isinstance(value, str) or not value:
        raise InvalidKeyMaterial("expected a non-empty base64url string")
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise InvalidKeyMaterial("the value is not valid base64url") from None
    if expected_length is not None and len(raw) != expected_length:
        raise InvalidKeyMaterial(f"expected {expected_length} bytes, got {len(raw)}")
    return raw


def _require_key_id(key_id: str) -> str:
    if not isinstance(key_id, str) or not _KEY_ID_PATTERN.match(key_id):
        raise InvalidKeyMaterial(
            "a key id must be 2-64 characters of lowercase alphanumerics, '.', '_' or '-'"
        )
    return key_id


@dataclass(frozen=True, slots=True)
class VerifyingKey:
    """One public key a consumer is willing to accept signatures from."""

    key_id: str
    _public: Ed25519PublicKey

    @classmethod
    def from_encoded(cls, key_id: str, public_key: str) -> VerifyingKey:
        """Build a verifying key from a base64url-encoded raw Ed25519 public key."""
        raw = b64u_decode(public_key, expected_length=_ED25519_KEY_BYTES)
        return cls(key_id=_require_key_id(key_id), _public=Ed25519PublicKey.from_public_bytes(raw))

    def verify(self, message: bytes, signature: bytes) -> bool:
        """Return whether ``signature`` is this key's signature over ``message``."""
        if len(signature) != _ED25519_SIGNATURE_BYTES:
            return False
        try:
            self._public.verify(signature, message)
        except InvalidSignature:
            return False
        return True


@dataclass(frozen=True, slots=True)
class SigningKey:
    """One private key a producer signs with.

    ``__repr__`` and ``__str__`` are overridden so the seed cannot reach a log line, a
    traceback, or a debugger transcript through ordinary formatting.
    """

    key_id: str
    _private: Ed25519PrivateKey

    @classmethod
    def from_encoded(cls, key_id: str, private_key: str) -> SigningKey:
        """Build a signing key from a base64url-encoded 32-byte Ed25519 seed."""
        raw = b64u_decode(private_key, expected_length=_ED25519_KEY_BYTES)
        return cls(
            key_id=_require_key_id(key_id), _private=Ed25519PrivateKey.from_private_bytes(raw)
        )

    def sign(self, message: bytes) -> bytes:
        return self._private.sign(message)

    def public_key(self) -> VerifyingKey:
        return VerifyingKey(key_id=self.key_id, _public=self._private.public_key())

    def encoded_public_key(self) -> str:
        """Return the base64url public key to hand to a consumer."""
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        return b64u_encode(self._private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    def __repr__(self) -> str:  # pragma: no cover - formatting only
        return f"SigningKey(key_id={self.key_id!r}, private=<redacted>)"

    __str__ = __repr__


def generate_signing_key(key_id: str) -> SigningKey:
    """Generate a fresh Ed25519 signing key.

    Used by the demo scripts and by tests so that development signing material is created
    in memory for one run, rather than committed anywhere and mistaken for a credential.
    """
    return SigningKey(key_id=_require_key_id(key_id), _private=Ed25519PrivateKey.generate())


def encoded_private_key(key: SigningKey) -> str:
    """Return the base64url seed for ``key``, for injecting into a child process.

    Deliberately a module-level function rather than a method: extracting a private key is a
    named, greppable operation, not something that happens by attribute access.
    """
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    return b64u_encode(key._private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))


@dataclass(frozen=True, slots=True)
class TrustedKeyRing:
    """The public keys a consumer accepts, indexed by key id.

    A ring rather than a single key so that rotation does not require simultaneous
    redeployment: the new key is added, producers switch to it, and only then is the old key
    removed. Both are trusted during the overlap, and neither is trusted outside it.
    """

    keys: tuple[VerifyingKey, ...] = ()

    @classmethod
    def parse(cls, entries: tuple[str, ...] | list[str]) -> TrustedKeyRing:
        """Build a ring from ``key_id:base64url_public_key`` configuration entries."""
        keys: list[VerifyingKey] = []
        seen: set[str] = set()
        for entry in entries:
            key_id, separator, encoded = entry.partition(":")
            if not separator:
                raise InvalidKeyMaterial(
                    "a trusted key entry must be written as 'key_id:base64url_public_key'"
                )
            key = VerifyingKey.from_encoded(key_id.strip(), encoded.strip())
            if key.key_id in seen:
                raise InvalidKeyMaterial(f"the key id {key.key_id!r} is listed more than once")
            seen.add(key.key_id)
            keys.append(key)
        return cls(keys=tuple(keys))

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(key.key_id for key in self.keys)

    def __bool__(self) -> bool:
        return bool(self.keys)

    def find(self, key_id: str) -> VerifyingKey | None:
        for key in self.keys:
            if key.key_id == key_id:
                return key
        return None


#: The ring a consumer has before it is configured: trusting nobody. Shared as a constant so
#: it can be a dataclass default without constructing one per field definition.
EMPTY_KEY_RING: Final[TrustedKeyRing] = TrustedKeyRing()


def signing_input(document: dict[str, Any]) -> bytes:
    """Return the exact bytes a signature covers.

    The signing input is the canonical serialization of the whole document with the
    ``signature`` field removed — so it covers ``snapshot_id`` and ``snapshot_digest`` as
    well as every connector. Binding the digest into the signed bytes is what stops an
    attacker from pairing a genuine signature with a substituted digest.
    """
    payload = {key: value for key, value in document.items() if key != SIGNATURE_EXCLUDED_FIELD}
    return canonical_bytes(payload)


def sign_document(document: dict[str, Any], key: SigningKey) -> dict[str, str]:
    """Sign ``document`` and return the signature block to attach to it."""
    signature = key.sign(signing_input(document))
    return {
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": key.key_id,
        "value": b64u_encode(signature),
    }


def verify_document(document: dict[str, Any], ring: TrustedKeyRing) -> str:
    """Verify a document's signature against ``ring`` and return the key id that signed it.

    Every failure path raises. There is no return value that means "no signature was
    checked", because a caller that forgot to inspect such a value would silently accept an
    unsigned document — which is precisely the failure this function exists to prevent.
    """
    block = document.get(SIGNATURE_EXCLUDED_FIELD)
    if block is None:
        raise SignatureVerificationError("the document carries no producer signature")
    if not isinstance(block, dict):
        raise SignatureVerificationError("the signature block is not an object")

    algorithm = block.get("algorithm")
    if algorithm != SIGNATURE_ALGORITHM:
        raise SignatureVerificationError(
            f"the signature algorithm is not supported; expected {SIGNATURE_ALGORITHM!r}"
        )

    key_id = block.get("key_id")
    if not isinstance(key_id, str) or not key_id:
        raise SignatureVerificationError("the signature names no key id")

    if not ring:
        raise SignatureVerificationError("this consumer has no trusted signing keys configured")

    key = ring.find(key_id)
    if key is None:
        # Naming the key id is safe — it is not a secret, and an operator debugging a
        # rotation needs to know which key the producer used.
        raise SignatureVerificationError(f"the signing key {key_id!r} is not trusted here")

    value = block.get("value")
    if not isinstance(value, str):
        raise SignatureVerificationError("the signature value is missing")
    try:
        signature = b64u_decode(value, expected_length=_ED25519_SIGNATURE_BYTES)
    except InvalidKeyMaterial:
        raise SignatureVerificationError("the signature value is malformed") from None

    if not key.verify(signing_input(document), signature):
        raise SignatureVerificationError("the signature does not verify for this document")
    return key.key_id
