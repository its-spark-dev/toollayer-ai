# Clean-Room Plan

ToolLayer AI is an independent portfolio implementation built from first principles using
public standards and general software architecture patterns.

It does not contain proprietary source code, internal assets, confidential data, or private
infrastructure configuration from any employer.

This document records *how* that claim is kept true, so a reviewer can judge the process and
not only the result.

---

## 1. Why this document exists

The author previously worked on a system in the same problem space: turning API descriptions
into governed tools that an LLM runtime can execute. That experience shaped the *problem
understanding* behind this repository. It did not supply any of its content.

Working in a domain twice is normal and legitimate. Republishing someone else's code is not.
The clean-room method below is the line between the two, and this document states where that
line was drawn before any code was written.

---

## 2. Methodology

The project follows a **behavioral-specification-first** rule:

1. **Identify a responsibility**, in the abstract — for example, "convert one OpenAPI
   operation into a provider-neutral tool definition".
2. **Write a behavioral specification** for it: inputs, outputs, invariants, and the exact set
   of failure cases. The specification describes *what must be true*, never *how it was done
   before*.
3. **Implement against the specification**, choosing this project's own decomposition, naming,
   data model, and error taxonomy.
4. **Write tests against the specification**, not against an implementation.

Where a specification derives from general prior experience, the resulting design is
constrained to concepts that are:

- **generic** — they appear in public standards or in published architecture literature;
- **independently implementable** — a competent engineer could derive them from the standards;
- **portfolio-relevant** — they demonstrate transferable engineering skill;
- **non-confidential** — they encode no organization-specific policy, data, or infrastructure.

Similarity to prior art in this repository is therefore expressed through **responsibilities,
data flow, architectural boundaries, public standards, interface design, and testable
behavior** — and never through implementation detail.

---

## 3. Acceptable sources

- The **OpenAPI Specification** 3.0 and 3.1.
- **JSON Schema** Draft 2020-12.
- **RFC 6901** (JSON Pointer), **RFC 3339** (timestamps), **RFC 7807**-style problem
  reporting concepts, **RFC 9110** (HTTP semantics, conditional requests, ETag).
- **Semantic Versioning 2.0.0**.
- Publicly documented model-provider tool-calling formats, used only as *output adapter*
  targets.
- Public documentation for FastAPI, Pydantic, SQLAlchemy, Alembic, React, and Vite.
- General, widely published architecture patterns: control plane / data plane separation,
  immutable artifacts, content-addressed digests, allowlisting, default-deny authorization.
- The author's own general engineering knowledge.

## 4. Prohibited sources

Nothing from any private or employer-owned repository may enter this project, including:

- source code, in whole, in part, renamed, translated, or lightly edited;
- internal JSON Schemas, database schemas, or migration files;
- internal API specifications and OpenAPI documents;
- UI components, styling, copy, or assets;
- internal terminology, product names, project names, or code names;
- test fixtures, seed data, personas, logs, or evaluation cases;
- deployment configuration, CI pipelines, container registries, hostnames, or addresses;
- architecture documents, ADRs, or runbooks;
- Git history, commit messages, or branch names.

Authorship of a commit in a private repository is **not** proof of the right to publish it.
The default assumption is that the employer owns the work product, and the default action is
independent reimplementation.

---

## 5. Architectural concepts intentionally generalized

These concepts are general to the problem domain and appear here in a newly designed form:

| Concept | How it is generalized here |
|---|---|
| Control plane / runtime separation | Two independently deployable services that never import each other's modules |
| API-description ingestion | OpenAPI 3.0/3.1 JSON or YAML upload, with the source bytes and digest retained |
| Provider-neutral tool contract | A project-defined canonical schema, explicitly **not** an industry standard |
| Provider adapters | Output-only projections to two public model-provider formats |
| Draft → published → deployed lifecycle | Mutable draft, immutable published version, immutable deployment snapshot |
| Immutability by digest | Canonical JSON serialization plus SHA-256 |
| Governed execution | Registry-driven, allowlisted, schema-validated, default-deny |
| Untrusted tool output | Tool results are data, never instructions |

## 6. Implementation choices intentionally made differently

To keep the independence of this implementation demonstrable, the following are deliberate,
documented divergences rather than accidents:

1. **Newly authored contracts.** Every field name, schema `$id`, and error code in
   `packages/contracts` was designed for this project, with an explicit `contract_version`
   envelope.
2. **Roles instead of organizational attributes.** Access policy uses generic synthetic roles
   (`support-agent`, `support-lead`, `auditor`), not organizational hierarchy attributes.
3. **Wider conversion coverage.** The converter supports `GET`, `POST`, `PUT`, `PATCH`, and
   `DELETE`, including JSON request bodies.
4. **Two provider adapters.** Provider neutrality is a testable claim only when more than one
   adapter exists, so both an OpenAI-compatible and an Anthropic-compatible adapter ship here.
5. **ORM persistence.** SQLAlchemy models on SQLite (default) or PostgreSQL, with a single
   newly authored initial migration.
6. **Deterministic offline model provider.** The default provider is a rule-based
   `MockLLMProvider`, so the full demo and test suite run with no API key.
7. **English throughout.** All code, documentation, UI copy, and messages are in English.
8. **Local-only deployment.** Docker Compose for local development; no production topology is
   described or implied.

---

## 7. Confidentiality boundaries

The following must never appear in this repository, in any file or any commit:

employer or organization names · internal product, project, or code names · employee names or
email addresses · internal Git hosting URLs · container registry URLs · hostnames · IP
addresses · VPN details · OAuth client identifiers, secrets, or callback URLs · tenant
identifiers · certificates or keys · internal environment values · private API endpoints or
specifications · internal database schemas or migration identifiers · real logs · real user or
organization data · private test fixtures · customer information · internal deployment
topology · organization-specific role or permission names · proprietary UI assets · original
screenshots · original Git history.

The demonstration domain is fully synthetic. Names used throughout are deliberately generic:
**Example Organization**, **Demo Workspace**, **Sample Support API**, **Synthetic Task API**,
**Tool Control Plane**, **LLM Orchestration Runtime**.

## 8. Public demo domain

The demo domain is **Support Ticket Management**, invented for this repository. It was chosen
because it is:

- instantly understandable without domain explanation;
- rich enough to show filtering, retrieval, and state mutation, so read and write policies both
  matter;
- naturally role-shaped, so authorization is meaningful rather than decorative;
- unrelated to any real system the author has worked on.

All tickets, teams, and people in `apps/demo-api` are fabricated.

## 9. Reuse decision process

Before any file from any prior private work could be reused directly, **all** of the following
would have to hold:

1. the author explicitly confirms the exact file, and
2. the file is covered by a public license permitting reuse, **or** the employer has
   authorized publication of that specific material.

Where authorization is unclear, the file is not copied, not renamed, not lightly sanitized, not
translated, and its history is not transferred. Its responsibility is studied, a behavioral
specification is written, and an independent equivalent is implemented.

**Status for this repository: no direct reuse was requested, evaluated as acceptable, or
performed. Every component was independently implemented.**

## 10. Publication risks and how they are handled

| Risk | Handling |
|---|---|
| Accidental inclusion of an internal identifier | Automated scan over the working tree *and* full Git history before publication; results recorded in `docs/audits/v0.1.0-pre-publication.md` |
| Committed secrets | Secret scan across all commits; `.env.example` contains only placeholders |
| Dependency licensing | Dependency license inventory reviewed before publication |
| Structural similarity being mistaken for copying | This document, `docs/feature-parity.md`, and the ADRs state the boundary explicitly |
| Overstated claims | Limitations are stated in the README, the architecture document, and the case study |
| Private notes leaking | `private-notes/` is in `.gitignore` and its absence is verified in the pre-publication review |

## 11. Human review requirements

Automation cannot discharge this responsibility. Before the repository was made public, these
were confirmed by hand:

1. no employer-confidential material is present in the tree or in history;
2. the README's claims match what the code does;
3. the limitations sections are honest;
4. the license choice is appropriate;
5. no commit carries a co-author or AI-attribution trailer;
6. `private-notes/` is absent from every commit;
7. publishing is acceptable given the author's employment agreement.

That review was completed before publication. The list is kept because it records what the method required, not because anything remains outstanding.
