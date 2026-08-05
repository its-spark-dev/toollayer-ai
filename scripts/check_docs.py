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
    *sorted((REPO_ROOT / "docs").rglob("*.md")),
    REPO_ROOT / "README.md",
    REPO_ROOT / "SECURITY.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "CODE_OF_CONDUCT.md",
    REPO_ROOT / "PRE_PUBLICATION_REVIEW.md",
]

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
        if not doc.exists():
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


def check_test_count(problems: list[str]) -> None:
    """A claimed test count must match what the suite actually runs."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--no-header", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    match = re.search(r"(\d+) passed", result.stdout + result.stderr)
    if not match:
        _fail(problems, "could not determine the test count; the suite did not report a pass line")
        return
    actual = int(match.group(1))

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for claimed in re.findall(r"make test\s+#\s*(\d+) Python tests", readme):
        if int(claimed) != actual:
            _fail(problems, f"README claims {claimed} Python tests, the suite runs {actual}")
    for claimed in re.findall(r"tests-(\d+)%20passing", readme):
        # The badge counts the console tests too, which this process does not run.
        if int(claimed) < actual:
            _fail(problems, f"the test badge claims {claimed}, below the {actual} Python tests")


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
