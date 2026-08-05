# ADR 0002 — Separate the Control Plane from the Runtime

**Status:** Accepted · **Date:** 2026-08-05

## Context

Turning an API description into a governed tool, and executing a tool call, are both part of
"letting an AI application use an API". One service could do both.

But the two have different threat models and different tempos. Configuration time is slow,
human-supervised, auditable, and trusted. Request time is fast, driven by model output, and
adversarial. A single service would apply one set of assumptions to both.

## Decision

Two services with one direction of dependency.

The **Control Plane** owns configuration time: what a tool *is*. It never processes a user
request and never calls an upstream API.

The **Runtime** owns request time: whether *this* call, by *this* caller, may proceed. It never
authors or publishes anything.

They communicate through exactly one read-only, versioned HTTP endpoint that serves an
immutable, digest-verified snapshot. No shared database, no callback, no push.

## Consequences

**Good.** A compromised runtime cannot publish a tool — the only endpoint it can reach is
read-only. The runtime scales horizontally without touching the authoring path. Either side
can be rebuilt in another language against the published schemas. Failure domains are separate:
the control plane being down does not stop the runtime serving.

**Bad.** Two deployables, two configurations. A published change is not instantly visible to a
running runtime. Some logic — policy evaluation — lives in a shared package so both sides can
use it, which is a third thing to version.

**Accepted.** Eventual consistency bounded by the refresh interval. `docs/architecture.md` §6
states the consequences, including that disablement is not immediate revocation.

## Alternatives

- **One service.** Simpler to deploy; means the component that accepts model-driven requests
  also holds the credential that can publish tools. Rejected on that basis alone.
- **A shared database instead of an API.** Removes the versioned contract, and with it the
  ability to evolve either side independently or to verify what the runtime received.
