# ADR 0003 — A project-defined canonical representation, with provider adapters

**Status:** Accepted · **Date:** 2026-08-05

## Context

Every model provider defines its own tool-calling format, and the formats differ in ways that
matter: how optionality is expressed, which JSON Schema keywords are honored, what the size
limits are.

Three options: store OpenAPI and convert at request time; store one provider's format; or
define an internal representation and project outward.

Storing OpenAPI would put parsing on the request path and would carry a document with no place
for the things review adds — policy, provenance, role access. Storing a provider's format
would make that provider's constraints the project's constraints permanently.

## Decision

Define a canonical representation that is the source of truth, and treat every provider format
as an **output projection** produced on demand and never persisted.

Ship **two** adapters — OpenAI-compatible and Anthropic-compatible. One would prove nothing: a
single adapter could mean the canonical format is that provider's format renamed.

Document explicitly that the representation is project-defined and not an industry standard.

## Consequences

**Good.** Review, policy, and provenance have a home. Adding a provider is one adapter, not a
migration. Projection failures isolate per tool. The neutrality claim is testable, and
`tests/contract` tests it.

**Bad.** A third format to maintain and version. Every provider needs an adapter written and
kept current. Projection is not free at request time (it is cached per snapshot load).

**Accepted, and documented.** Perfect portability is not achievable. The OpenAI strict-mode
projection performs one **controlled normalization** — widening optional properties to accept
`null` — which is not lossless. It is safe only because the runtime reverses it and validates
against the *unmodified* canonical schema. `docs/contracts.md` §5 states this in full.

## Alternatives

- **Emit a provider format directly.** Cheapest today, and it makes the project a client of
  one vendor's schema decisions forever.
- **Store OpenAPI and convert per request.** Puts parsing on the hot path and leaves nowhere to
  record what review decided.
