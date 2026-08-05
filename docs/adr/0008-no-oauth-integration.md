# ADR 0008 — No real OAuth or credential management

**Status:** Accepted · **Date:** 2026-08-05

## Context

A tool that calls a real API usually needs a credential. A complete system would manage OAuth
application configuration, per-user delegated grants, encrypted token storage, and refresh.

That is a large subsystem, and it is one that cannot be demonstrated honestly offline: a fake
OAuth flow demonstrates nothing about a real one, and a real one needs a provider account, a
registered application, a callback URL, and secrets.

## Decision

Omit it, and say so.

A connector carries `auth_profile_ref` — an **opaque name**, never a secret. It names a profile
the runtime would resolve locally. In this repository nothing resolves it, because the demo API
needs no credential.

The contracts have no field that could hold a secret. That is structural: there is no column,
no schema property, and no code path where a token could be stored in a published artifact even
by mistake.

## Consequences

**Good.** No secret can leak through a published artifact, because none can be put there. The
demo runs with no account. The seam where credential resolution belongs is visible and named.
The project is not pretending to have solved a problem it has not.

**Bad.** The project cannot call a real third-party API as shipped. A reviewer looking for
OAuth experience will not find it here.

**Accepted and documented.** `docs/feature-parity.md` lists this as **intentionally omitted**
with the reason. Claiming a mocked OAuth flow as a feature would be the dishonest option.

## What would be needed

If credentials were added: a credential broker service; short-lived leases rather than stored
tokens on the execution path; encryption at rest with a managed key; per-user grant records
separate from application configuration; and a rule that a connector definition still never
carries a secret — the resolution stays runtime-local. The `auth_profile_ref` seam is where
that attaches.
