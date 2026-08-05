# Tool Control Plane

The Control Plane turns an API description into governed, versioned, provider-neutral tool
definitions. It owns configuration time and never processes a user request.

## 1. Pipeline

```
OpenAPI document
      │  ingest      bounded, strict, offline
      ▼
  parsed document
      │  resolve     same-document $ref only
      ▼
  resolved operations
      │  convert     deterministic, refuses what it cannot represent
      ▼
  candidate tools + diagnostics        ← a proposal, not a decision
      │  review      a human decides what publishes and who may call it
      ▼
  reviewed draft
      │  publish     server-authoritative rebuild + digest
      ▼
  immutable PublishedVersion
      │  deploy      pin exact versions
      ▼
  immutable DeploymentSnapshot         → the runtime reads this
```

## 2. Source ingestion

`POST /admin/v1/connectors` accepts an OpenAPI 3.0 or 3.1 document as JSON or YAML. Format is
chosen by content, not by file extension — trusting the extension would let an upload pick
its own parser.

The uploaded bytes are stored verbatim alongside their SHA-256 digest and length. Everything
downstream is reproducible from them, and a reviewer can always see exactly what was
submitted rather than a reserialization of it.

Three properties are enforced before the document becomes a Python object:

**Bounded.** Byte length (2 MiB default), nesting depth (64), and node count (200,000) are
capped before traversal, so a small upload cannot expand into an unbounded walk.

**Unambiguous.** Duplicate object keys are rejected — in JSON *and* in YAML — as are a UTF-8
BOM, invalid UTF-8, non-finite numbers, and trailing data. Two parsers disagreeing about which
duplicate key wins is how a reviewer and a runtime end up looking at different documents.

**Offline.** YAML is parsed in safe mode. No loader resolves a URL or a filesystem path, and
there is no HTTP client in the package at all — which makes SSRF through a crafted `$ref`
structurally impossible rather than merely blocked.

## 3. Reference resolution

Only `#/...` pointers are resolved. Remote URLs, sibling files, and absolute paths are refused.

Resolution is depth- and cycle-bounded. A self-referential schema is legal OpenAPI and cannot
be inlined, so it is reported as an unsupported feature for the operation that uses it rather
than being allowed to hang the request.

In OpenAPI 3.1 a `$ref` may carry sibling annotations. Those are applied on top of the
resolved target, so a `description` override works while a sibling cannot replace a validation
keyword the target asserted.

## 4. Analysis and conversion

Every operation is converted independently. One operation that cannot be converted produces a
diagnostic and does not fail the document — a converter that refused a whole specification
over one awkward operation would be useless against real-world documents.

### What conversion produces

| Output | Derived from |
|---|---|
| `tool_name` | `operationId`, normalized to snake_case; otherwise derived from method and path |
| `description` | The operation's `description` or `summary`; otherwise a placeholder, flagged as `generated` |
| `input_schema` | Path and query parameters plus a JSON request body, as a closed Draft 2020-12 object schema |
| `operation.bindings` | One RFC 6901 pointer per argument → its request location |
| `policy.effect_class` | The HTTP method: read / write / destructive |
| `provenance` | The source path, method, `operationId` (or null), tags, and deprecation |

Naming is stable and legible. `GET /v1/tickets/{ticket_id}` with no `operationId` becomes
`get_tickets_by_ticket_id`; a leading version segment is dropped because it describes the URL
layout rather than the operation. A derived name never sets `source_operation_id`, so nothing
downstream can mistake naming for provenance. Names longer than 64 characters keep a readable
prefix plus a digest suffix, so distinct inputs stay distinct.

### What conversion refuses

| Refused | Why |
|---|---|
| Header and cookie parameters | Not exposed as model-supplied arguments |
| `oneOf` / `allOf` / `anyOf` / `not` | Supporting them means deciding what a partial match does at execution time — a semantic choice the document does not make |
| Untyped schemas | A value with no declared type cannot be validated |
| Open objects (`additionalProperties: true`) | Cannot be projected into a closed tool schema |
| Non-default `style` / `explode` / `allowReserved` | The executor encodes one value per parameter; accepting these would build a request the API cannot parse |
| Non-JSON request bodies | The executor sends JSON |
| Per-path or per-operation `servers` | The connector declares one reviewed base URL |
| A path parameter not marked `required: true` | Would produce a URL with an unfilled placeholder |
| A name used in two parameter locations | The flat argument object cannot represent both |
| Two operations normalizing to one tool name | Auto-renaming would invent a name that appears nowhere in the source |
| Unknown schema keywords | A keyword that silently means nothing at runtime is a validation hole |

The last row is the general principle: the keyword filter is an **allowlist**, because the
input schema is not just data — the runtime executes it against model output.

## 5. Draft review

A draft is the mutable authoring workspace. Exactly one exists per connector.

A reviewer may change:

- whether an operation is published at all;
- the model-facing description;
- the effect class and whether confirmation is required;
- which roles may call the tool.

A reviewer may **not** change the input schema, path, method, or bindings. Those are derived
from the source document, and hand-editing them would mean the published tool no longer
described the API it claims to describe. Changing them means changing the document and
re-analyzing.

Every mutation carries `expected_revision`. A mismatch is rejected with `revision_conflict`
rather than merged, so two reviewers cannot silently overwrite each other.

Field provenance is recorded: a description is `source`, `generated`, `assisted` (a model
proposed it and a human accepted it), or `human`. A `generated` placeholder blocks publication —
a machine's placeholder is not documentation.

## 6. Publication

Publication is **server-authoritative**. It rebuilds the connector from the stored analysis
and the stored review, and accepts nothing from the request beyond the version number and who
is publishing. A compromised or buggy console therefore cannot publish a definition that no
reviewer ever saw.

The sequence:

1. Check readiness. Every blocking issue is reported at once, not one failed publish at a time.
2. Rebuild each included tool from the stored analysis, applying the reviewed description and
   policy.
3. Validate the assembled document against the JSON Schema — the portable contract, not only
   the models that built it.
4. Compute the digest over the canonical serialization.
5. Write the version and delete the draft, in one transaction.

Blocking issues include: no base URL, no operation selected, a description still marked
`generated`, a restricted tool with no roles, or a review that no longer matches the analysis.

Versions must strictly increase. Publishing 0.1.0 after 0.2.0 would make "latest" ambiguous
and let a snapshot pin a number that means something other than a reader expects.

## 7. Deployments and snapshots

A deployment is a named runtime. A snapshot is the immutable set of connector versions it may
serve.

Snapshot construction resolves each selection to an exact published version and embeds it
whole. The runtime then needs one request to know everything it may serve, and can verify what
it received without trusting the transport.

- Exactly one version per connector. A duplicate is rejected.
- A disabled version cannot enter a new snapshot.
- The `snapshot_id` is derived from the content digest rather than allocated — two snapshots
  with the same content and revision *are* the same snapshot, and the identifier says so.
- Revisions increase monotonically and are never reused. Creating revision N+1 deactivates N;
  N stays queryable and byte-identical.

## 8. Persistence

SQLAlchemy models on SQLite (default) or PostgreSQL. See `docs/data-model.md`.

The distinction the schema is built around is mutable versus immutable. Mutable rows carry a
revision and are updated in place. Immutable rows store a complete contract document plus its
digest and are never updated after insert. Every uniqueness rule that protects immutability is
a database constraint rather than an application check, because an application check does not
survive two concurrent requests.

## 9. API surface

### Administrator (`x-toollayer-admin-token`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/v1/connectors` | List connectors |
| `POST` | `/admin/v1/connectors` | Register a document and analyze it |
| `GET` | `/admin/v1/connectors/{key}` | Connector summary |
| `GET` | `/admin/v1/connectors/{key}/draft` | The draft, its analysis, review, and readiness |
| `PATCH` | `/admin/v1/connectors/{key}/draft` | Apply review decisions |
| `POST` | `/admin/v1/connectors/{key}/publish` | Publish an immutable version |
| `GET` | `/admin/v1/connectors/{key}/versions` | List published versions |
| `GET` | `/admin/v1/connectors/{key}/versions/{v}` | The exact published document |
| `POST` | `/admin/v1/connectors/{key}/versions/{v}/disable` | Stop a version being deployable |
| `GET` | `/admin/v1/connectors/{key}/versions/{v}/adapters/{provider}` | Provider projection preview |
| `GET`/`POST` | `/admin/v1/deployments` | List and create deployments |
| `GET`/`POST` | `/admin/v1/deployments/{key}/snapshots` | List and create snapshots |
| `GET` | `/admin/v1/deployments/{key}/snapshots/{revision}` | One snapshot, with its document |

### Runtime (`x-toollayer-service-token`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/internal/v1/deployments/{key}/snapshot` | The active snapshot, with an ETag and `304` support |

### Operations

`GET /healthz` — liveness; deliberately does not touch the database.
`GET /readyz` — readiness; the database must answer.

The two tokens are separate and must differ; the service refuses to start otherwise. A leaked
runtime credential can therefore never publish anything.

## 10. Failure cases

| Situation | Code | Status |
|---|---|---|
| The document is malformed | `invalid_source_document` | 422 |
| The document uses a refused feature | `unsupported_spec_feature` | 422 |
| The document exceeds an ingestion limit | `source_too_large` | 413 |
| The draft changed since it was read | `revision_conflict` | 409 |
| The draft is not ready to publish | `not_ready_for_publication` | 422 |
| That version already exists | `immutable_version` | 409 |
| The version does not increase | `invalid_request` | 400 |
| The connector, version, or snapshot does not exist | `not_found` | 404 |
| The deployment has no snapshot | `snapshot_unavailable` | 503 |
| The token is missing or wrong | `unauthenticated` | 401 |

Every failure uses the shared envelope, carries a stable code, and names the failing location
with a JSON Pointer. No message contains the value that caused it.
