"""The caller-identity boundary, in both of its modes.

The distinction these tests pin down is the one the documentation now makes explicitly:
``asserted_header`` is a demonstration convenience in which the runtime *believes* what the
caller says, and ``verified_token`` is authentication. The first is not a weaker version of
the second; it is a different thing, and a deployment must be able to tell which one it is
running from the outside.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from runtime_service.identity import CallerAuthenticator, InvalidCallerToken, issue_caller_token
from toollayer_contracts import ErrorCode, TrustedKeyRing, generate_signing_key

pytestmark = pytest.mark.security

ISSUER = "https://identity.example.org"
AUDIENCE = "toollayer-runtime"


@pytest.fixture()
def caller_key() -> Any:
    """The host application's token-signing key, generated for this test only."""
    return generate_signing_key("caller-key")


@pytest.fixture()
def verifying(caller_key: Any) -> CallerAuthenticator:
    return CallerAuthenticator(
        mode="verified_token",
        trusted_keys=TrustedKeyRing.parse([f"caller-key:{caller_key.encoded_public_key()}"]),
        issuer=ISSUER,
        audience=AUDIENCE,
    )


def _token(key: Any, **overrides: Any) -> str:
    arguments: dict[str, Any] = {
        "subject": "avery@example.org",
        "roles": ("support-agent",),
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "issued_at": time.time(),
        "lifetime_seconds": 300,
    }
    arguments.update(overrides)
    return issue_caller_token(key, **arguments)


class TestAssertedHeaderMode:
    def test_it_believes_the_headers_it_is_given(self) -> None:
        """Characterizing the demonstration mode, so its weakness is on the record.

        Anything that can reach a runtime in this mode can name any role. That is why it is
        called asserted identity and never authentication.
        """
        caller = CallerAuthenticator(mode="asserted_header").identify(
            bearer_token=None,
            asserted_subject="anyone@example.org",
            asserted_roles="support-lead,admin",
        )
        assert caller is not None
        assert caller.subject == "anyone@example.org"
        assert caller.roles == frozenset({"support-lead", "admin"})

    def test_no_headers_means_no_caller(self) -> None:
        assert (
            CallerAuthenticator(mode="asserted_header").identify(
                bearer_token=None, asserted_subject=None, asserted_roles=None
            )
            is None
        )

    def test_blank_and_duplicated_roles_are_normalized(self) -> None:
        caller = CallerAuthenticator(mode="asserted_header").identify(
            bearer_token=None,
            asserted_subject="avery@example.org",
            asserted_roles=" support-agent , , support-agent ,",
        )
        assert caller is not None
        assert caller.roles == frozenset({"support-agent"})


class TestVerifiedTokenMode:
    def test_a_valid_token_yields_the_claimed_subject_and_roles(
        self, verifying: CallerAuthenticator, caller_key: Any
    ) -> None:
        caller = verifying.identify(
            bearer_token=_token(caller_key, roles=("support-lead", "support-agent")),
            asserted_subject=None,
            asserted_roles=None,
        )
        assert caller is not None
        assert caller.subject == "avery@example.org"
        assert caller.roles == frozenset({"support-lead", "support-agent"})

    def test_a_missing_token_is_refused(self, verifying: CallerAuthenticator) -> None:
        with pytest.raises(InvalidCallerToken, match="required"):
            verifying.identify(bearer_token=None, asserted_subject=None, asserted_roles=None)

    def test_assertion_headers_are_refused_rather_than_ignored(
        self, verifying: CallerAuthenticator, caller_key: Any
    ) -> None:
        """The escalation this mode exists to prevent.

        Refusing rather than dropping the headers matters: a caller that believed its
        asserted role took effect would be operating on a false model of its own privileges.
        """
        with pytest.raises(InvalidCallerToken, match="not accepted"):
            verifying.identify(
                bearer_token=_token(caller_key),
                asserted_subject="attacker@example.org",
                asserted_roles="support-lead",
            )

    def test_a_token_signed_by_an_untrusted_key_is_refused(
        self, verifying: CallerAuthenticator
    ) -> None:
        stranger = generate_signing_key("caller-key")  # same key id, different key material
        with pytest.raises(InvalidCallerToken, match="does not verify"):
            verifying.identify(
                bearer_token=_token(stranger), asserted_subject=None, asserted_roles=None
            )

    def test_a_token_naming_an_unknown_key_is_refused(self, verifying: CallerAuthenticator) -> None:
        stranger = generate_signing_key("some-other-key")
        with pytest.raises(InvalidCallerToken, match="not trusted"):
            verifying.identify(
                bearer_token=_token(stranger), asserted_subject=None, asserted_roles=None
            )

    def test_an_expired_token_is_refused(
        self, verifying: CallerAuthenticator, caller_key: Any
    ) -> None:
        expired = _token(caller_key, issued_at=time.time() - 3600, lifetime_seconds=60)
        with pytest.raises(InvalidCallerToken, match="expired"):
            verifying.identify(bearer_token=expired, asserted_subject=None, asserted_roles=None)

    def test_a_token_for_another_audience_is_refused(
        self, verifying: CallerAuthenticator, caller_key: Any
    ) -> None:
        """A token minted for a different service must not be replayable here."""
        with pytest.raises(InvalidCallerToken, match="not intended for this runtime"):
            verifying.identify(
                bearer_token=_token(caller_key, audience="some-other-service"),
                asserted_subject=None,
                asserted_roles=None,
            )

    def test_a_token_from_another_issuer_is_refused(
        self, verifying: CallerAuthenticator, caller_key: Any
    ) -> None:
        with pytest.raises(InvalidCallerToken, match="unexpected issuer"):
            verifying.identify(
                bearer_token=_token(caller_key, issuer="https://attacker.test"),
                asserted_subject=None,
                asserted_roles=None,
            )

    @pytest.mark.parametrize(
        "malformed",
        ["", "not-a-token", "a.b", "a.b.c.d", "...", "€.€.€"],
        ids=["empty", "one-part", "two-parts", "four-parts", "empty-parts", "non-ascii"],
    )
    def test_a_malformed_token_is_refused(
        self, verifying: CallerAuthenticator, malformed: str
    ) -> None:
        with pytest.raises(InvalidCallerToken):
            verifying.identify(bearer_token=malformed, asserted_subject=None, asserted_roles=None)

    def test_the_none_algorithm_is_refused(
        self, verifying: CallerAuthenticator, caller_key: Any
    ) -> None:
        """The classic JWT downgrade. There is no algorithm the document gets to choose."""
        import json

        from toollayer_contracts.signing import b64u_encode

        header = b64u_encode(
            json.dumps({"alg": "none", "typ": "JWT", "kid": "caller-key"}).encode()
        )
        claims = b64u_encode(
            json.dumps(
                {
                    "iss": ISSUER,
                    "aud": AUDIENCE,
                    "sub": "attacker@example.org",
                    "roles": ["support-lead"],
                    "exp": int(time.time()) + 300,
                }
            ).encode()
        )
        with pytest.raises(InvalidCallerToken, match="unsupported signature algorithm"):
            verifying.identify(
                bearer_token=f"{header}.{claims}.", asserted_subject=None, asserted_roles=None
            )

    def test_a_token_without_an_expiry_is_refused(
        self, verifying: CallerAuthenticator, caller_key: Any
    ) -> None:
        """A credential with no expiry is a permanent one. Omission is not a way to get it."""
        import json

        from toollayer_contracts.signing import b64u_encode

        header = b64u_encode(
            json.dumps({"alg": "EdDSA", "typ": "JWT", "kid": "caller-key"}).encode()
        )
        claims = b64u_encode(
            json.dumps(
                {"iss": ISSUER, "aud": AUDIENCE, "sub": "avery@example.org", "roles": []}
            ).encode()
        )
        signature = b64u_encode(caller_key.sign(f"{header}.{claims}".encode("ascii")))
        with pytest.raises(InvalidCallerToken, match="does not expire"):
            verifying.identify(
                bearer_token=f"{header}.{claims}.{signature}",
                asserted_subject=None,
                asserted_roles=None,
            )

    def test_a_tampered_payload_invalidates_the_signature(
        self, verifying: CallerAuthenticator, caller_key: Any
    ) -> None:
        import json

        from toollayer_contracts.signing import b64u_encode

        header, _claims, signature = _token(caller_key).split(".")
        escalated = b64u_encode(
            json.dumps(
                {
                    "iss": ISSUER,
                    "aud": AUDIENCE,
                    "sub": "avery@example.org",
                    "roles": ["support-lead"],
                    "exp": int(time.time()) + 300,
                }
            ).encode()
        )
        with pytest.raises(InvalidCallerToken, match="does not verify"):
            verifying.identify(
                bearer_token=f"{header}.{escalated}.{signature}",
                asserted_subject=None,
                asserted_roles=None,
            )

    def test_an_oversized_token_is_refused_before_parsing(
        self, verifying: CallerAuthenticator
    ) -> None:
        with pytest.raises(InvalidCallerToken, match="too large"):
            verifying.identify(bearer_token="a" * 9000, asserted_subject=None, asserted_roles=None)


class TestModeIsObservable:
    def test_health_reports_asserted_identity_as_unverified(
        self, monkeypatch: pytest.MonkeyPatch, published_snapshot: dict[str, Any]
    ) -> None:
        body = _health(monkeypatch)
        assert body["caller_authentication"] == "asserted_header"
        assert body["caller_identity_is_verified"] is False

    def test_health_reports_verified_identity_when_configured(
        self, monkeypatch: pytest.MonkeyPatch, caller_key: Any
    ) -> None:
        monkeypatch.setenv("TOOLLAYER_CALLER_AUTH_MODE", "verified_token")
        monkeypatch.setenv(
            "TOOLLAYER_CALLER_TOKEN_TRUSTED_KEYS", f"caller-key:{caller_key.encoded_public_key()}"
        )
        monkeypatch.setenv("TOOLLAYER_CALLER_TOKEN_ISSUER", ISSUER)
        monkeypatch.setenv("TOOLLAYER_CALLER_TOKEN_AUDIENCE", AUDIENCE)
        body = _health(monkeypatch)
        assert body["caller_authentication"] == "verified_token"
        assert body["caller_identity_is_verified"] is True

    def test_health_reports_the_snapshot_verification_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = _health(monkeypatch)
        assert body["snapshot_verification"] == "required"
        assert body["snapshot_trusted_key_ids"] == ["test-snapshot-key"]

    def test_health_reports_an_unsigned_runtime_as_unsigned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TOOLLAYER_SNAPSHOT_VERIFICATION", "disabled")
        monkeypatch.delenv("TOOLLAYER_SNAPSHOT_TRUSTED_KEYS", raising=False)
        body = _health(monkeypatch)
        assert body["snapshot_verification"] == "disabled"
        assert body["snapshot_trusted_key_ids"] == []

    def test_no_health_response_contains_a_token_or_key(
        self, monkeypatch: pytest.MonkeyPatch, caller_key: Any
    ) -> None:
        from toollayer_contracts.signing import encoded_private_key

        monkeypatch.setenv("TOOLLAYER_CALLER_AUTH_MODE", "verified_token")
        monkeypatch.setenv(
            "TOOLLAYER_CALLER_TOKEN_TRUSTED_KEYS", f"caller-key:{caller_key.encoded_public_key()}"
        )
        monkeypatch.setenv("TOOLLAYER_CALLER_TOKEN_ISSUER", ISSUER)
        monkeypatch.setenv("TOOLLAYER_CALLER_TOKEN_AUDIENCE", AUDIENCE)
        rendered = _health_text(monkeypatch)
        assert encoded_private_key(caller_key) not in rendered


class TestMisconfigurationFailsClosed:
    def test_required_verification_without_a_key_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from runtime_service.config import ConfigurationError, get_settings, reset_settings_cache

        monkeypatch.delenv("TOOLLAYER_SNAPSHOT_TRUSTED_KEYS", raising=False)
        reset_settings_cache()
        with pytest.raises(ConfigurationError, match="no trusted key"):
            get_settings()
        reset_settings_cache()

    def test_verified_caller_mode_without_an_audience_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch, caller_key: Any
    ) -> None:
        from runtime_service.config import ConfigurationError, get_settings, reset_settings_cache

        monkeypatch.setenv("TOOLLAYER_CALLER_AUTH_MODE", "verified_token")
        monkeypatch.setenv(
            "TOOLLAYER_CALLER_TOKEN_TRUSTED_KEYS", f"caller-key:{caller_key.encoded_public_key()}"
        )
        monkeypatch.setenv("TOOLLAYER_CALLER_TOKEN_ISSUER", ISSUER)
        reset_settings_cache()
        with pytest.raises(ConfigurationError, match="AUDIENCE"):
            get_settings()
        reset_settings_cache()

    def test_an_unrecognized_mode_name_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No guessing what a near-miss was meant to say."""
        from runtime_service.config import ConfigurationError, get_settings, reset_settings_cache

        monkeypatch.setenv("TOOLLAYER_SNAPSHOT_VERIFICATION", "Disabled")
        reset_settings_cache()
        with pytest.raises(ConfigurationError, match=r"required.*disabled"):
            get_settings()
        reset_settings_cache()


class TestVerifiedIdentityOverHttp:
    def test_a_signed_token_reaches_the_authorized_tool(
        self, monkeypatch: pytest.MonkeyPatch, orchestrator: Any, caller_key: Any
    ) -> None:
        client = _runtime_client(monkeypatch, orchestrator, caller_key)
        token = issue_caller_token(
            caller_key,
            subject="bao@example.org",
            roles=("support-lead",),
            issuer=ISSUER,
            audience=AUDIENCE,
            issued_at=time.time(),
        )
        response = client.get("/v1/tools", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["caller"] == "bao@example.org"
        assert "change_support_ticket_status" in {tool["tool_name"] for tool in body["tools"]}

    def test_asserted_headers_cannot_reach_the_restricted_tool(
        self, monkeypatch: pytest.MonkeyPatch, orchestrator: Any, caller_key: Any
    ) -> None:
        """The header path that works in demo mode is closed here, with a 401."""
        client = _runtime_client(monkeypatch, orchestrator, caller_key)
        response = client.get(
            "/v1/tools",
            headers={
                "x-toollayer-caller": "attacker@example.org",
                "x-toollayer-roles": "support-lead",
            },
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == ErrorCode.UNAUTHENTICATED

    def test_no_token_is_a_401_rather_than_an_anonymous_caller(
        self, monkeypatch: pytest.MonkeyPatch, orchestrator: Any, caller_key: Any
    ) -> None:
        client = _runtime_client(monkeypatch, orchestrator, caller_key)
        assert client.get("/v1/tools").status_code == 401

    def test_an_expired_token_is_a_401(
        self, monkeypatch: pytest.MonkeyPatch, orchestrator: Any, caller_key: Any
    ) -> None:
        client = _runtime_client(monkeypatch, orchestrator, caller_key)
        expired = issue_caller_token(
            caller_key,
            subject="bao@example.org",
            roles=("support-lead",),
            issuer=ISSUER,
            audience=AUDIENCE,
            issued_at=time.time() - 3600,
            lifetime_seconds=60,
        )
        response = client.get("/v1/tools", headers={"authorization": f"Bearer {expired}"})
        assert response.status_code == 401

    def test_a_rejected_token_is_never_echoed_back(
        self, monkeypatch: pytest.MonkeyPatch, orchestrator: Any, caller_key: Any
    ) -> None:
        client = _runtime_client(monkeypatch, orchestrator, caller_key)
        token = issue_caller_token(
            caller_key,
            subject="bao@example.org",
            roles=("support-lead",),
            issuer="https://attacker.test",
            audience=AUDIENCE,
            issued_at=time.time(),
        )
        response = client.get("/v1/tools", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert token not in response.text


def _configure_verified(monkeypatch: pytest.MonkeyPatch, caller_key: Any) -> None:
    monkeypatch.setenv("TOOLLAYER_CALLER_AUTH_MODE", "verified_token")
    monkeypatch.setenv(
        "TOOLLAYER_CALLER_TOKEN_TRUSTED_KEYS", f"caller-key:{caller_key.encoded_public_key()}"
    )
    monkeypatch.setenv("TOOLLAYER_CALLER_TOKEN_ISSUER", ISSUER)
    monkeypatch.setenv("TOOLLAYER_CALLER_TOKEN_AUDIENCE", AUDIENCE)


def _runtime_client(
    monkeypatch: pytest.MonkeyPatch, orchestrator: Any, caller_key: Any
) -> TestClient:
    from runtime_service.config import reset_settings_cache
    from runtime_service.main import create_app

    _configure_verified(monkeypatch, caller_key)
    reset_settings_cache()
    return TestClient(create_app(orchestrator))


def _health(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import json

    return dict(json.loads(_health_text(monkeypatch)))


def _health_text(monkeypatch: pytest.MonkeyPatch) -> str:
    from runtime_service.config import get_settings, reset_settings_cache

    reset_settings_cache()
    posture = get_settings().security_posture
    reset_settings_cache()
    import json

    return json.dumps(posture)
