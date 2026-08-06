"""Producer authentication for deployment snapshots.

These tests exist because a content digest and a producer signature defend against different
adversaries, and the difference is easy to state and easy to get wrong.

The digest defends against *accident*: corruption in storage or transit, and a payload edited
without its digest updated. ``test_content_changed_but_the_original_digest_kept_is_refused``
is that case.

The signature defends against an *active attacker*: someone who can rewrite the response body
and is therefore free to recompute the digest too.
``test_content_and_digest_both_replaced_is_still_refused`` is that case — and it is the one
the digest alone cannot pass, which is the whole reason signing was added.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import pytest

from runtime_service.snapshot import (
    SnapshotIntegrityError,
    SnapshotSignatureError,
    SnapshotVerification,
    load_snapshot_document,
)
from toollayer_contracts import (
    SNAPSHOT_DIGEST_EXCLUDED,
    ErrorCode,
    TrustedKeyRing,
    content_digest,
    generate_signing_key,
    sign_document,
)
from toollayer_contracts.errors import ToolLayerError

pytestmark = pytest.mark.security


def _resign(document: dict[str, Any], key: Any) -> dict[str, Any]:
    """Recompute the digest and signature the way the Control Plane does."""
    unsigned = {name: value for name, value in document.items() if name != "signature"}
    unsigned["snapshot_digest"] = content_digest(unsigned, exclude=SNAPSHOT_DIGEST_EXCLUDED)
    unsigned["signature"] = sign_document(unsigned, key)
    return unsigned


def _trusting(*keys: Any) -> SnapshotVerification:
    return SnapshotVerification(
        mode="required",
        trusted_keys=TrustedKeyRing.parse(
            [f"{key.key_id}:{key.encoded_public_key()}" for key in keys]
        ),
    )


class TestSignedSnapshotsAreAccepted:
    def test_a_snapshot_signed_by_a_trusted_key_loads(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        loaded = load_snapshot_document(published_snapshot, verification=snapshot_verification)
        assert loaded.signed
        assert loaded.signing_key_id == "test-snapshot-key"

    def test_the_control_plane_signs_what_it_publishes(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        signature = published_snapshot["signature"]
        assert signature["algorithm"] == "ed25519"
        assert signature["key_id"] == "test-snapshot-key"

    def test_the_signature_covers_the_digest_and_the_identifier(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        """Both derived fields are inside the signed bytes, not alongside them.

        If the signature covered only the payload, a genuine signature could be lifted onto a
        document whose ``snapshot_digest`` had been swapped for one describing other content.
        """
        from toollayer_contracts import signing_input

        covered = signing_input(published_snapshot)
        assert published_snapshot["snapshot_digest"].encode() in covered
        assert published_snapshot["snapshot_id"].encode() in covered
        assert b'"signature"' not in covered


class TestTampering:
    def test_content_changed_but_the_original_digest_kept_is_refused(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        """The accident case, and the weakest attacker. Caught by the digest alone."""
        tampered = copy.deepcopy(published_snapshot)
        tampered["connectors"][0]["runtime"]["base_url"] = "https://attacker.test"

        with pytest.raises(SnapshotIntegrityError) as caught:
            load_snapshot_document(tampered, verification=snapshot_verification)
        assert caught.value.code == ErrorCode.SNAPSHOT_INTEGRITY_FAILED

    def test_content_and_digest_both_replaced_is_still_refused(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        """The active attacker. This is what a plain digest cannot stop.

        The payload is rewritten *and* the digest recomputed over it, so the document is
        internally consistent and every hash check passes. It fails on the signature, which
        the attacker cannot reproduce without the Control Plane's private key.
        """
        forged = copy.deepcopy(published_snapshot)
        forged["connectors"][0]["runtime"]["base_url"] = "https://attacker.test"
        unsigned = {name: value for name, value in forged.items() if name != "signature"}
        unsigned["snapshot_digest"] = content_digest(unsigned, exclude=SNAPSHOT_DIGEST_EXCLUDED)
        # The attacker keeps the genuine signature, having no key to make a new one.
        forged = {**unsigned, "signature": published_snapshot["signature"]}

        from toollayer_contracts import verify_digest

        assert verify_digest(forged, forged["snapshot_digest"], exclude=SNAPSHOT_DIGEST_EXCLUDED), (
            "the forgery is digest-consistent; only the signature distinguishes it"
        )

        with pytest.raises(SnapshotSignatureError) as caught:
            load_snapshot_document(forged, verification=snapshot_verification)
        assert caught.value.code == ErrorCode.SNAPSHOT_SIGNATURE_INVALID

    def test_a_policy_widened_by_an_attacker_is_refused(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        """The highest-value tamper target: turning a restricted tool into a public one."""
        forged = copy.deepcopy(published_snapshot)
        for tool in forged["connectors"][0]["tools"]:
            tool["policy"]["access"] = {"access_mode": "public", "allowed_roles": []}
            tool["policy"]["requires_confirmation"] = False
        unsigned = {name: value for name, value in forged.items() if name != "signature"}
        unsigned["snapshot_digest"] = content_digest(unsigned, exclude=SNAPSHOT_DIGEST_EXCLUDED)
        forged = {**unsigned, "signature": published_snapshot["signature"]}

        with pytest.raises(SnapshotSignatureError):
            load_snapshot_document(forged, verification=snapshot_verification)


class TestSignatureRejection:
    def test_a_missing_signature_is_refused_in_secure_mode(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        unsigned = {
            name: value for name, value in published_snapshot.items() if name != "signature"
        }
        with pytest.raises(SnapshotSignatureError, match="no producer signature"):
            load_snapshot_document(unsigned, verification=snapshot_verification)

    def test_a_malformed_signature_value_is_refused(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        broken = copy.deepcopy(published_snapshot)
        # Same length and alphabet as a real signature, so it passes the schema and fails
        # only where it should: at the cryptographic check.
        broken["signature"]["value"] = "A" * 86
        with pytest.raises(SnapshotSignatureError, match="does not verify"):
            load_snapshot_document(broken, verification=snapshot_verification)

    def test_a_signature_that_is_not_an_object_is_refused(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        broken = {**published_snapshot, "signature": "not-an-object"}
        with pytest.raises(Exception) as caught:
            load_snapshot_document(broken, verification=snapshot_verification)
        assert caught.value.__class__.__name__ in {
            "SnapshotSignatureError",
            "ContractViolationError",
        }

    def test_an_unsupported_algorithm_is_refused(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        """No negotiation. A document naming another algorithm does not get to pick one."""
        broken = copy.deepcopy(published_snapshot)
        broken["signature"]["algorithm"] = "hmac-sha256"
        with pytest.raises(Exception) as caught:
            load_snapshot_document(broken, verification=snapshot_verification)
        # The schema pins `algorithm` to a constant, so this is refused before the signature
        # layer is reached. Asserted at the load boundary rather than on a specific type,
        # because "which check caught it" is an implementation detail and "it was refused"
        # is the property.
        assert caught.value.__class__.__name__ in {
            "SnapshotSignatureError",
            "ContractViolationError",
        }

    def test_an_unsupported_algorithm_is_refused_by_the_signature_layer_directly(self) -> None:
        from toollayer_contracts import SignatureVerificationError, verify_document

        key = generate_signing_key("some-key")
        document = {"a": 1}
        block = sign_document(document, key)
        document["signature"] = {**block, "algorithm": "rsa-pss"}  # type: ignore[assignment]
        with pytest.raises(SignatureVerificationError, match="not supported"):
            verify_document(
                document, TrustedKeyRing.parse([f"some-key:{key.encoded_public_key()}"])
            )

    def test_an_unknown_key_id_is_refused(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        broken = copy.deepcopy(published_snapshot)
        broken["signature"]["key_id"] = "some-other-key"
        with pytest.raises(SnapshotSignatureError, match="not trusted"):
            load_snapshot_document(broken, verification=snapshot_verification)

    def test_a_valid_signature_from_an_untrusted_key_is_refused(
        self,
        published_snapshot: dict[str, Any],
        snapshot_verification: SnapshotVerification,
        untrusted_signing_key: Any,
    ) -> None:
        """A correct signature is not the same as an acceptable one.

        This document is cryptographically flawless — it just was not produced by anyone this
        runtime was told to trust.
        """
        forged = _resign(copy.deepcopy(published_snapshot), untrusted_signing_key)
        with pytest.raises(SnapshotSignatureError, match="not trusted"):
            load_snapshot_document(forged, verification=snapshot_verification)

    def test_required_mode_with_no_trusted_key_refuses_everything(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        """The empty ring fails closed rather than degrading to 'accept anything'."""
        with pytest.raises(SnapshotSignatureError, match="no trusted signing keys"):
            load_snapshot_document(
                published_snapshot, verification=SnapshotVerification(mode="required")
            )


class TestKeyRotation:
    def test_both_keys_verify_during_a_rotation_window(
        self, published_snapshot: dict[str, Any], signing_key: Any
    ) -> None:
        """Old and new keys are both trusted while a rotation is in progress.

        Without an overlap, rotating would mean the producer and every consumer had to change
        key in the same instant — so in practice nobody would rotate.
        """
        incoming = generate_signing_key("test-snapshot-key-2")
        ring = _trusting(signing_key, incoming)

        signed_by_old = load_snapshot_document(published_snapshot, verification=ring)
        signed_by_new = load_snapshot_document(
            _resign(copy.deepcopy(published_snapshot), incoming), verification=ring
        )
        assert signed_by_old.signing_key_id == "test-snapshot-key"
        assert signed_by_new.signing_key_id == "test-snapshot-key-2"

    def test_a_retired_key_stops_verifying_once_it_leaves_the_ring(
        self, published_snapshot: dict[str, Any], signing_key: Any
    ) -> None:
        incoming = generate_signing_key("test-snapshot-key-2")
        rotated = _resign(copy.deepcopy(published_snapshot), incoming)

        # After the window closes only the new key remains trusted.
        with pytest.raises(SnapshotSignatureError, match="not trusted"):
            load_snapshot_document(published_snapshot, verification=_trusting(incoming))
        assert load_snapshot_document(rotated, verification=_trusting(incoming)).signed


class TestUnsignedDemonstrationMode:
    def test_unsigned_mode_accepts_an_unsigned_snapshot_and_says_so(
        self, published_snapshot: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        unsigned = {
            name: value for name, value in published_snapshot.items() if name != "signature"
        }
        with caplog.at_level(logging.WARNING, logger="toollayer.runtime.snapshot"):
            loaded = load_snapshot_document(
                unsigned, verification=SnapshotVerification(mode="disabled")
            )
        assert loaded.signed is False
        assert loaded.signing_key_id is None
        # Explicit and observable: an unauthenticated artifact is never accepted silently.
        assert "unsigned deployment snapshot" in caplog.text

    def test_unsigned_mode_still_refuses_a_signature_that_does_not_verify(
        self, published_snapshot: dict[str, Any], signing_key: Any
    ) -> None:
        """Leniency about a *missing* signature is not leniency about a *wrong* one."""
        broken = copy.deepcopy(published_snapshot)
        broken["signature"]["value"] = "A" * 86
        with pytest.raises(SnapshotSignatureError):
            load_snapshot_document(
                broken,
                verification=SnapshotVerification(
                    mode="disabled",
                    trusted_keys=TrustedKeyRing.parse(
                        [f"{signing_key.key_id}:{signing_key.encoded_public_key()}"]
                    ),
                ),
            )


class TestContractCompatibilityWithUnsignedV1Snapshots:
    """What happens to a snapshot published before signing existed.

    The contract moved 1.0.0 -> 1.1.0 when `signature` was added. The field is optional, so an
    older document still *parses*; whether it is *served* is a separate decision made by the
    verification policy. Keeping those two answers distinct is the point — a version check and
    a security policy failing for the same reason would make either one impossible to reason
    about.
    """

    def _as_v1_unsigned(self, published: dict[str, Any]) -> dict[str, Any]:
        """The same snapshot as a 1.0.0 Control Plane would have produced it."""
        document = {name: value for name, value in published.items() if name != "signature"}
        document["contract_version"] = "1.0.0"
        document["snapshot_digest"] = content_digest(document, exclude=SNAPSHOT_DIGEST_EXCLUDED)
        return document

    def test_an_older_minor_version_is_still_a_supported_contract(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        from toollayer_contracts.version import require_supported

        assert str(require_supported("1.0.0")) == "1.0.0"

    def test_a_v1_unsigned_snapshot_is_refused_in_the_default_secure_mode(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        """Refused for the *signature*, not for the version — the message says which."""
        with pytest.raises(SnapshotSignatureError, match="no producer signature"):
            load_snapshot_document(
                self._as_v1_unsigned(published_snapshot), verification=snapshot_verification
            )

    def test_a_v1_unsigned_snapshot_still_loads_in_explicit_unsigned_mode(
        self, published_snapshot: dict[str, Any]
    ) -> None:
        loaded = load_snapshot_document(
            self._as_v1_unsigned(published_snapshot),
            verification=SnapshotVerification(mode="disabled"),
        )
        assert loaded.signed is False
        assert loaded.snapshot.contract_version == "1.0.0"

    def test_a_newer_minor_than_this_build_understands_is_refused(
        self, published_snapshot: dict[str, Any], snapshot_verification: SnapshotVerification
    ) -> None:
        """The other direction: a 1.0.0 consumer refusing a signed document, in miniature."""
        future = {**published_snapshot, "contract_version": "1.99.0"}
        with pytest.raises(ToolLayerError) as caught:
            load_snapshot_document(future, verification=snapshot_verification)
        assert caught.value.code == ErrorCode.UNSUPPORTED_CONTRACT_VERSION


class TestSigningMaterialNeverLeaks:
    def test_no_admin_response_contains_the_private_key(
        self,
        control_plane: Any,
        published_snapshot: dict[str, Any],
        signing_key: Any,
        admin_headers: dict[str, str],
    ) -> None:
        secret = _seed(signing_key)
        for path in (
            "/healthz",
            "/admin/v1/deployments",
            "/admin/v1/deployments/demo-workspace/snapshots",
            "/admin/v1/connectors",
        ):
            response = control_plane.get(path, headers=admin_headers)
            assert secret not in response.text, path

    def test_the_health_endpoint_reports_signing_without_exposing_the_key(
        self, control_plane: Any, signing_key: Any
    ) -> None:
        body = control_plane.get("/healthz").json()
        assert body["snapshot_signing"] == "enabled"
        assert body["snapshot_signing_key_id"] == signing_key.key_id
        assert _seed(signing_key) not in control_plane.get("/healthz").text

    def test_the_signing_key_does_not_render_itself(self, signing_key: Any) -> None:
        """Formatting a key must not be a way to print it."""
        assert _seed(signing_key) not in repr(signing_key)
        assert _seed(signing_key) not in str(signing_key)
        assert "redacted" in repr(signing_key)

    def test_the_snapshot_document_carries_no_private_material(
        self, published_snapshot: dict[str, Any], signing_key: Any
    ) -> None:
        import json

        assert _seed(signing_key) not in json.dumps(published_snapshot)


def _seed(key: Any) -> str:
    from toollayer_contracts.signing import encoded_private_key

    return encoded_private_key(key)
