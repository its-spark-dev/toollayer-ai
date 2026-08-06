"""The YAML side of the ingestion boundary.

An uploaded API description is the first untrusted input the system takes, and YAML is the
format that makes that dangerous: ``yaml.load`` with the wrong loader turns a document into
arbitrary Python objects, and every unsafe use looks exactly like a safe one until you follow
the ``Loader=`` argument to wherever it was defined.

These tests pin four separate properties, because they can each be lost independently:

* **What the loader can build.** Only standard YAML scalars, sequences and mappings — no
  ``python/*`` tag, no unknown tag, no multi-constructor.
* **What it refuses.** Duplicate keys, merge keys, and non-scalar keys, each with the
  project's structured error rather than whatever the library happened to raise.
* **That it cannot be broken silently.** The loader is a subclass, so a later edit could
  widen it; the import-time guard is asserted here against the exact ways that happens.
* **That nothing runs.** A sentinel proves construction never reaches a callable, and the
  process is watched for command execution and filesystem writes throughout.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from toollayer_openapi.errors import DocumentTooLargeError, InvalidDocumentError
from toollayer_openapi.loader import (
    SourceLimits,
    _assert_loader_is_safe,
    _NoDuplicateKeyLoader,
    _parse_yaml,
    load_document,
)

pytestmark = pytest.mark.security

MINIMAL_SPEC = "openapi: '3.0.3'\ninfo: {title: T, version: '1'}\npaths: {}\n"

#: Appended to only if a payload manages to construct a callable. Nothing in this module ever
#: calls it deliberately, so a non-empty list is proof of arbitrary construction.
SENTINEL_CALLS: list[tuple[Any, ...]] = []


def sentinel(*args: Any, **kwargs: Any) -> str:
    SENTINEL_CALLS.append(args)
    return "constructed"


#: Payloads that reach arbitrary Python construction under a loader that permits it. Each is
#: written against this module so that, if one ever did construct, it would land in
#: `SENTINEL_CALLS` rather than doing something real.
PYTHON_TAG_PAYLOADS = {
    "object/apply-sentinel": (
        "a: !!python/object/apply:tests.security.test_yaml_ingestion_safety.sentinel ['x']"
    ),
    "object/new-sentinel": (
        "a: !!python/object/new:tests.security.test_yaml_ingestion_safety.sentinel"
    ),
    "object-sentinel": ("a: !!python/object:tests.security.test_yaml_ingestion_safety.sentinel {}"),
    "name-os-system": "a: !!python/name:os.system",
    "module-os": "a: !!python/module:os",
    "object/apply-os-system": 'a: !!python/object/apply:os.system ["#"]',
    "object/apply-subprocess": 'a: !!python/object/apply:subprocess.check_output [["true"]]',
}


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Fail loudly if parsing a payload executes anything, rather than trusting it did not.

    The assertions in these tests are all of the form "this was refused". That is only
    meaningful if a refusal is the *reason* nothing happened, so the obvious side channels are
    booby-trapped for the duration.
    """
    SENTINEL_CALLS.clear()

    def _forbidden(name: str) -> Any:
        def _fail(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"parsing a YAML payload invoked {name}({args!r})")

        return _fail

    monkeypatch.setattr(os, "system", _forbidden("os.system"))
    monkeypatch.setattr(os, "popen", _forbidden("os.popen"))
    monkeypatch.setattr(subprocess, "run", _forbidden("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _forbidden("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "check_output", _forbidden("subprocess.check_output"))

    marker = tmp_path / "written-by-a-payload"
    monkeypatch.chdir(tmp_path)
    yield
    assert SENTINEL_CALLS == [], f"a payload constructed a callable: {SENTINEL_CALLS}"
    assert not marker.exists()
    assert not any(tmp_path.iterdir()), f"a payload wrote to disk: {list(tmp_path.iterdir())}"


class TestUnsafeTagsAreRefused:
    @pytest.mark.parametrize("payload", PYTHON_TAG_PAYLOADS.values(), ids=PYTHON_TAG_PAYLOADS)
    def test_a_python_tag_is_refused_without_constructing_anything(self, payload: str) -> None:
        with pytest.raises(InvalidDocumentError):
            _parse_yaml(payload)
        assert SENTINEL_CALLS == []

    @pytest.mark.parametrize("payload", PYTHON_TAG_PAYLOADS.values(), ids=PYTHON_TAG_PAYLOADS)
    def test_the_payload_targets_a_capability_an_unsafe_loader_really_has(
        self, payload: str
    ) -> None:
        """Proves the payloads are live ammunition, not strings that happen to be refused.

        The tag each one uses is checked against ``yaml.UnsafeLoader``'s constructor table
        rather than by loading it there — establishing that these are tags a permissive loader
        would act on, without any of them being acted on.
        """
        tag = payload.split("!!", 1)[1].split(" ", 1)[0].split("[", 1)[0].strip()
        unsafe_prefixes = tuple(yaml.UnsafeLoader.yaml_multi_constructors)
        unsafe_exact = set(yaml.UnsafeLoader.yaml_constructors)
        qualified = f"tag:yaml.org,2002:{tag}"
        assert qualified in unsafe_exact or qualified.startswith(unsafe_prefixes), qualified
        assert qualified not in _NoDuplicateKeyLoader.yaml_constructors

    @pytest.mark.parametrize(
        "payload",
        ["a: !customtag {b: 1}", "a: !!weirdtype 5", "a: !<tag:example.com,2024:thing> 1"],
        ids=["local-tag", "unknown-yaml-org-tag", "verbatim-uri-tag"],
    )
    def test_an_unknown_tag_is_refused(self, payload: str) -> None:
        with pytest.raises(InvalidDocumentError):
            _parse_yaml(payload)


class TestLoaderCannotBeWidenedSilently:
    def test_the_loader_inherits_only_from_safe_bases(self) -> None:
        names = {base.__name__ for base in _NoDuplicateKeyLoader.__mro__}
        assert "SafeLoader" in names
        assert not names & {"Loader", "FullLoader", "UnsafeLoader"}
        assert not names & {"Constructor", "FullConstructor", "UnsafeConstructor"}

    def test_no_python_constructor_is_registered(self) -> None:
        assert not [t for t in _NoDuplicateKeyLoader.yaml_constructors if "python" in str(t)]

    def test_no_multi_constructor_is_registered(self) -> None:
        """Multi-constructors match by tag *prefix*, which is how `!!python/object:` is reached."""
        assert _NoDuplicateKeyLoader.yaml_multi_constructors == {}

    def test_the_subclass_changed_exactly_one_tag(self) -> None:
        """`add_constructor` copies the parent table, so this pins what the copy differs by."""
        safe = yaml.SafeLoader.yaml_constructors
        ours = _NoDuplicateKeyLoader.yaml_constructors
        assert set(ours) == set(safe)
        differing = {t for t in ours if ours[t] is not safe[t]}
        assert differing == {yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG}

    def test_the_import_time_guard_rejects_an_unsafe_base(self) -> None:
        class Widened(yaml.Loader):
            pass

        with pytest.raises(RuntimeError, match="unsafe base"):
            _assert_loader_is_safe(Widened)

    def test_the_import_time_guard_rejects_a_python_tag(self) -> None:
        class Widened(yaml.SafeLoader):
            pass

        Widened.add_constructor("tag:yaml.org,2002:python/object:os.system", lambda _l, _n: None)
        with pytest.raises(RuntimeError, match="Python tags"):
            _assert_loader_is_safe(Widened)

    def test_the_import_time_guard_rejects_a_multi_constructor(self) -> None:
        class Widened(yaml.SafeLoader):
            pass

        Widened.add_multi_constructor("tag:yaml.org,2002:python/object:", lambda _l, _s, _n: None)
        with pytest.raises(RuntimeError, match="multi-constructors"):
            _assert_loader_is_safe(Widened)

    def test_the_module_makes_no_yaml_load_call(self) -> None:
        """The pattern CodeQL flags, asserted absent at the syntax level rather than by eye.

        `yaml.load(text, Loader=X)` is safe or catastrophic depending on `X`, which is chosen
        somewhere else. This module instantiates its loader directly, so there is no argument
        that a later edit could point at `yaml.Loader`.
        """
        import ast
        import inspect

        from toollayer_openapi import loader as loader_module

        tree = ast.parse(inspect.getsource(loader_module))
        offending = [
            f"line {node.lineno}: {ast.unparse(node)[:70]}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) in {"yaml.load", "load"}
        ]
        assert offending == [], f"yaml.load reintroduced: {offending}"


class TestStructuralRefusals:
    def test_duplicate_keys_are_refused(self) -> None:
        with pytest.raises(InvalidDocumentError, match="duplicate mapping key"):
            _parse_yaml("a: 1\nb: 2\na: 3")

    def test_duplicate_keys_are_refused_when_nested(self) -> None:
        with pytest.raises(InvalidDocumentError, match="duplicate mapping key"):
            _parse_yaml("outer:\n  inner: 1\n  inner: 2\n")

    def test_a_merge_key_is_refused_with_its_own_message(self) -> None:
        """Valid YAML, deliberately unsupported, and previously mislabelled as malformed."""
        with pytest.raises(InvalidDocumentError, match="merge keys are not supported"):
            _parse_yaml("base: &b {k: 1}\nchild:\n  <<: *b\n  j: 2\n")

    @pytest.mark.parametrize(
        "payload", ["? {a: 1}\n: value", "? [1, 2]\n: value"], ids=["mapping-key", "sequence-key"]
    )
    def test_a_non_scalar_key_is_a_structured_error_not_a_type_error(self, payload: str) -> None:
        """Previously an unhandled `TypeError: unhashable type`, which surfaced as a 500."""
        with pytest.raises(InvalidDocumentError, match="keys must be scalars"):
            _parse_yaml(payload)

    def test_malformed_yaml_produces_the_projects_error(self) -> None:
        with pytest.raises(InvalidDocumentError, match="not well-formed YAML"):
            _parse_yaml("a: [1, 2\nb: }{")

    def test_aliases_still_resolve(self) -> None:
        """Anchors and aliases are ordinary YAML and stay supported."""
        parsed = _parse_yaml("x: &anchor {k: 1}\ny: *anchor\n")
        assert parsed == {"x": {"k": 1}, "y": {"k": 1}}


class TestLimitsHold:
    @pytest.mark.parametrize("depth", [331, 500, 1_000, 20_000], ids=str)
    def test_deep_nesting_is_a_structured_refusal_at_any_depth(self, depth: int) -> None:
        """PyYAML's composer recurses even though its scanner does not.

        Before this was handled, a 692-byte upload produced an unhandled `RecursionError` and
        an HTTP 500 — from the one code path whose comment promised the opposite.
        """
        document = ("openapi: '3.0.3'\npaths: {}\nd: " + "[" * depth + "]" * depth).encode()
        with pytest.raises(DocumentTooLargeError, match="nests too deeply"):
            load_document(document, filename="deep.yaml")

    def test_alias_expansion_is_caught_by_the_node_limit(self) -> None:
        """The billion-laughs shape. Aliases share objects, so the cost is in the walk."""
        lines = ["a0: &a0 x"]
        for i in range(1, 7):
            lines.append(f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]")
        lines += ["openapi: '3.0.3'", "paths: {p: *a6}"]
        document = "\n".join(lines).encode()
        assert len(document) < 1024
        with pytest.raises(DocumentTooLargeError, match="too many nodes"):
            load_document(document, filename="bomb.yaml")

    def test_the_byte_limit_applies_before_any_parsing(self) -> None:
        oversized = b"openapi: '3.0.3'\npaths: {}\nx: " + b"a" * (3 * 1024 * 1024)
        with pytest.raises(DocumentTooLargeError, match="ingestion limit"):
            load_document(oversized, filename="big.yaml", limits=SourceLimits())

    def test_the_node_limit_is_enforced(self) -> None:
        document = (
            "openapi: '3.0.3'\npaths: {}\nitems: [" + ",".join("1" * 1 for _ in range(50)) + "]"
        ).encode()
        with pytest.raises(DocumentTooLargeError, match="too many nodes"):
            load_document(document, filename="n.yaml", limits=SourceLimits(max_nodes=5))


class TestValidDocumentsStillLoad:
    def test_a_minimal_openapi_document_loads(self) -> None:
        loaded = load_document(MINIMAL_SPEC.encode(), filename="min.yaml")
        assert loaded.source_format == "yaml"
        assert loaded.spec_version == "3.0.3"

    def test_the_repositorys_own_example_specification_loads(self) -> None:
        """The document every test, the demo and the capture run against."""
        spec = Path(__file__).resolve().parents[2] / "examples" / "support-api.openapi.yaml"
        loaded = load_document(spec.read_bytes(), filename=spec.name)
        assert loaded.source_format == "yaml"
        assert loaded.document["openapi"].startswith("3.")
        assert loaded.document["paths"]

    def test_ordinary_yaml_scalar_types_survive(self) -> None:
        parsed = _parse_yaml(
            "s: text\ni: 42\nf: 1.5\nb: true\nn: null\nd: 2026-08-06\nl: [1, 2]\nm: {k: v}\n"
        )
        assert parsed["s"] == "text"
        assert parsed["i"] == 42 and parsed["f"] == 1.5
        assert parsed["b"] is True and parsed["n"] is None
        assert parsed["l"] == [1, 2] and parsed["m"] == {"k": "v"}
