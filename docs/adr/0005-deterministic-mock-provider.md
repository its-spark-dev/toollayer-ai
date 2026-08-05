# ADR 0005 — A deterministic provider is the default

**Status:** Accepted · **Date:** 2026-08-05

## Context

The runtime needs something to choose a tool and propose arguments. A real model provider would
be the obvious choice — and would make every demonstration and every test depend on an API key,
a network call, and a sampled output.

That matters most exactly where it hurts most. A test asserting "an injected instruction does
not cause a second tool call" is only meaningful if the component behaves the same way every
run. Against a real model, the same test is one sample of a distribution.

## Decision

Ship `MockLLMProvider` as the default: rule-based, offline, deterministic. It implements the
same three-method interface a real provider would (`select_tool`, `generate_arguments`,
`format_response`), so a real provider drops into the same seam.

Document plainly that it is **not a model and does not pretend to be one**.

Every test and the entire demonstration run against it. No API key, no network egress.

## Consequences

**Good.** The security tests are assertions rather than samples. CI is hermetic and fast.
Anyone can clone and run the demo in minutes with no account. The demo output is identical
every time, which is what makes it usable as a smoke test — `scripts/demo.py` exits non-zero if
any control fails to reject.

**Bad.** The demo cannot show a real model's reasoning. The provider's scoring is a heuristic
that would not survive a broad API; it is fitted to demonstrating the *pipeline*, not to being
good at tool selection.

**Accepted.** That trade is the right way round for this project: the interesting engineering
is the validation, authorization, and execution boundary, and none of it needs a model to be
demonstrated. The provider seam is where a real model goes, and it is documented as such.

## Alternatives

- **A real provider by default.** Better demo, worse tests, and a barrier to anyone trying the
  project.
- **Recorded fixtures of real responses.** Deterministic, but the recordings drift from the
  live model and the tests then assert something no longer true.
- **No provider; require explicit tool calls only.** Would remove the natural-language step
  entirely, which is the part that makes the governance interesting.
