"""Contract-model behavior: invariants, canonical serialization, and digests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError
from tests.factories import closed_schema, make_operation, make_tool

from toollayer_contracts import (
    CONTRACT_VERSION,
    ArgumentBinding,
    ToolAccessPolicy,
    canonical_json,
    content_digest,
    verify_digest,
)
from toollayer_contracts.canonical_json import CanonicalJsonError
from toollayer_contracts.version import (
    IncompatibleContractVersionError,
    compare_precedence,
    parse_version,
    require_supported,
)

pytestmark = pytest.mark.unit

CLOSED_SCHEMA = closed_schema({"status": {"type": "string"}})
_tool = make_tool


class TestCanonicalJson:
    def test_key_order_does_not_change_the_bytes(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_equal_documents_share_a_digest(self) -> None:
        assert content_digest({"b": [1, 2], "a": "x"}) == content_digest({"a": "x", "b": [1, 2]})

    def test_any_change_changes_the_digest(self) -> None:
        assert content_digest({"a": 1}) != content_digest({"a": 2})

    def test_a_document_can_carry_its_own_digest(self) -> None:
        payload = {"value": 42}
        digest = content_digest(payload)
        document = {**payload, "digest": digest}
        assert verify_digest(document, digest, exclude=("digest",))

    def test_verification_fails_when_the_content_changed(self) -> None:
        payload = {"value": 42}
        document = {**payload, "digest": content_digest(payload)}
        document["value"] = 43
        assert not verify_digest(document, document["digest"], exclude=("digest",))

    def test_rejects_values_it_cannot_serialize_deterministically(self) -> None:
        with pytest.raises(CanonicalJsonError):
            canonical_json({"x": float("nan")})
        with pytest.raises(CanonicalJsonError):
            canonical_json({"x": {1, 2}})


class TestContractVersion:
    def test_accepts_the_current_version(self) -> None:
        assert str(require_supported(CONTRACT_VERSION)) == CONTRACT_VERSION

    def test_rejects_a_different_major_line(self) -> None:
        with pytest.raises(IncompatibleContractVersionError, match="major"):
            require_supported("2.0.0")

    def test_rejects_a_newer_minor_than_this_build_understands(self) -> None:
        with pytest.raises(IncompatibleContractVersionError, match="minor"):
            require_supported("1.99.0")

    def test_accepts_an_older_minor(self) -> None:
        require_supported("1.0.0")

    def test_rejects_a_non_semver_value_without_echoing_it(self) -> None:
        with pytest.raises(IncompatibleContractVersionError) as caught:
            require_supported("v1.2.3-not-a-version!!")
        assert "not-a-version" not in str(caught.value)

    def test_orders_a_prerelease_below_its_release(self) -> None:
        assert compare_precedence(parse_version("1.0.0-rc.1"), parse_version("1.0.0")) < 0
        assert compare_precedence(parse_version("1.2.0"), parse_version("1.1.9")) > 0
        assert compare_precedence(parse_version("1.0.0"), parse_version("1.0.0")) == 0


class TestToolInvariants:
    def test_accepts_a_well_formed_tool(self) -> None:
        assert _tool().tool_name == "list_things"

    def test_rejects_an_open_input_schema(self) -> None:
        with pytest.raises(PydanticValidationError, match="additionalProperties"):
            _tool(input_schema={**CLOSED_SCHEMA, "additionalProperties": True})

    def test_rejects_a_binding_for_an_undeclared_argument(self) -> None:
        with pytest.raises(PydanticValidationError, match="does not declare"):
            _tool(
                operation=make_operation(
                    bindings=(
                        ArgumentBinding(
                            argument_pointer="/nope", target="query", target_name="nope"
                        ),
                    )
                )
            )

    def test_rejects_two_arguments_bound_to_one_location(self) -> None:
        with pytest.raises(PydanticValidationError, match="same request location"):
            make_operation(
                bindings=(
                    ArgumentBinding(argument_pointer="/a", target="query", target_name="q"),
                    ArgumentBinding(argument_pointer="/b", target="query", target_name="q"),
                )
            )

    def test_rejects_a_body_binding_without_a_media_type(self) -> None:
        with pytest.raises(PydanticValidationError, match="request_body_media_type"):
            make_operation(
                method="POST",
                bindings=(
                    ArgumentBinding(argument_pointer="/body/x", target="body", target_name="/x"),
                ),
            )

    def test_rejects_an_absolute_path_template(self) -> None:
        with pytest.raises(PydanticValidationError):
            make_operation(path_template="https://elsewhere.example/v1")


class TestAccessPolicy:
    def test_a_public_tool_carries_no_roles(self) -> None:
        with pytest.raises(PydanticValidationError, match="public tool cannot carry"):
            ToolAccessPolicy(access_mode="public", allowed_roles=("support-lead",))

    def test_a_restricted_tool_must_allow_someone(self) -> None:
        with pytest.raises(PydanticValidationError, match="at least one role"):
            ToolAccessPolicy(access_mode="restricted", allowed_roles=())

    def test_role_order_does_not_change_the_published_bytes(self) -> None:
        left = ToolAccessPolicy(access_mode="restricted", allowed_roles=("b-role", "a-role"))
        right = ToolAccessPolicy(access_mode="restricted", allowed_roles=("a-role", "b-role"))
        assert canonical_json(left.model_dump(mode="json")) == canonical_json(
            right.model_dump(mode="json")
        )


class TestRuntimeBinding:
    @pytest.mark.parametrize(
        "base_url",
        [
            "https://user:secret@api.example.org",
            "ftp://api.example.org",
            "https://api.example.org/v1?token=abc",
            "not-a-url",
        ],
    )
    def test_rejects_an_unreviewable_base_url(self, base_url: str) -> None:
        from toollayer_contracts.models import RuntimeBinding

        with pytest.raises(PydanticValidationError):
            RuntimeBinding(protocol="http", base_url=base_url, auth_profile_ref=None)

    def test_accepts_an_origin_with_a_base_path(self) -> None:
        from toollayer_contracts.models import RuntimeBinding

        binding = RuntimeBinding(
            protocol="http", base_url="https://api.example.org/v1", auth_profile_ref=None
        )
        assert binding.base_url.endswith("/v1")
