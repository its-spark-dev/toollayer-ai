# Portfolio Strategy

This document states what ToolLayer AI is trying to prove, to whom, and where the scope stops.
It is public on purpose: a reviewer should be able to see that the project had a thesis before
it had code.

---

## 1. Target roles

- Applied AI Engineer
- AI Backend Engineer
- LLM Platform Engineer
- Backend Software Engineer
- Platform Engineer
- AI Integration Engineer
- Developer Tools Engineer

## 2. Intended audience

| Audience | What they are looking for | Where they will look |
|---|---|---|
| Recruiter / sourcer | A clear one-line story and evidence of real depth | README hero, architecture diagram, screenshots |
| Hiring manager | Judgement, scope control, honesty about limits | `docs/portfolio-case-study.md`, `docs/feature-parity.md`, the ADRs |
| Senior/staff engineer | Boundaries, contracts, failure handling, tests | `docs/architecture.md`, `packages/contracts`, `tests/security`, `tests/contract` |
| Security-minded reviewer | Whether the safety claims are real | `docs/threat-model.md`, `packages/policy-engine`, `tests/security` |

## 3. The 30-second recruiter impression

> This person builds the *platform* that lets AI applications call real APIs safely. They took
> OpenAPI specifications, turned them into governed, versioned, provider-neutral tool
> definitions, and then proved the whole thing works end to end with a runtime that validates,
> authorizes, and executes those tools — with the unsafe cases rejected and tested.

Not: "this person made a chatbot".

## 4. The expected engineering-reviewer impression

A reviewer who reads for ten minutes should conclude:

- The author understands the difference between an **input format** (OpenAPI), an **internal
  source of truth** (the canonical contract), and an **output format** (a provider's tool
  schema) — and keeps them separate.
- The author designs for **immutability and versioning** rather than mutable configuration.
- The author treats **model output as untrusted input**, and puts validation and policy on the
  execution path rather than in the prompt.
- The author knows what a **service boundary** is and enforces it with contracts and tests
  instead of good intentions.
- The author states **limitations** plainly instead of overselling.

## 5. Strongest differentiators

1. **The OpenAPI-to-tool transformation pipeline**, with deterministic conversion and an
   enumerated set of rejections rather than best-effort guessing.
2. **A project-defined canonical contract with two provider adapters**, which makes
   "provider-neutral" a testable claim instead of a slogan.
3. **Immutable published versions and deployment snapshots**, verified by digest.
4. **A default-deny execution boundary** with SSRF protection, allowlisting, and schema
   validation — with security tests that demonstrate the rejections.
5. **A visible governance step**: a human reviews the machine's proposal before anything is
   publishable.
6. **A fully offline demo**: the entire flow, including the model provider, runs with no API
   key and no network egress.

## 6. Features that must be implemented

- OpenAPI 3.0/3.1 ingestion with source-byte preservation and digest.
- Operation discovery, parameter conversion, request-body conversion, JSON Schema generation.
- Tool-name normalization and collision rejection.
- Canonical contract validation with pointer-precise diagnostics.
- Draft lifecycle with optimistic concurrency; publish with semantic versioning; immutable
  published versions.
- Deployments and immutable deployment snapshots.
- An internal snapshot read API with ETag revalidation.
- Runtime snapshot loading, tool discovery, deterministic selection, argument generation,
  JSON Schema validation, policy evaluation, allowlisted execution, structured results.
- Provider adapters for two public tool formats.
- Security controls with tests: unknown tool, unauthorized role, unknown destination, SSRF,
  prompt injection, tool injection, timeout, upstream failure, oversized response.
- An admin UI that makes the transformation legible.
- Docker Compose, Makefile, and a one-command demo.

## 7. Features that may be simplified

- Authentication: static admin and service tokens, not an identity provider.
- Persistence: SQLite by default; PostgreSQL supported by connection string.
- Multi-tenancy: a single organization scope.
- Enrichment: a deterministic rule-based provider rather than a live model.
- Observability: structured logging with redaction, not a metrics/tracing stack.
- Frontend: a focused console over the pipeline, not a full product UI.

## 8. Features intentionally omitted

- Real OAuth flows and end-user credential storage.
- Any connector to a real third-party SaaS API.
- Multi-region, high-availability, or Kubernetes deployment.
- Retrieval-augmented generation, embeddings, vector stores, fine-tuning, or model training.
- A production chatbot product, conversation memory, or streaming chat UX.
- Any claim that the canonical contract is an industry standard.

## 9. Three-minute demonstration flow

1. **0:00** — `make demo`. The synthetic Support API, the Control Plane, and the Runtime start.
2. **0:20** — Register `examples/support-api.openapi.yaml`. Analysis extracts six operations.
3. **0:45** — Open the console. Side by side: the source OpenAPI operation, the generated
   provider-neutral tool definition, the validation diagnostics, and the OpenAI/Anthropic
   adapter output.
4. **1:15** — Review: exclude one operation, edit a description, restrict one write tool to the
   `support-lead` role. Publish `0.1.0`. The version becomes immutable.
5. **1:40** — Create a deployment and a snapshot. The snapshot digest is shown.
6. **2:00** — The Runtime loads the snapshot and lists the tools it may use.
7. **2:10** — Ask *"show me the open high-priority tickets for the billing team"*. The runtime
   selects `list_support_tickets`, generates arguments, validates them against the JSON Schema,
   evaluates policy, calls the synthetic API, and returns a formatted result.
8. **2:40** — Three rejections, each with a distinct structured error: an unauthorized role
   calling the restricted write tool; a request whose text tries to redirect the tool to an
   external host; a fabricated tool name that is not in the snapshot.
9. **3:00** — Point at `tests/security/` and note that each rejection has a test.

## 10. README strategy

Order the README so a reader can stop at any point and still have gained something:

1. Name, tagline, one-sentence description.
2. The problem, in four lines.
3. The architecture diagram.
4. The transformation pipeline, shown as a concrete before/after.
5. Quick start (under ten minutes to a running demo).
6. Control Plane, then Runtime — Control Plane first and longer.
7. Security highlights, each linking to its test.
8. Tech stack, repository layout, test strategy, ADR index.
9. Limitations, stated plainly.
10. Clean-room notice and portfolio context, last.

## 11. Expected interview topics

- Why an internal canonical representation instead of emitting a provider format directly?
- How do you keep two providers' tool formats honest when their semantics differ?
- What exactly is immutable, and what does the digest actually cover?
- Why does the runtime revalidate a snapshot it already holds?
- Where does authorization run, and why there rather than earlier?
- How do you stop model output from becoming an unvalidated request?
- What does the converter refuse to convert, and why is refusing better than guessing?
- How would you scale this: caching, snapshot distribution, multi-tenancy?
- What did you deliberately leave out, and what would you build next?

## 12. Scope controls against overengineering

- Two services, four shared packages. No new top-level component without an ADR.
- No technology enters the stack for keyword value; every dependency must be used by code that
  ships.
- The Runtime stays a reference implementation. If a feature only makes the chat experience
  nicer, it does not belong here.
- Tests target boundaries and failure paths, not a coverage number.
- Documentation depth is spent on architecture and decisions, not on API reference text that
  the OpenAPI document already provides.
