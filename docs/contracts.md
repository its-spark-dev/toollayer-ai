# Contracts

## 1. What a contract is here

The Control Plane and the Runtime never import each other. Everything they share is in
`packages/contracts`, and it stays small on purpose: schemas, typed models, deterministic
serialization, the error shape, and the provider adapters. **No business logic.** Putting
service behavior there would recreate the coupling the boundary exists to prevent.

> The internal representation is defined by this project. It is **not** an industry standard,
> and no interoperability is claimed with anything outside this repository.

That disclaimer is not modesty. Treating a project-local format as a standard is how a
downstream consumer ends up depending on a shape that was only ever one team's convention.

## 2. The documents

| Contract | Schema | Crosses a boundary? |
|---|---|---|
| Tool Definition | `tool-definition.schema.json` | Embedded in a connector |
| Connector Definition | `connector-definition.schema.json` | Embedded in a snapshot |
| Deployment Snapshot | `deployment-snapshot.schema.json` | **Yes** — Control Plane → Runtime |
| Tool Execution request/result | `tool-execution.schema.json` | Client → Runtime |
| Error Envelope | `error-envelope.schema.json` | Every failure from every service |

### Tool Definition

One provider-neutral tool bound to one reviewed HTTP operation:

- `tool_name` — the dispatch identifier, unique within a connector version;
- `description` — model-facing;
- `input_schema` — a **closed** Draft 2020-12 object schema;
- `operation` — method, path template, and one binding per argument;
- `policy` — effect class, confirmation requirement, role access;
- `provenance` — where it came from, including the description's origin.

The split between the first three fields and `operation` is deliberate: the model sees what
it may call, and the runtime separately knows how to call it. A model never sees a URL.

### Connector Definition

A versioned bundle of tools with a `runtime.base_url`, source provenance including the
document digest, and audit timestamps. It carries **no credential** — `auth_profile_ref` is an
opaque name the runtime resolves locally, never a secret.

### Deployment Snapshot

The immutable set of connector versions one deployment may serve, with a monotonic `revision`,
a content-derived `snapshot_id`, and a `snapshot_digest` covering the whole document.

## 3. Ownership and versioning

The contracts are owned by the shared package, not by either service. Neither can change a
shape unilaterally: `tests/contract` runs against both representations and fails on drift.

Every boundary-crossing document declares `contract_version` (SemVer). The consumer's rule:

- **Different major** → refuse. The shapes are not compatible.
- **Newer minor than this build** → refuse. A consumer that has not been taught a newer minor
  may be missing a field the producer considers meaningful, and silently ignoring it is how
  two services drift without anyone noticing.
- **Same or older minor** → accept.

Refusing a newer minor is conservative — a purely additive change would technically be safe —
but "purely additive" is a claim the consumer cannot verify, and the cost of being wrong is a
tool that behaves differently from its published definition.

## 4. Compatibility guarantees

Within a major line:

| Change | Allowed | Why |
|---|---|---|
| Add an optional field | Yes | Old consumers ignore it; they were built when it did not exist |
| Add an enum value | **No** | An old consumer would reject a document it cannot represent |
| Remove or rename a field | No | Breaks every existing consumer |
| Tighten a constraint | No | A previously valid document becomes invalid |
| Loosen a constraint | **No** | Old consumers would still reject documents the producer now considers valid |

Both "no" rows on constraint changes are stricter than usual. They follow from every document
being validated by *both* sides: a shape only one side accepts is not a contract.

## 5. Provider adapters

The canonical definition is the source of truth. A provider's format is an output projection,
produced on demand and never persisted as connector state. That direction matters — if the
canonical definition were derived from a provider format, the project would inherit whichever
provider it was built against.

An adapter has one hard obligation: **never widen what the canonical definition asserts.** A
constraint that cannot be expressed in the target fails with a stable diagnostic instead of
being dropped. A silently dropped `maxLength` is a validation hole that only shows up when
someone exploits it.

Two adapters ship:

| Provider | Shape | Divergence |
|---|---|---|
| `openai` | `{type: "function", function: {name, description, parameters, strict: true}}` | Strict mode requires every property in `required` |
| `anthropic` | `{name, description, input_schema}` | Ordinary JSON Schema; nothing to normalize |

### The one controlled normalization

OpenAI strict function calling requires every declared property to appear in `required`. Simply
adding names to `required` would make optional arguments mandatory — a real change to what the
tool accepts. Instead the property's type is widened to admit `null` (and its `enum`, if it has
one, gains `null` too, or the projection would claim to accept a value it then rejects).

This is **not lossless**: the projected schema accepts one value the canonical schema does not.
It is safe only because it is reversible. `normalize_provider_arguments` strips the `null`
placeholders before validation, and the resulting object is checked against the *unmodified*
canonical schema. The canonical schema, never the projection, decides what executes.

An undeclared argument is deliberately *not* stripped during normalization, so the subsequent
validation rejects it loudly instead of it disappearing quietly.

`tests/contract` asserts the round trip, the widened enum, and the non-stripping.

Perfect cross-provider portability is not guaranteed. Providers differ in what their schema
subsets accept, so a tool that projects cleanly for one may be refused for another. That is
reported per tool, and the sibling tools stay usable.

## 6. Standardized errors

One shape, everywhere:

```json
{
  "error": {
    "code": "argument_validation_failed",
    "message": "the proposed arguments do not satisfy the tool's input schema",
    "pointer": "/limit",
    "details": [{"code": "schema.maximum", "message": "...", "pointer": "/limit"}],
    "request_id": "9f2c1ab4e5d60781"
  }
}
```

Two rules govern it.

**Callers branch on `code`, never on `message`.** Codes are stable; messages are free to improve.

**A message describes the rule that failed and where, never the value that failed it.**
Echoing rejected input is how secrets, tokens, and internal hostnames end up in logs and in a
user's browser. Every validation path in this repository reports a JSON Pointer and a keyword,
and `tests/security` asserts that a rejected argument containing a credential-shaped string
does not appear in the response.

The full code vocabulary is in `packages/contracts/toollayer_contracts/errors.py`. Each code
maps to exactly one HTTP status in one table, so no handler has to remember which is which.

## 7. Contract testing

`tests/contract/` checks the things that would otherwise let the two services drift apart
without any other test failing:

| Test | What it protects |
|---|---|
| Every packaged schema is a valid Draft 2020-12 schema | A malformed schema validates nothing |
| Models and schemas accept and reject the same documents | One representation drifting from the other |
| Published documents round-trip through the models unchanged | Serialization asymmetry |
| The runtime accepts what the control plane published | The actual cross-service claim |
| A tampered snapshot is refused | The integrity story |
| A different major version is refused | Version negotiation |
| Both adapters project every published tool | The neutrality claim |
| Projection does not mutate the canonical definition | Cross-provider contamination |
| The strict projection is reversible | The one place a projection is not lossless |
| The internal API's ETag equals the snapshot digest | Two documents could otherwise share a tag |
| Every failure validates against the error envelope | The uniform failure claim |

One of these caught a real defect during development: the Pydantic models supplied defaults
for fields the JSON Schema marked required, so a document omitting `policy` was accepted by
one representation and rejected by the other — and, worse, the default was *permissive*. The
models were changed to require what the schema requires.
