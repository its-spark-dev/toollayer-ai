# ADR 0009 — Clean-room implementation

**Status:** Accepted · **Date:** 2026-08-05

## Context

The author previously worked on a system in this problem domain: turning API descriptions into
governed tools for an LLM runtime. That work is employer-owned and not publishable.

Working in a domain twice is normal and legitimate. Republishing someone else's code is not.
The two need a line between them that is drawn *before* any code is written, and that a
reviewer can inspect afterwards.

Authorship of a commit in a private repository is **not** proof of the right to publish it. The
default assumption is that the employer owns the work product.

## Decision

Adopt a **behavioral-specification-first** method:

1. Identify a responsibility in the abstract.
2. Write a specification for it — inputs, outputs, invariants, failure cases. What must be
   true, never how it was done before.
3. Implement against the specification, with this project's own decomposition and naming.
4. Test against the specification, not against an implementation.

Similarity to prior art is expressed through **responsibilities, data flow, architectural
boundaries, public standards, interface design, and testable behavior** — never through
implementation detail.

Deliberate divergences, recorded so the independence is demonstrable rather than asserted:
newly authored contracts with a `contract_version` envelope; generic roles instead of
organizational attributes; wider method and request-body coverage; two provider adapters
instead of one; ORM persistence with a new schema; a deterministic offline provider; English
throughout.

## Consequences

**Good.** The repository is publishable. The method is inspectable in
`docs/CLEAN_ROOM_PLAN.md`. The divergences make some parts genuinely better — two adapters
make provider neutrality testable, and request-body support widens what the converter covers.

**Bad.** Everything was written from scratch, which is slower. Some solved problems were solved
again. A reader cannot verify the claim by comparison, because the thing to compare against is
not public.

**Accepted.** The claim rests on the method recorded in [`docs/CLEAN_ROOM_PLAN.md`](../CLEAN_ROOM_PLAN.md),
which was committed before the implementation began, and on a scan of the working tree and
every commit on every ref for internal identifiers before publication, which found none. It is
stated as a claim, not proven: what it would be proven against is not public.

## Enforcement

- No file from any private repository is copied, renamed, lightly edited, or translated.
- No Git history is transferred.
- Confidential identifiers are enumerated and scanned for across the tree and all history.
- A human review is required before the repository becomes public. Automation cannot discharge
  that responsibility.
