# ADR 0006 — Default-deny execution policy

**Status:** Accepted · **Date:** 2026-08-05

## Context

The runtime makes outbound HTTP requests, with arguments derived from model output, from inside
a trust boundary. That is a server-side request forgery primitive unless something constrains
it.

A connector's `base_url` is reviewed configuration. It is not authorization: it says where an
administrator intended the tools to go, and it is stored data that could be wrong or tampered
with.

## Decision

Default deny, layered, and configured per deployment rather than per connector:

1. An **empty allowlist permits nothing**. There is no wildcard.
2. **Exact origin matching** — scheme, host, and port. Not suffix matching, because
   `notexample.org.attacker.test` passes `endswith(".example.org")`.
3. **Post-resolution address checks** — the host is resolved and *every* returned address is
   checked. Link-local is refused unconditionally; no setting enables it.
4. **No redirects**, disabled at the client so no code path can forget.
5. **Finite bounds** on connect timeout, read timeout, and response size, validated at
   construction.

Two independent statements — the connector's `base_url` and the deployment's allowlist — must
agree before a request leaves the process.

## Consequences

**Good.** A compromised stored `base_url` is not sufficient to redirect traffic. A public
hostname resolving to cloud metadata is refused. Every bound is finite, so a slow or enormous
upstream cannot hold a worker open. Nine tests in `tests/security` demonstrate the refusals.

**Bad.** Every deployment must configure its allowlist; nothing works out of the box. A
legitimate upstream that changes its address needs a configuration change. Resolving before
connecting adds a lookup.

**Accepted.** Local development genuinely calls `localhost`, so three escape hatches exist —
plaintext HTTP, loopback, private addresses. All three default to off, `/healthz` reports when
any is on, and a test proves loopback is refused when the flag is off. There is a
time-of-check-to-time-of-use gap between resolution and connection; closing it fully needs a
pinned-IP transport, which is noted as a limitation rather than claimed.

## Alternatives

- **Allow by default, deny known-bad.** Requires enumerating every internal range and every
  metadata endpoint, forever. Wrong default.
- **Trust the connector's `base_url` alone.** Makes stored data the authorization decision.
- **Egress filtering at the network layer only.** Good defense in depth, unavailable in a
  local demo, and it cannot produce a useful error message.
