# Changelog

Notable changes to ToolLayer AI. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Two version numbers appear here and they move independently. The **release version** describes
this repository. The **contract version** (`CONTRACT_VERSION`) describes the documents that
cross the boundary between the Control Plane and the Runtime; it is project-defined, not an
industry standard.

## [Unreleased]

Nothing yet.

## [0.2.2] — 2026-08-06

Contract version: **unchanged at 1.1.0.** Migration impact: **none.**

A correctness fix to when the Control Plane commits. **A successful mutating response now
means the transaction has committed before the response is sent.** Previously it could mean
"will commit shortly", and a client that immediately read back what it had just written could
be told the write did not exist.

### Fixed

- **Read-your-own-write on the admin API.** FastAPI runs the exit half of a `yield` dependency
  after the response has already gone to the client, and that is where the request-scoped
  session committed. So a `201` reached the caller, and the row became durable afterwards.

  On a reused keep-alive connection this is invisible: the server finishes the whole request
  cycle, teardown included, before reading the next request off that socket, so the two
  requests serialize. Send the follow-up on a *different* connection and it is handled
  independently, and can begin while the previous commit is still pending.

  Measured against the unfixed code over 3,000 create-then-read pairs: **zero** failures
  reusing the connection, **seven** on fresh ones. Two instances reached CI — one reported as
  `no deployment exists with that key` immediately after that deployment was created, one as a
  spurious `revision_conflict` immediately after a draft was reviewed. Both endpoints, one
  cause.

  Mutating routes now commit after the endpoint returns and before the response is built. This
  is a change to *when* the commit happens, not a retry and not specific to any one database
  backend.

- **A failing commit is no longer invisible.** Because the commit now happens while the
  response is still being assembled, a commit that raises produces a `500` instead of being
  logged after a `201` the client has already accepted.

### Changed

- The end-to-end demo job prints each service's log inline when it fails, as well as uploading
  it. An uploaded artifact does not survive a job re-run, and re-running is the first thing
  anyone does with an intermittent failure.

### Migration from 0.2.1

**None.** No API shape, status code, error code, configuration key, contract document or
accepted input changed. The only observable difference is that a success response is now a
stronger statement than it was: the write is committed by the time you receive it.

## [0.2.1] — 2026-08-06

Contract version: **unchanged at 1.1.0.** Migration impact: **none** — the set of documents
this ingester accepts is identical.

A patch release hardening the YAML side of the ingestion boundary. It began as a CodeQL alert
that turned out to be a false positive, and the investigation it prompted found three real
defects in the same function — two of which turned a small authenticated upload into an
HTTP 500.

### Fixed

- **Deeply nested documents are refused instead of crashing the request.** PyYAML's scanner
  and parser are iterative but its *composer* is not, so a document nested past CPython's
  stack limit raised `RecursionError` from inside the library before reaching the depth check
  in `_enforce_shape` — the code path whose comment promises exactly that cannot happen. A
  692-byte upload was enough. Now reported as `document_too_large` ("the document nests too
  deeply"), the limit it actually hit.
- **A non-scalar mapping key is a structured refusal.** `? {a: 1}` is valid YAML and
  unrepresentable in JSON. It reached a hashability check with an unhashable value and raised
  an unhandled `TypeError`, also surfacing as an HTTP 500. Now `invalid_source_document`.
- **YAML merge keys are refused as themselves.** `<<: *anchor` was reported as "not
  well-formed YAML", which is untrue — it is well-formed and deliberately unsupported,
  because resolving one gives the reviewed document and the file on disk different key sets.

### Changed

- **The YAML loader is instantiated directly rather than through `yaml.load(..., Loader=...)`.**
  The generic call takes its safety from an argument defined elsewhere; removing it leaves the
  loader class named at the only place it is used. A new import-time check refuses to start a
  build whose loader has gained an unsafe base class, a `python/*` tag, or a
  multi-constructor — the three ways a `SafeLoader` subclass can be widened without any line
  that looks security-relevant changing.
- **The ingestion refusal set is documented** in `docs/control-plane.md` alongside the
  existing conversion refusals.

### Security

The CodeQL alert (`py/unsafe-deserialization`, reported Critical) was a **false positive**.
Unsafe object deserialization was not possible and no code execution path existed: the loader
inherited only from `SafeLoader` and `SafeConstructor`, registered no `python/*` constructor
and no multi-constructor, and its one customization replaced the standard mapping tag. CodeQL
resolves the `Loader=` argument against a list of known-safe loader classes, and a subclass
defined in this repository is not on that list.

What the investigation did find were **availability and error-handling defects**: two inputs
under a kilobyte that produced an unhandled exception and a 500 at the system's first trust
boundary. Those are what this release fixes.

42 regression tests cover the loader's tag surface, each way the subclass could be widened,
every structural refusal, the parser limits, and the absence of the `yaml.load` call at syntax
level. No payload is permitted a side effect: a sentinel records any callable construction, and
`os.system`, `os.popen` and the `subprocess` entry points are trapped to fail the test if a
parse ever reaches them.

### Migration from 0.2.0

**None.** Anchors, aliases and every standard scalar type still load, and the repository's own
example specification is asserted to parse unchanged. Three inputs that previously produced an
HTTP 500 or a misleading message now produce the project's structured errors. Duplicate-key
rejection is unchanged. No configuration, contract or artifact change is required.

## [0.2.0] — 2026-08-06

Contract version: **1.0.0 → 1.1.0** (additive; see [Migration](#migration-from-010)).

This release turns three documented security intentions into verifiable boundaries. In each
case the previous behavior was a weaker property than the documentation described, and the
correction is in the implementation, the tests, and the prose together.

### Added

- **Ed25519 signatures on deployment snapshots.** The Control Plane signs the canonical bytes
  of the whole document with the `signature` field removed — which therefore covers
  `snapshot_id` and `snapshot_digest` as well as every connector. The Runtime verifies against
  an explicitly configured trusted key.
- **A trusted signing-key ring** so keys rotate without every service changing in the same
  instant. Several keys are trusted during the overlap and none outside it.
- **A verified caller-identity mode.** `verified_token` accepts a signed compact JWS (`EdDSA`
  over Ed25519, RFC 8037) and checks signature, issuer, audience and expiry before deriving a
  subject and roles. It verifies offline against a configured public key, so no identity
  provider is needed to run or test it.
- **`snapshot_signature_invalid`** as a distinct error code. "These bytes are damaged" and
  "these bytes are not ours" say different things to an operator.
- **Health and readiness disclosure.** `/healthz` reports the snapshot verification mode, the
  trusted key ids, and whether caller identity is verified or merely asserted. `/readyz`
  reports which key authenticated the snapshot being served.
- **`scripts/generate_signing_key.py`**, so development key material is generated per run
  rather than committed.
- **CI jobs** for wheel build and clean install, Docker end-to-end execution, lockfile drift,
  and supply-chain scanning; a CodeQL workflow; Dependabot for uv, npm, Actions and Docker.
- **SBOM generation** (`make sbom`) in CycloneDX for the locked Python graph, plus the console
  dependency tree.
- **A GitHub Pages portfolio page** built from the existing captures and visual language.
- **117 new tests** across contract, security, integration and unit suites — snapshot
  authenticity, key rotation, streaming bounds against a real HTTP server, one-snapshot
  consistency, deep immutability, caller-token verification, and malformed destinations.

### Changed

- **One orchestration request now uses one immutable snapshot revision.** Discovery takes the
  snapshot the turn started with rather than acquiring its own.
- **The outbound response bound is enforced while the body is read**, not after. The transport
  streams in bounded chunks and closes the response at the limit, so the cap bounds memory
  rather than only the returned value.
- **The loaded tool index is deeply immutable** (`MappingProxyType`), so a retained reference
  cannot alter what a later request resolves.
- **Dependency installation is locked and reproducible.** `uv.lock` is the resolution of
  record; `requirements.lock` is its hash-pinned export, used by pip, Docker and CI. `make
  lock` regenerates both and a CI job fails if they drift.
- **The Docker topology is executed in CI**, not statically reviewed — built, started on its
  health checks, driven through the full demonstration, and checked for non-root containers,
  absent key material, and answering ports.
- **Container base images are pinned to patch versions** rather than floating tags, with
  Dependabot proposing the next pin.
- **The console container runs as an unprivileged user** on port 8080 instead of nginx's master
  process running as root. nginx gained CSP, `Permissions-Policy` and `server_tokens off`.
- **Vite 5 → 7 and Vitest 2 → 4**, clearing high and critical advisories rather than lowering
  the audit threshold.
- **Security documentation distinguishes** deterministic serialization, content digest,
  producer signature, trusted-key configuration, and transport security, instead of using them
  interchangeably.

### Fixed

- **Snapshot substitution was overstated as prevented by SHA-256 alone.** Computing a digest
  requires no secret, so an attacker who could rewrite a payload could rewrite its digest. The
  threat model named exactly that attacker and offered self-digest recomputation as the
  mitigation.
- **Large responses were fully buffered before being truncated.** `response.content` had
  already read the entire body; the slice bounded only what was passed onward.
- **Malformed ports escaped as unhandled exceptions.** `urlsplit(...).port` raises lazily at
  the point of access, so an out-of-range port produced a 500 instead of a policy refusal.
  Every parse failure is now `destination_not_allowed`.
- **A single orchestration request could span two snapshot revisions** when a refresh landed
  between discovery and execution.
- **The tool index was shallowly immutable** — a frozen dataclass wrapping a mutable `dict`.
- **IPv4-mapped IPv6 addresses bypassed the address-family checks.** `::ffff:127.0.0.1` did not
  report itself as loopback; it is now unwrapped before the checks run.
- **`date-time` format validation was silently inactive.** `rfc3339-validator` was not a
  declared dependency, so jsonschema skipped the keyword — hiding that timestamps read back
  from SQLite were serialized without a UTC offset.
- **`make capture` could not start the runtime** once signing became required, because the
  capture script was never given key material.
- **nginx security headers did not apply to `/assets/`.** `add_header` does not merge across
  levels, so the server-level headers stopped applying the moment that block declared its own.
- **Documentation counts and section references were wrong** — 185 in two documents, 186 in
  three others, against a real figure of 180; and three `docs/deployment.md` cross-references
  off by one.

### Security

New guarantees:

- A snapshot whose content **and** digest have both been replaced is refused, because the
  attacker cannot reproduce the signature. This holds as far as the Runtime's trusted-key
  configuration is intact.
- An oversized response is disconnected at the limit rather than received in full and then
  discarded.
- In `verified_token` mode, assertion headers are refused outright rather than ignored, and
  every malformed, expired, wrongly-scoped or wrongly-signed token fails closed.

Explicitly still true:

- Signatures authenticate the **artifact**, not the transport. There is no TLS in the Compose
  topology, and a real deployment needs it.
- The default demo topology asserts caller identity rather than verifying it, and says so in
  its health output.
- A DNS time-of-check-to-time-of-use gap remains between address checking and connection. No
  pinned-IP protection is claimed.
- Revocation is polling at the refresh interval, not push.
- The audit trail is ordinary mutable database rows — a record, not tamper-evident evidence.

### Migration from 0.1.0

The contract goes to 1.1.0 because `signature` was added to the deployment snapshot. It is an
**optional** field, so a document produced by 0.1.0 still validates and still parses here. The
snapshot schema is closed, so the reverse does not hold: a 1.0.0 consumer refuses a signed
document, which is what the minor bump exists to make explicit.

1. **Configure Control Plane signing.** Generate a pair with
   `python scripts/generate_signing_key.py --key-id <name>`, then set
   `TOOLLAYER_SNAPSHOT_SIGNING_KEY` and `TOOLLAYER_SNAPSHOT_SIGNING_KEY_ID`. The private half
   belongs in a secret manager. Setting one without the other refuses to start.
2. **Configure Runtime trust.** Set `TOOLLAYER_SNAPSHOT_TRUSTED_KEYS` to
   `key_id:base64url_public_key`, comma separated. Verification is `required` by default and
   the Runtime refuses to start without a key rather than degrading to accepting anything.
3. **Republish snapshots.** A published snapshot is immutable and cannot gain a signature after
   the fact. Create a new snapshot once signing is configured. Existing unsigned snapshots are
   refused in the default mode with `snapshot_signature_invalid`; they load only under an
   explicit `TOOLLAYER_SNAPSHOT_VERIFICATION=disabled`, which is reported by `/healthz`.
4. **Choose an identity mode.** `TOOLLAYER_CALLER_AUTH_MODE` defaults to `asserted_header`,
   which is the previous behavior. `verified_token` additionally requires
   `TOOLLAYER_CALLER_TOKEN_TRUSTED_KEYS`, `..._ISSUER` and `..._AUDIENCE`.
5. **Reinstall from the lockfile.** `make setup` now installs from `requirements.lock` with
   hashes. Regenerate with `make lock` after changing `pyproject.toml`.
6. **Note the console container port.** nginx listens on 8080 inside the container instead of
   80. The published host port is unchanged at 5173.

`make demo` and `make demo-docker` need none of this: both generate an ephemeral key pair per
run and exercise the signed path by default.

## [0.1.0] — 2026-08-05

Initial public release. OpenAPI-to-tool conversion, human review, immutable published versions
and deployment snapshots, provider adapters, a governed execution boundary, and the reference
runtime.

[Unreleased]: https://github.com/its-spark-dev/toollayer-ai/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/its-spark-dev/toollayer-ai/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/its-spark-dev/toollayer-ai/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/its-spark-dev/toollayer-ai/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/its-spark-dev/toollayer-ai/releases/tag/v0.1.0
