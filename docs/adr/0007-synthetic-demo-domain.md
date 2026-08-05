# ADR 0007 — A synthetic support-ticket domain

**Status:** Accepted · **Date:** 2026-08-05

## Context

The project needs an API to convert and call. It could use a public third-party API, a
generic "todo" example, or something invented.

A public API would add an API key, network dependency, rate limits, and terms of service to a
project whose demo is meant to run offline in minutes. A trivial todo example would not
exercise filtering, state transitions, or role-based access — so the governance would have
nothing to govern.

There is a further constraint: this repository is a clean-room implementation, and the demo
domain must be unrelated to any system the author has worked on. `docs/CLEAN_ROOM_PLAN.md`
records that boundary.

## Decision

Invent **Support Ticket Management**, entirely synthetic, and implement it as
`apps/demo-api` with a hand-authored OpenAPI document at `examples/support-api.openapi.yaml`.

Six operations: list with five filters, retrieve, assign, change status, list teams, list team
members. Read and write, path and query parameters, JSON request bodies, enums, bounds, and a
business rule that produces a genuine `409`.

Everything — every ticket, team, and person — is fabricated. Names are deliberately generic:
Example Organization, Demo Workspace, Sample Support API.

## Consequences

**Good.** Understandable without domain explanation. Rich enough that filtering, state
mutation, and role restriction are all natural rather than contrived — restricting "change
ticket status" to a lead is obviously sensible, which makes the authorization demo land. Runs
offline with no account. Unambiguously unrelated to any prior work.

**Bad.** It is a toy. No pagination beyond a limit, no authentication, no realistic error
surface. A reviewer might read the demo API as representative of the project's engineering
depth rather than as a fixture.

**Mitigation.** The demo API is deliberately the least interesting code in the repository, and
`docs/PORTFOLIO_STRATEGY.md` says where the attention should go.

## Note on the OpenAPI document

It is hand-authored rather than generated from the FastAPI app. The document is the *input* to
the pipeline, so writing it by hand exercises the converter against a specification a person
wrote — with the `$ref` reuse, descriptions, and optionality a real one has — instead of
against a serialization of the code it describes.

One seeded ticket (`TKT-1007`) carries a prompt-injection payload in its body. That is on
purpose: `tests/security` uses it to prove upstream content is treated as data.
