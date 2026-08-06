# ADR 0004 — Immutable published versions and deployment snapshots

**Status:** Accepted · **Date:** 2026-08-05

## Context

A runtime executes tools with real effects. When something goes wrong, the first question is
"what exactly was it running?" — and that question needs an answer that is not "whatever the
configuration said at the time".

Mutable configuration cannot answer it. Neither can a mutable "latest version" pointer.

## Decision

Two immutable artifacts.

A **published version** is written once. Only `disabled_at` may change afterwards, which is why
its digest still verifies after disablement. Versions must strictly increase.

A **deployment snapshot** pins exactly one published version per connector and is never edited.
A change creates revision N+1 and deactivates N; N stays queryable and byte-identical.

Both carry a SHA-256 over their canonical serialization, and consumers recompute it. Deployment
snapshots additionally carry an Ed25519 producer signature over the canonical bytes that include
that digest.
Serialization is pinned — sorted keys, no insignificant whitespace, no non-finite numbers — so
two processes serializing the same logical document produce the same bytes.

Uniqueness is enforced by **database constraint**, not application check.

## Consequences

**Good.** "What was it running?" has an exact answer: a revision, an identifier, a digest.
Content integrity is checkable by the consumer. Producer authenticity is a separate claim and
needs the signature: a digest is asserted by whoever wrote the document, and an attacker who can
rewrite the document can rewrite the digest with it. A runtime
mid-request cannot have its tools change underneath it. Rollback is publishing an earlier
snapshot's selections, not an `UPDATE`.

**Bad.** More rows: every change is a new artifact. A fix requires a new version even when it
is a typo in a description. Storage grows monotonically with no retention policy implemented.

**Accepted.** Disablement is not immediate revocation — a runtime holding an older snapshot
serves it until its next refresh. Immediate revocation would need a push channel, which is the
coupling ADR 0002 avoids. Recorded in `docs/threat-model.md` §7.

## Alternatives

- **Mutable versions.** Cheaper; makes the artifact unverifiable and the incident question
  unanswerable.
- **A "latest" pointer instead of snapshots.** Removes the ability to pin, and makes every
  publish a production change.
- **Content-addressed storage only, no revisions.** Loses the human-legible ordering a
  deployment needs.
