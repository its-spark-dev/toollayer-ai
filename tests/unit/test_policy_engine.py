"""Policy-engine behavior: authorization, destinations, arguments, and redaction."""

from __future__ import annotations

import pytest
from tests.factories import closed_schema, make_operation, make_provenance, make_tool

from toollayer_contracts.errors import PolicyDenied
from toollayer_contracts.models import ArgumentBinding, ToolDefinition
from toollayer_policy import (
    ArgumentValidationError,
    CallerIdentity,
    DestinationPolicy,
    ExecutionLimits,
    ToolPolicyError,
    authorize_stored_policy,
    authorize_tool,
    normalize_origin,
    parse_audience_policy,
    prepare_request,
    redact_headers,
    redact_mapping,
    redact_url,
    validate_arguments,
)

pytestmark = pytest.mark.unit


class StubResolver:
    def __init__(self, table: dict[str, tuple[str, ...]]) -> None:
        self._table = table

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        return self._table.get(host, ())


def _tool(**overrides) -> ToolDefinition:
    defaults: dict[str, object] = {
        "tool_name": "get_thing",
        "display_name": "Get thing",
        "description": "Get one thing.",
        "input_schema": closed_schema(
            {
                "thing_id": {"type": "string"},
                "verbose": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            ["thing_id"],
        ),
        "operation": make_operation(
            path_template="/v1/things/{thing_id}",
            bindings=(
                ArgumentBinding(
                    argument_pointer="/thing_id", target="path", target_name="thing_id"
                ),
                ArgumentBinding(argument_pointer="/verbose", target="query", target_name="verbose"),
                ArgumentBinding(argument_pointer="/limit", target="query", target_name="limit"),
            ),
        ),
        "provenance": make_provenance(source_path="/v1/things/{thing_id}"),
    }
    defaults.update(overrides)
    return make_tool(**defaults)


class TestAuthorization:
    def test_a_public_tool_allows_anyone_including_an_anonymous_caller(self) -> None:
        policy = parse_audience_policy({"access": {"access_mode": "public", "allowed_roles": []}})
        assert authorize_tool(policy=policy, caller=None).allowed
        assert authorize_tool(policy=policy, caller=CallerIdentity.of("u")).allowed

    def test_a_tool_with_no_access_object_is_public(self) -> None:
        assert not parse_audience_policy({"effect_class": "read"}).restricted

    def test_a_restricted_tool_requires_a_listed_role(self) -> None:
        policy = parse_audience_policy(
            {"access": {"access_mode": "restricted", "allowed_roles": ["support-lead"]}}
        )
        assert authorize_tool(policy=policy, caller=CallerIdentity.of("u", ["support-lead"])).allowed
        denied = authorize_tool(policy=policy, caller=CallerIdentity.of("u", ["support-agent"]))
        assert denied.denied and denied.reason_code == "role_not_permitted"

    def test_a_restricted_tool_denies_a_caller_with_no_roles(self) -> None:
        policy = parse_audience_policy(
            {"access": {"access_mode": "restricted", "allowed_roles": ["support-lead"]}}
        )
        decision = authorize_tool(policy=policy, caller=None)
        assert decision.denied and decision.reason_code == "caller_has_no_roles"

    def test_an_unreadable_policy_denies_rather_than_defaulting_to_public(self) -> None:
        for broken in (
            {"access": {"access_mode": "restricted", "allowed_roles": []}},
            {"access": {"access_mode": "public", "allowed_roles": ["x"]}},
            {"access": {"access_mode": "everyone", "allowed_roles": []}},
            {"access": "not-an-object"},
            {"access": {"access_mode": "restricted", "allowed_roles": ["Not Canonical"]}},
        ):
            with pytest.raises(ToolPolicyError):
                parse_audience_policy(broken)
            assert authorize_stored_policy(broken, CallerIdentity.of("u", ["support-lead"])).denied

    def test_authorize_tool_denies_when_the_policy_could_not_be_parsed(self) -> None:
        assert authorize_tool(policy=None, caller=CallerIdentity.of("u", ["admin"])).denied


class TestDestinationPolicy:
    def test_an_empty_allowlist_permits_nothing(self) -> None:
        with pytest.raises(PolicyDenied, match="no allowlisted destinations"):
            DestinationPolicy().check("https://api.example.org/v1")

    def test_matching_is_exact_not_suffix_based(self) -> None:
        policy = DestinationPolicy.from_origins(["https://api.example.org"])
        policy.check(
            "https://api.example.org/v1/things",
            resolver=StubResolver({"api.example.org": ("100.0.0.1",)}),
        )
        for hostile in (
            "https://api.example.org.attacker.test/v1",
            "https://notapi.example.org/v1",
            "https://api.example.org:8443/v1",
        ):
            with pytest.raises(PolicyDenied, match="allowlist"):
                policy.check(hostile)

    def test_the_host_comparison_ignores_case(self) -> None:
        policy = DestinationPolicy.from_origins(["https://API.Example.org"])
        policy.check(
            "https://api.example.org/v1",
            resolver=StubResolver({"api.example.org": ("100.0.0.1",)}),
        )

    def test_plaintext_http_is_refused_unless_enabled(self) -> None:
        policy = DestinationPolicy.from_origins(["http://api.example.org"], allow_plaintext_http=False)
        with pytest.raises(PolicyDenied, match="plaintext"):
            policy.check("http://api.example.org/v1")

    def test_credentials_in_the_url_are_refused(self) -> None:
        policy = DestinationPolicy.from_origins(["https://api.example.org"])
        with pytest.raises(PolicyDenied, match="credentials"):
            policy.check("https://user:pass@api.example.org/v1")

    def test_an_allowlisted_name_resolving_into_private_space_is_refused(self) -> None:
        # This is the SSRF case a URL-shaped allowlist alone does not catch: the origin is
        # allowed, but the name points somewhere it must not reach.
        policy = DestinationPolicy.from_origins(["https://api.example.org"])
        for address, message in (
            ("169.254.169.254", "link-local"),
            ("10.0.0.5", "not globally routable"),
            ("127.0.0.1", "loopback"),
            ("::1", "loopback"),
            ("224.0.0.1", "reserved"),
        ):
            with pytest.raises(PolicyDenied, match=message):
                policy.check(
                    "https://api.example.org/v1",
                    resolver=StubResolver({"api.example.org": (address,)}),
                )

    def test_every_resolved_address_is_checked_not_just_the_first(self) -> None:
        policy = DestinationPolicy.from_origins(["https://api.example.org"])
        with pytest.raises(PolicyDenied, match="link-local"):
            policy.check(
                "https://api.example.org/v1",
                resolver=StubResolver({"api.example.org": ("100.0.0.1", "169.254.169.254")}),
            )

    def test_link_local_is_refused_even_when_private_addresses_are_allowed(self) -> None:
        policy = DestinationPolicy.from_origins(
            ["https://api.example.org"], allow_private_addresses=True, allow_loopback=True
        )
        with pytest.raises(PolicyDenied, match="link-local"):
            policy.check(
                "https://api.example.org/v1",
                resolver=StubResolver({"api.example.org": ("169.254.169.254",)}),
            )

    def test_the_method_allowlist_is_enforced_separately(self) -> None:
        policy = DestinationPolicy.from_origins(
            ["https://api.example.org"], allowed_methods=frozenset({"GET"})
        )
        policy.check_method("GET")
        with pytest.raises(PolicyDenied, match="DELETE"):
            policy.check_method("delete")

    def test_normalize_origin_makes_the_port_explicit(self) -> None:
        assert normalize_origin("https://api.example.org") == "https://api.example.org:443"
        assert normalize_origin("http://api.example.org") == "http://api.example.org:80"


class TestArgumentValidation:
    def test_accepts_arguments_that_satisfy_the_schema(self) -> None:
        assert validate_arguments(_tool(), {"thing_id": "abc", "limit": 3})["limit"] == 3

    @pytest.mark.parametrize(
        "arguments",
        [
            {},  # missing a required argument
            {"thing_id": "abc", "surprise": 1},  # undeclared argument
            {"thing_id": 42},  # wrong type
            {"thing_id": "abc", "limit": 99},  # out of bounds
            "not-an-object",
        ],
    )
    def test_rejects_anything_the_schema_does_not_allow(self, arguments) -> None:
        with pytest.raises(ArgumentValidationError):
            validate_arguments(_tool(), arguments)

    def test_the_error_names_the_location_without_echoing_the_value(self) -> None:
        with pytest.raises(ArgumentValidationError) as caught:
            validate_arguments(_tool(), {"thing_id": "abc", "limit": 12345})
        assert "12345" not in str(caught.value)
        assert caught.value.pointer == "/limit"

    def test_reports_every_violation_not_only_the_first(self) -> None:
        with pytest.raises(ArgumentValidationError) as caught:
            validate_arguments(_tool(), {"limit": 99, "extra": True})
        assert len(caught.value.details) >= 2


class TestRequestConstruction:
    def test_builds_a_url_from_the_bindings(self) -> None:
        request = prepare_request(
            _tool(),
            {"thing_id": "abc", "verbose": True, "limit": 5},
            base_url="https://api.example.org/",
        )
        assert request.url == "https://api.example.org/v1/things/abc?verbose=true&limit=5"

    def test_an_absent_optional_argument_is_omitted_not_blanked(self) -> None:
        request = prepare_request(_tool(), {"thing_id": "abc"}, base_url="https://api.example.org")
        assert request.url == "https://api.example.org/v1/things/abc"

    def test_a_path_argument_cannot_rewrite_the_request_target(self) -> None:
        request = prepare_request(
            _tool(), {"thing_id": "../../admin?x=1"}, base_url="https://api.example.org"
        )
        assert request.url == "https://api.example.org/v1/things/..%2F..%2Fadmin%3Fx%3D1"
        assert "?x=1" not in request.url

    def test_a_json_body_is_built_from_body_bindings(self) -> None:
        tool = _tool(
            tool_name="create_thing",
            input_schema=closed_schema(
                {
                    "body": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    }
                },
                ["body"],
            ),
            operation=make_operation(
                method="POST",
                bindings=(
                    ArgumentBinding(
                        argument_pointer="/body/name", target="body", target_name="/name"
                    ),
                ),
                request_body_media_type="application/json",
            ),
            provenance=make_provenance(source_method="post"),
        )
        request = prepare_request(tool, {"body": {"name": "x"}}, base_url="https://api.example.org")
        assert request.body == b'{"name":"x"}'
        assert ("content-type", "application/json") in request.headers


class TestRedaction:
    def test_sensitive_headers_are_replaced(self) -> None:
        redacted = redact_headers(
            {"authorization": "Bearer abc", "x-toollayer-service-token": "s", "accept": "json"}
        )
        assert redacted["authorization"] == "[redacted]"
        assert redacted["x-toollayer-service-token"] == "[redacted]"
        assert redacted["accept"] == "json"

    def test_sensitive_fields_are_replaced_at_any_depth(self) -> None:
        redacted = redact_mapping({"outer": {"api_key": "k", "name": "n"}})
        assert redacted["outer"]["api_key"] == "[redacted]"
        assert redacted["outer"]["name"] == "n"

    def test_url_credentials_and_sensitive_query_values_are_removed(self) -> None:
        redacted = redact_url("https://u:p@api.example.org/v1?access_token=abc&page=2")
        assert "p@" not in redacted and "abc" not in redacted
        assert "page=2" in redacted


class TestExecutionLimits:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"connect_timeout_seconds": 0},
            {"read_timeout_seconds": 600},
            {"max_response_bytes": 0},
            {"max_response_bytes": 1024 * 1024 * 1024},
        ],
    )
    def test_every_bound_must_be_finite_and_sane(self, kwargs) -> None:
        with pytest.raises(ValueError):
            ExecutionLimits(**kwargs)

    def test_defaults_are_bounded(self) -> None:
        limits = ExecutionLimits()
        assert limits.connect_timeout_seconds > 0
        assert limits.read_timeout_seconds > 0
        assert limits.max_response_bytes > 0
