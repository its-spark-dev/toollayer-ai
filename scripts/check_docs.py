#!/usr/bin/env python3
"""Check that the documentation still describes the code.

Run by ``make check-docs`` and by CI. It exists because documentation rots in ways tests do
not catch: a renamed test, a moved file, a diagram that stopped parsing, a count that drifted.
Each check here corresponds to a mistake that has actually happened in this repository.

Mermaid syntax is deliberately *not* checked here — that needs a JavaScript runtime, and it is
covered by the console workflow instead. What is checked is everything reachable from Python.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PUBLIC_DOCS = [
    # `docs/**` covers the audits under docs/audits/ as well.
    *sorted((REPO_ROOT / "docs").rglob("*.md")),
    REPO_ROOT / "README.md",
    REPO_ROOT / "SECURITY.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "CODE_OF_CONDUCT.md",
    REPO_ROOT / "CHANGELOG.md",
]

#: Phrases that were true of an earlier design and are not true now. Each one is a claim this
#: repository actually made and had to correct, so the check exists to stop it coming back —
#: in a new document, in a rewritten paragraph, or in a copy-paste from the old text.
#:
#: The pattern is what to refuse; the note says what to write instead.
RETIRED_CLAIMS: tuple[tuple[str, str], ...] = (
    (
        r"rather than buffered",
        "the response bound is streaming; say so rather than reusing the old test name",
    ),
    (
        r"digest[^.\n]{0,80}\bauthenticat",
        "a digest identifies content; the signature authenticates the producer",
    ),
    (
        r"self-verifying",
        "a document cannot verify itself; say digest-verified, or signed by a trusted key",
    ),
    (
        r"statically reviewed, not executed",
        "the Docker topology is executed in CI; update the verification status",
    ),
    (
        # Only an affirmative claim. "not production-ready" and "is not a production system"
        # are the statements this repository does make, and refusing those too would push
        # someone to delete the disclaimer rather than fix a claim.
        r"(?<!not )(?<!never )\bproduction[- ]ready\b",
        "this project does not claim production readiness",
    ),
)

#: Words that mean a document was left unfinished. `description_origin` legitimately uses the
#: word "placeholder" in prose, so the check looks for the markup form.
UNFINISHED = (
    r"\[GIF placeholder",
    r"\[screenshot placeholder",
    r"\bTODO\b",
    r"\bTBD\b",
    r"\bFIXME\b",
    r"_\[.*placeholder.*\]_",
)


class Failure(Exception):
    pass


def _fail(problems: list[str], message: str) -> None:
    problems.append(message)


def check_links(problems: list[str]) -> None:
    """Every relative link and image path must resolve."""
    for doc in PUBLIC_DOCS:
        if not doc.exists():
            _fail(problems, f"missing document: {doc.relative_to(REPO_ROOT)}")
            continue
        for link in re.findall(r"\]\((?!https?:|mailto:)([^)#]+)", doc.read_text(encoding="utf-8")):
            if not ((doc.parent / link).exists() or (REPO_ROOT / link).exists()):
                _fail(problems, f"{doc.relative_to(REPO_ROOT)}: broken link → {link}")


def check_referenced_paths(problems: list[str]) -> None:
    """A backticked repository path must exist."""
    pattern = re.compile(r"`((?:apps|packages|tests|docs|scripts|examples|docker)/[\w./-]+)`")
    for doc in PUBLIC_DOCS:
        if not doc.exists():
            continue
        for path in pattern.findall(doc.read_text(encoding="utf-8")):
            if not (REPO_ROOT / path.rstrip("/")).exists():
                _fail(problems, f"{doc.relative_to(REPO_ROOT)}: path does not exist → {path}")


def check_named_tests(problems: list[str]) -> None:
    """A test named in the documentation must exist.

    The README's security table cites tests by name as evidence. A renamed test would turn
    that evidence into a claim, which is exactly what this project says it does not do.
    """
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (REPO_ROOT / "tests").rglob("*.py")
    )
    for doc in PUBLIC_DOCS:
        # An audit records what a test was called at the version it describes. Requiring those
        # names to still exist would mean either never renaming a test or rewriting history.
        if not doc.exists() or "audits/" in doc.as_posix():
            continue
        for name in set(
            re.findall(r"`(Test[A-Za-z0-9_]+|test_[a-z0-9_]+)`", doc.read_text("utf-8"))
        ):
            if name not in sources:
                _fail(problems, f"{doc.relative_to(REPO_ROOT)}: no such test → {name}")


def check_make_targets(problems: list[str]) -> None:
    """A `make` command the documentation gives must be a real target."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z][\w-]*):", makefile, re.M))
    for doc in PUBLIC_DOCS:
        if not doc.exists():
            continue
        # Only inside backticks or at the start of a fenced command, so that ordinary prose
        # like "make the artifact verifiable" is not read as a target reference.
        text = doc.read_text(encoding="utf-8")
        referenced = set(re.findall(r"`make ([a-z][\w-]*)[^`]*`", text))
        referenced |= set(re.findall(r"^\s*make ([a-z][\w-]*)\s*$", text, re.M))
        for target in referenced:
            if target not in targets:
                _fail(problems, f"{doc.relative_to(REPO_ROOT)}: no such make target → {target}")


def check_unfinished(problems: list[str]) -> None:
    """No document may still carry placeholder markup."""
    for doc in PUBLIC_DOCS:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        for pattern in UNFINISHED:
            # Case-sensitive: "TODO" is a marker, but "a trivial todo example" is prose.
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                _fail(
                    problems,
                    f"{doc.relative_to(REPO_ROOT)}:{line}: unfinished marker → {match.group()}",
                )


def _console_test_count() -> int:
    """Count the console's test cases by reading its test files.

    Counted rather than executed: running Vitest needs Node and an installed
    ``node_modules``, which this check cannot assume. ``it(`` at the start of a statement is
    the only form the console's tests use, and the console workflow runs them for real — so
    a miscount here would show up as a mismatch between two numbers rather than as a silent
    wrong answer.
    """
    root = REPO_ROOT / "apps" / "control-plane" / "frontend" / "src"
    total = 0
    for path in sorted(root.rglob("*.test.tsx")) + sorted(root.rglob("*.test.ts")):
        total += len(re.findall(r"^\s*it\(", path.read_text(encoding="utf-8"), re.M))
    return total


def check_test_count(problems: list[str]) -> None:
    """Every claimed test count anywhere in the documentation must match reality.

    Counts are derived here and compared against every document, rather than maintained by
    hand in several places. The repository previously carried 185 in two documents and 186
    in three others, which is what a hand-maintained number does over time.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--no-header", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Checked, because a failing suite still prints an "N passed" line for the tests that
        # did pass — and reading N off a red run would report a count nobody should trust.
        _fail(problems, "the test suite does not pass, so its count cannot be verified")
        return
    match = re.search(r"(\d+) passed", result.stdout + result.stderr)
    if not match:
        _fail(problems, "could not determine the test count; the suite did not report a pass line")
        return

    python_tests = int(match.group(1))
    console_tests = _console_test_count()
    combined = python_tests + console_tests

    for doc in PUBLIC_DOCS:
        # A versioned audit records what was true when it was written. Holding it to today's
        # numbers would either force rewriting history or delete the record.
        if not doc.exists() or "audits/" in doc.as_posix():
            continue
        text = doc.read_text(encoding="utf-8")
        relative = doc.relative_to(REPO_ROOT)

        for claimed in re.findall(r"(\d+) Python tests", text):
            if int(claimed) != python_tests:
                _fail(
                    problems,
                    f"{relative}: claims {claimed} Python tests, the suite runs {python_tests}",
                )
        for claimed in re.findall(r"(\d+) console tests", text):
            if int(claimed) != console_tests:
                _fail(
                    problems,
                    f"{relative}: claims {claimed} console tests, there are {console_tests}",
                )
        for claimed in re.findall(r"tests-(\d+)%20passing", text):
            if int(claimed) != combined:
                _fail(
                    problems,
                    f"{relative}: the test badge claims {claimed}, the real total is "
                    f"{combined} ({python_tests} Python + {console_tests} console)",
                )

    # Per-suite counts, wherever a document breaks them down. Derived the same way, so a new
    # test file cannot leave a stale subtotal behind.
    for suite in ("unit", "contract", "integration", "security", "e2e"):
        collected = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"tests/{suite}",
                "--collect-only",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        counted = sum(int(n) for n in re.findall(rf"tests/{suite}/\S+: (\d+)", collected.stdout))
        if not counted:
            continue
        for doc in PUBLIC_DOCS:
            if not doc.exists() or "audits/" in doc.as_posix():
                continue
            text = doc.read_text(encoding="utf-8")
            for claimed in re.findall(rf"`tests/{suite}`\s*\|\s*(\d+)\s*\|", text):
                if int(claimed) != counted:
                    _fail(
                        problems,
                        f"{doc.relative_to(REPO_ROOT)}: claims {claimed} tests in "
                        f"tests/{suite}, there are {counted}",
                    )


def check_retired_claims(problems: list[str]) -> None:
    """No document may reintroduce a claim this repository has already had to correct."""
    for doc in PUBLIC_DOCS:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        for pattern, note in RETIRED_CLAIMS:
            for match in re.finditer(pattern, text, re.I):
                # The audit documents quote the old wording when recording what was fixed.
                # Quoting a corrected claim is the opposite of making it.
                if "audits/" in str(doc):
                    continue
                line = text[: match.start()].count("\n") + 1
                _fail(
                    problems,
                    f"{doc.relative_to(REPO_ROOT)}:{line}: retired claim "
                    f"{match.group()!r} — {note}",
                )


def check_captured_assets(problems: list[str]) -> None:
    """Every image the README shows must be a committed asset."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"!\[[^\]]*\]\((docs/assets/[^)]+)\)", readme))
    if not referenced:
        _fail(problems, "the README references no captured assets")
    for link in referenced:
        if not (REPO_ROOT / link).exists():
            _fail(problems, f"README: missing asset → {link}")

    for image in sorted((REPO_ROOT / "docs" / "assets").glob("*.png")):
        relative = f"docs/assets/{image.name}"
        if relative not in referenced and relative not in readme:
            _fail(problems, f"unused captured asset → {relative}")


def check_alt_text(problems: list[str]) -> None:
    """Every captured image needs alt text, so the README is readable without images."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for alt, link in re.findall(r"!\[([^\]]*)\]\((docs/assets/[^)]+)\)", readme):
        if len(" ".join(alt.split())) < 40:
            _fail(problems, f"README: alt text too thin for {link}")


def main() -> int:
    problems: list[str] = []
    checks = (
        check_links,
        check_referenced_paths,
        check_named_tests,
        check_make_targets,
        check_unfinished,
        check_captured_assets,
        check_alt_text,
        check_retired_claims,
        check_test_count,
    )
    for check in checks:
        check(problems)

    if problems:
        print(f"{len(problems)} documentation problem(s):\n", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(f"documentation checks passed ({len(checks)} checks over {len(PUBLIC_DOCS)} documents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
