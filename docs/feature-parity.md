# Feature parity

What this repository implements, what it simplifies, what it mocks, and what it leaves out —
measured against the *generalized concept* of an OpenAPI-to-tool control plane with a governed
runtime.

This is a public document. It describes the shape of the problem, not any specific prior
system, and it names no organization, product, or infrastructure.

| Status | Meaning |
|---|---|
| **Implemented** | Built and tested here |
| **Simplified** | Present, deliberately narrower than a full system would need |
| **Mocked** | A deterministic stand-in behind a real interface |
| **Omitted** | Deliberately absent, with a stated reason |
| **Planned** | A named next step, not started |

---

## Control plane

| Capability | Status | Notes |
|---|---|---|
| OpenAPI 3.0/3.1 ingestion, JSON and YAML | **Implemented** | Format chosen by content, not extension |
| Source bytes preserved with a digest | **Implemented** | Everything downstream is reproducible from them |
| Ingestion limits (bytes, depth, node count) | **Implemented** | Enforced before traversal |
| Strict parsing (duplicate keys, BOM, non-finite) | **Implemented** | In both JSON and YAML |
| Same-document `$ref` resolution | **Implemented** | Depth- and cycle-bounded |
| Remote / filesystem `$ref` | **Omitted** | No resolver exists, so SSRF at ingestion is structurally impossible |
| Operation discovery | **Implemented** | GET, POST, PUT, PATCH, DELETE |
| Parameter conversion (path, query) | **Implemented** | With enumerated refusals |
| Header and cookie parameters | **Omitted** | Not exposed as model-supplied arguments |
| JSON request-body conversion | **Implemented** | One nested `body` argument, one binding per field |
| Non-JSON request bodies | **Omitted** | The executor sends JSON |
| Composed schemas (`oneOf`/`allOf`/`anyOf`) | **Omitted** | Refused rather than approximated |
| Tool-name normalization and derivation | **Implemented** | Deterministic; length-bounded without losing uniqueness |
| Tool-name collision detection | **Implemented** | Reported, never auto-renamed |
| Draft 2020-12 schema generation | **Implemented** | Closed objects only |
| Per-operation diagnostics | **Implemented** | One bad operation does not fail the document |
| Draft lifecycle with optimistic concurrency | **Implemented** | `expected_revision` on every mutation |
| Field provenance (source / generated / assisted / human) | **Implemented** | A `generated` placeholder blocks publication |
| LLM-assisted description enrichment | **Mocked** | The provider seam exists; the default is deterministic. The structural guarantee — enrichment cannot touch execution, policy, or destinations — is real |
| Server-authoritative publication | **Implemented** | Rebuilt from stored state, never from the request body |
| Semantic versioning with monotonic increase | **Implemented** | |
| Immutable published versions | **Implemented** | Digest-identified; enforced by database constraint |
| Version disablement | **Implemented** | Changes availability, not content |
| Deployments and immutable snapshots | **Implemented** | Content-derived identifier, monotonic revisions |
| Snapshot producer signing (Ed25519) | **Implemented** | Signed over the canonical bytes including the digest; `TestSignedSnapshotsAreAccepted` |
| Signing key rotation through a trusted key ring | **Implemented** | Overlapping trust window; `TestKeyRotation` |
| Internal read API with ETag and `304` | **Implemented** | The ETag *is* the digest |
| Provider adapter preview | **Implemented** | Both providers, per published version |
| Review console | **Implemented** | Source operation, tool definition, and both projections side by side |
| Multi-tenancy | **Omitted** | Single organization scope |
| Audit log | **Omitted** | Publication records who and when as ordinary mutable rows. Not tamper-evident, and not described as such |
| Role-based admin access | **Simplified** | One static admin token, not an identity system |
| Alternate storage backends | **Omitted** | One SQL repository; SQLite or PostgreSQL |

## Contracts

| Capability | Status | Notes |
|---|---|---|
| Normative JSON Schemas | **Implemented** | Five documents, Draft 2020-12 |
| Typed in-process models | **Implemented** | Kept in step by contract tests |
| Explicit contract versioning | **Implemented** | Checked on load; major mismatch and newer minor both refused |
| Canonical serialization and digests | **Implemented** | Sorted keys, no insignificant whitespace, no non-finite numbers |
| Standardized error envelope | **Implemented** | One shape, stable codes, one status table |
| OpenAI-compatible adapter | **Implemented** | With one documented, reversible normalization |
| Anthropic-compatible adapter | **Implemented** | Nothing to normalize |
| Contract compatibility tests | **Implemented** | Eleven checks; one caught a real model/schema drift |
| Guaranteed cross-provider portability | **Omitted** | Not achievable; stated rather than implied |

## Runtime

| Capability | Status | Notes |
|---|---|---|
| Snapshot retrieval with conditional requests | **Implemented** | |
| Schema validation and digest verification on load | **Implemented** | Verified, not trusted |
| Immutable in-memory snapshot | **Implemented** | Replaced by reference swap, never mutated |
| Graceful degradation when the control plane is down | **Implemented** | Keeps serving the verified snapshot |
| Role-aware tool discovery | **Implemented** | Same authorization function as execution |
| Tool selection from natural language | **Mocked** | Deterministic provider; a real one implements the same interface |
| Argument generation | **Mocked** | Schema-driven; reports missing rather than inventing |
| JSON Schema argument validation | **Implemented** | Every violation reported; values never echoed |
| Authorization at execution | **Implemented** | After selection, before the outbound call |
| Confirmation enforcement | **Implemented** | Enforced by the runtime, not the client |
| Governed HTTP execution | **Implemented** | See the security table below |
| Untrusted-content marking | **Implemented** | And no loop back into selection |
| Structured errors | **Implemented** | Twelve runtime codes |
| Reference client / CLI | **Implemented** | `toollayer-runtime tools` / `ask` / `run` |
| Real model provider adapter | **Planned** | The seam exists; no adapter ships |
| Multi-step tool chaining | **Omitted** | One tool per request, deliberately |
| Conversation memory, streaming, chat UX | **Omitted** | The runtime is not a chatbot product |
| Response caching | **Omitted** | |

## Security

| Control | Status | Test |
|---|---|---|
| Registry-driven execution (unknown tool rejected) | **Implemented** | `TestUnknownTool` |
| Destination allowlist, exact origin | **Implemented** | `TestDestinationPolicy` |
| HTTP method allowlist | **Implemented** | `test_the_method_allowlist_is_enforced_separately` |
| Default-deny (empty allowlist permits nothing) | **Implemented** | `test_an_empty_allowlist_permits_nothing` |
| JSON Schema argument validation | **Implemented** | `TestArgumentInjection` |
| Undeclared argument rejection | **Implemented** | `TestArgumentInjection` |
| Safe path and query handling | **Implemented** | `test_a_path_argument_cannot_escape_its_segment` |
| Request timeouts | **Implemented** | `test_a_timeout_is_reported_as_a_timeout` |
| Streaming response size limit | **Implemented** | Bounded while reading, proved against a real server: `test_the_client_stops_consuming_after_the_limit_is_crossed` |
| Redirect refusal | **Implemented** | `test_a_redirect_is_a_failure_rather_than_a_hop` |
| SSRF protection, post-resolution | **Implemented** | `test_an_allowlisted_name_resolving_into_private_space_is_refused` |
| Loopback and private address rejection | **Implemented** | `test_loopback_is_refused_when_the_escape_hatch_is_off` |
| Role-based tool visibility | **Implemented** | `TestUnauthorizedToolAccess` |
| Prompt-injection resistance at the policy boundary | **Implemented** | `TestPromptAndToolInjection` |
| Tool-injection rejection | **Implemented** | `TestUnknownTool` |
| Secret and header redaction | **Implemented** | `TestRedaction` |
| Structured error envelopes | **Implemented** | `test_every_failure_uses_the_shared_error_envelope` |
| Safe logging defaults | **Implemented** | Values never echoed into messages |
| Separate admin and service credentials | **Implemented** | Enforced at startup |
| Malformed destination handling | **Implemented** | Every parse failure is `destination_not_allowed`, never a 500; `TestMalformedDestinations` |
| Snapshot producer authentication | **Implemented** | `test_content_and_digest_both_replaced_is_still_refused` |
| One immutable snapshot revision per request | **Implemented** | `TestOneRequestOneRevision` |
| Deep immutability of the loaded tool index | **Implemented** | `TestDeepImmutability` |
| Verified caller identity (signed token) | **Implemented** | `TestVerifiedTokenMode`. Off in the demo topology; `/healthz` reports which mode is in force |
| Identity-provider integration (JWKS, refresh, revocation) | **Omitted** | The verification seam is real; populating it from a live provider is not implemented |
| Rate limiting and quotas | **Omitted** | |
| Admin and service token rotation and expiry | **Omitted** | Static bearer tokens. Snapshot *signing* keys do rotate |
| TLS / mTLS between services | **Omitted** | Signatures authenticate the artifact, not the transport |
| Secret management | **Omitted** | Secrets come from the environment; no vault integration, no encryption at rest |
| Immediate revocation | **Omitted** | Disablement takes effect at the next refresh. Polling, not push |
| Pinned-IP transport (closing the TOCTOU gap) | **Planned** | Resolution and connection are still separate steps. No pinned-IP protection is claimed |

## Developer experience

| Capability | Status |
|---|---|
| `make setup` / `test` / `lint` / `typecheck` / `demo` | **Implemented** |
| Docker Compose for the whole stack | **Implemented** — built and executed in CI |
| Reproducible dependency graph (`uv.lock` + hash-pinned `requirements.lock`) | **Implemented** |
| Wheel build, clean install, and packaged-schema smoke test | **Implemented** |
| Supply-chain scanning (`pip-audit`, `npm audit`, gitleaks over history, CodeQL, SBOM) | **Implemented** |
| Static portfolio page for GitHub Pages | **Implemented** — requires one manual repository setting |
| One-command end-to-end demonstration | **Implemented** |
| CI with lint, format, types, tests, demo, packaging, Docker, and hygiene checks | **Implemented** |
| Offline operation, no API key | **Implemented** |
| Alembic migrations | **Implemented** |
| Metrics and tracing | **Omitted** |
| Multi-tenancy | **Omitted** — a tenant column without isolation would be a claim, not a feature |
| Horizontal snapshot distribution | **Omitted** — the runtime is stateless and would scale; nothing here addresses distribution |
| Real hosted model provider | **Omitted** — the provider is an interface; a hosted one would end the offline guarantee |
| Multi-step tool chaining | **Omitted** — a loop is the structure whose absence the injection tests currently rely on |
| Conversation memory | **Omitted** — the runtime is not a chatbot product |
| Real OAuth and end-user credential brokerage | **Omitted** — a connector carries an opaque `auth_profile_ref` and no secret |

---

## Why the omissions are omissions

Three of them are worth stating directly, because each could look like an oversight:

**Real credential management.** It cannot be demonstrated honestly offline. A mocked OAuth flow
would prove nothing about a real one. The contracts have no field that could hold a secret, so
the omission is structural rather than pending. ADR 0008.

**Multi-step tool chaining.** The runtime executes at most one tool per request. That is a
constraint of the reference implementation, not a claim that chaining is unnecessary — and
adding it would mean revisiting the injection analysis, because a loop is exactly the structure
that currently does not exist.

**Immediate revocation.** Disablement takes effect at the next snapshot refresh. Closing that
window needs a push channel from the control plane to every runtime, which is the coupling the
architecture deliberately avoids. The bound is configurable and stated.
