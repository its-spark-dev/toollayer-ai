# ADR 0001 — One repository for two services

**Status:** Accepted · **Date:** 2026-08-05

## Context

The project has two independently deployable services, four shared packages, a synthetic
upstream API, and a console. They could live in separate repositories or in one.

The two services must not become coupled — that boundary is the point of the design. So the
question is whether one repository makes coupling easier to introduce than it prevents.

## Decision

One repository, with explicit internal boundaries:

```
apps/       control-plane, runtime, demo-api        — deployables
packages/   contracts, openapi-converter,           — libraries
            policy-engine, mock-llm
```

The rule that keeps this honest: **`control_plane` and `runtime_service` never import each
other.** They may import the shared packages; the shared packages may not import them.

## Consequences

**Good.** One clone, one `make setup`, one CI run. A contract change and both consumers move
in one commit, which is what makes `tests/contract` able to catch drift at all. A reviewer
sees the whole system without cross-referencing repositories.

**Bad.** Nothing stops a careless import except review and the tests. Separate repositories
would enforce the boundary mechanically. CI runs everything on every change.

**Mitigation.** The contract tests fail on drift, and `docs/architecture.md` states the rule
where a contributor will find it. For a project this size that is proportionate; at ten
services it would not be.

## Alternatives

- **Two repositories.** Mechanically enforces the boundary; makes coordinated contract changes
  a two-repository dance with a version-pinning step. Rejected: the coordination cost is real
  and immediate, the coupling risk is hypothetical and reviewable.
- **A repository per package.** All the cost, no additional benefit at this scale.
