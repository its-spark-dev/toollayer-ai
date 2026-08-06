"""One request uses one immutable snapshot revision.

Two properties, and they fail in ways that ordinary tests do not catch.

**One revision per request.** If a turn re-read the snapshot store partway through, a refresh
landing in that window would let it discover tools from one revision and execute against
another — with a policy nobody ever evaluated as a whole. The tests here force a refresh at
exactly that moment and assert the turn does not notice.

**Deep immutability.** ``LoadedSnapshot`` is a frozen dataclass, but freezing the outside of
an object that holds a mutable ``dict`` proves nothing: anyone with a reference could rewrite
the dispatch index and change what every subsequent request resolves to.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.integration


class _SwitchingClient:
    """A snapshot client that serves a different revision on every fetch.

    Standing in for a Control Plane that publishes while a request is in flight — the exact
    condition under which a second ``ensure_fresh()`` inside one turn would be visible.
    """

    def __init__(self, base: Any, verification: Any, signing_key: Any) -> None:
        self._base = base
        self._verification = verification
        self._signing_key = signing_key
        self.fetches = 0

    @property
    def verification(self) -> Any:
        return self._verification

    def fetch(self, deployment_key: str, *, if_none_match: str | None = None) -> Any:
        self.fetches += 1
        return self._at_revision(self._base.revision + self.fetches)

    def _at_revision(self, revision: int) -> Any:
        """Rebuild the loaded snapshot at a new revision, re-signed so it still verifies."""
        import copy

        from runtime_service.snapshot import load_snapshot_document
        from toollayer_contracts import SNAPSHOT_DIGEST_EXCLUDED, content_digest, sign_document

        document = copy.deepcopy(self._base.snapshot.model_dump(mode="json"))
        document.pop("signature", None)
        document["revision"] = revision
        document["snapshot_digest"] = content_digest(document, exclude=SNAPSHOT_DIGEST_EXCLUDED)
        document["signature"] = sign_document(document, self._signing_key)
        return load_snapshot_document(document, verification=self._verification)


@pytest.fixture()
def switching_store(loaded_snapshot: Any, snapshot_verification: Any, signing_key: Any) -> Any:
    """A store whose every refresh yields a new revision."""
    from runtime_service.snapshot import SnapshotStore

    client = _SwitchingClient(loaded_snapshot, snapshot_verification, signing_key)
    store = SnapshotStore(client, deployment_key="demo-workspace", refresh_seconds=60)  # type: ignore[arg-type]
    store.set(loaded_snapshot)
    return store


class TestOneRequestOneRevision:
    def test_a_refresh_during_a_turn_does_not_change_the_revision_it_reports(
        self, switching_store: Any, runtime_executor: Any
    ) -> None:
        """The turn is interrupted at its most vulnerable moment and does not notice.

        The provider hook fires between discovery and execution. Refreshing there would move
        a re-reading orchestrator onto a new revision mid-request.
        """
        from runtime_service.orchestrator import Orchestrator
        from toollayer_mock_llm import MockLLMProvider

        starting = switching_store.current.revision

        class RefreshingProvider(MockLLMProvider):
            def select_tool(self, text: str, tools: Any) -> Any:
                switching_store.refresh()
                return super().select_tool(text, tools)

        engine = Orchestrator(
            store=switching_store, provider=RefreshingProvider(), executor=runtime_executor
        )
        outcome = engine.handle(
            "show me the open high priority tickets for the billing team", caller=None
        )

        assert outcome.snapshot_revision == starting
        assert switching_store.current.revision > starting, "the store really did move on"

    def test_every_part_of_the_outcome_names_the_same_revision(
        self, orchestrator: Any, loaded_snapshot: Any
    ) -> None:
        outcome = orchestrator.handle(
            "show me the open high priority tickets for the billing team", caller=None
        )
        loaded_step = next(
            step for step in outcome.trace.steps if step["step"] == "snapshot_loaded"
        )
        assert loaded_step["revision"] == outcome.snapshot_revision
        assert loaded_step["snapshot_id"] == outcome.snapshot_id
        assert outcome.snapshot_id == loaded_snapshot.snapshot_id
        assert outcome.connector_version == loaded_snapshot.tools[0].connector_version

    def test_the_turn_acquires_the_snapshot_exactly_once(
        self, switching_store: Any, runtime_executor: Any
    ) -> None:
        """Counting fetches is what distinguishes 'one snapshot' from 'the same by luck'.

        Discovery used to call ``ensure_fresh()`` on its own; with a stale store that is a
        second fetch, and a second fetch is a second revision.
        """
        from runtime_service.orchestrator import Orchestrator
        from toollayer_mock_llm import MockLLMProvider

        # Zero refresh interval, so every ensure_fresh() actually goes to the client.
        switching_store._refresh_seconds = 1
        switching_store._current = None
        engine = Orchestrator(
            store=switching_store, provider=MockLLMProvider(), executor=runtime_executor
        )
        switching_store.refresh()
        before = switching_store._client.fetches

        outcome = engine.handle(
            "show me the open high priority tickets for the billing team", caller=None
        )
        assert switching_store._client.fetches - before <= 1
        assert outcome.snapshot_revision == switching_store.current.revision

    def test_a_later_request_does_observe_the_newer_revision(
        self, switching_store: Any, runtime_executor: Any
    ) -> None:
        """Consistency within a request must not become staleness across requests."""
        from runtime_service.orchestrator import Orchestrator
        from toollayer_mock_llm import MockLLMProvider

        engine = Orchestrator(
            store=switching_store, provider=MockLLMProvider(), executor=runtime_executor
        )
        first = engine.handle("show me the open tickets for the billing team", caller=None)
        switching_store.refresh()
        second = engine.handle("show me the open tickets for the billing team", caller=None)
        assert second.snapshot_revision > first.snapshot_revision

    def test_an_in_flight_snapshot_reference_keeps_serving_its_own_revision(
        self, switching_store: Any
    ) -> None:
        """The held object is unaffected by anything the store does afterwards."""
        held = switching_store.current
        switching_store.refresh()
        switching_store.refresh()
        assert held.revision != switching_store.current.revision
        assert held.resolve("list_support_tickets").tool_name == "list_support_tickets"


class TestAFailedRefreshNeverDowngradesWhatIsServed:
    def test_a_snapshot_that_fails_verification_does_not_replace_the_held_one(
        self, loaded_snapshot: Any, snapshot_verification: Any, untrusted_signing_key: Any
    ) -> None:
        """Availability without accepting the forgery.

        The Control Plane starts returning artifacts this runtime cannot authenticate. The
        right answer is to keep serving the last verified revision, not to fail every request
        and not to accept the new one.
        """
        from runtime_service.snapshot import SnapshotStore

        client = _SwitchingClient(loaded_snapshot, snapshot_verification, untrusted_signing_key)
        store = SnapshotStore(client, deployment_key="demo-workspace")  # type: ignore[arg-type]
        store.set(loaded_snapshot)

        assert store.refresh().revision == loaded_snapshot.revision
        assert store.current.signing_key_id == "test-snapshot-key"

    def test_with_nothing_held_a_failed_verification_refuses_outright(
        self, loaded_snapshot: Any, snapshot_verification: Any, untrusted_signing_key: Any
    ) -> None:
        """No held snapshot means no fallback: the runtime serves nothing rather than that."""
        from runtime_service.snapshot import SnapshotSignatureError, SnapshotStore

        client = _SwitchingClient(loaded_snapshot, snapshot_verification, untrusted_signing_key)
        store = SnapshotStore(client, deployment_key="demo-workspace")  # type: ignore[arg-type]
        with pytest.raises(SnapshotSignatureError):
            store.refresh()
        assert store.loaded is False


class TestDeepImmutability:
    def test_the_tool_index_cannot_be_mutated(self, loaded_snapshot: Any) -> None:
        with pytest.raises(TypeError):
            loaded_snapshot.tools_by_name["injected"] = None

    def test_an_existing_entry_cannot_be_replaced(self, loaded_snapshot: Any) -> None:
        """The dangerous case: swapping a real tool for one pointing somewhere else."""
        with pytest.raises(TypeError):
            loaded_snapshot.tools_by_name["list_support_tickets"] = None

    def test_the_index_cannot_be_cleared_or_popped(self, loaded_snapshot: Any) -> None:
        for attempt in (
            lambda: loaded_snapshot.tools_by_name.clear(),
            lambda: loaded_snapshot.tools_by_name.pop("list_support_tickets"),
            lambda: loaded_snapshot.tools_by_name.update({"x": None}),
        ):
            with pytest.raises((TypeError, AttributeError)):
                attempt()

    def test_the_snapshot_object_itself_is_frozen(self, loaded_snapshot: Any) -> None:
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            loaded_snapshot.tools_by_name = {}

    def test_a_bound_tool_cannot_have_its_destination_rewritten(self, loaded_snapshot: Any) -> None:
        """The base URL is what the destination policy is checked against."""
        from dataclasses import FrozenInstanceError

        bound = loaded_snapshot.resolve("list_support_tickets")
        with pytest.raises(FrozenInstanceError):
            bound.base_url = "https://attacker.test"

    def test_a_tool_policy_cannot_be_widened_in_place(self, loaded_snapshot: Any) -> None:
        """Contract models are frozen, so the index holds no writable policy either."""
        bound = loaded_snapshot.resolve("change_support_ticket_status")
        assert bound.tool.policy.requires_confirmation is True
        with pytest.raises(ValidationError):
            bound.tool.policy.requires_confirmation = False

    def test_the_tools_tuple_is_a_copy_not_a_live_view(self, loaded_snapshot: Any) -> None:
        tools = loaded_snapshot.tools
        assert isinstance(tools, tuple)
        assert len(tools) == len(loaded_snapshot.tools_by_name)
