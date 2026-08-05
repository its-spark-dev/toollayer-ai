# Pre-publication review

**Status: READY FOR HUMAN REVIEW**

Date: 2026-08-05 · Branch: `feature/initial-clean-room-implementation` · Contract version: 1.0.0

This records what was audited, what the automated checks found, and what a human still has to
decide. Automation cannot discharge the last part.

---

## 1. Audit scope

| Area | Covered |
|---|---|
| Working tree | Every tracked file (123) |
| Git history | Every commit on every ref |
| Secrets | Credential patterns in tree and history |
| Internal identifiers | Employer, product, host, and personal-name patterns in tree and history |
| Source reuse | Clean-room compliance |
| Dependencies | License inventory |
| Quality gates | Lint, strict type check, full test suite |
| Reproducibility | Clean clone, setup, tests, and demo from scratch |
| Container config | Static validation (Docker daemon unavailable in this environment) |
| Documentation | README claims checked against the implementation |
| Commit hygiene | Authorship and attribution trailers |

## 2. Implementation summary

| Component | Lines (approx.) | State |
|---|---|---|
| `packages/contracts` | 1,300 | 5 JSON Schemas, typed models, canonical serialization, error taxonomy, 2 provider adapters |
| `packages/openapi-converter` | 1,500 | Loader, reference resolver, schema conversion, operation converter, analyzer |
| `packages/policy-engine` | 1,100 | Authorization, destination policy, argument validation, governed executor, redaction |
| `packages/mock-llm` | 400 | Deterministic provider |
| `apps/control-plane/backend` | 2,000 | Ingestion, review, publication, deployments, admin + internal APIs, migrations |
| `apps/control-plane/frontend` | 900 | React + TypeScript review console |
| `apps/runtime` | 1,000 | Snapshot store, orchestrator, HTTP surface, CLI |
| `apps/demo-api` | 400 | Synthetic Support API |
| `tests` | 2,300 | 180 Python tests across 5 suites |
| `docs` | — | 12 documents, 9 ADRs |

The complete vertical slice works: an OpenAPI document is registered, analyzed, reviewed,
published, snapshotted, loaded by a separate service, and executed under policy — with
unauthorized, fabricated, malformed, and unconfirmed calls each rejected with a distinct code.

## 3. Secret scan

**Result: CLEAN.**

Patterns searched across the working tree and every commit on every ref: OpenAI-style keys,
GitHub tokens, AWS access key IDs, PEM private keys and certificates, Slack tokens, Google API
keys, JWTs.

```
working tree : no matches
full history : no matches
```

`.env.example` contains only placeholders, and they are deliberately obvious
(`dev-admin-token-change-me`). A CI job asserts it stays that way.

The Control Plane refuses to start if the admin and service tokens are equal, and logs a warning
if either is still the shipped placeholder.

## 4. Internal identifier scan

**Result: CLEAN.**

Searched in the working tree and in every commit, case-insensitively, for: the employer name and
its variants; internal product and platform names; internal Git hosting and package-index hosts;
the names of other contributors to the reference repositories; and Korean-language terms that
appeared in the original systems.

```
working tree : no matches
full history : no matches
```

**Network identifiers.** Every address in the repository is reserved or documented:
`127.0.0.1`, `localhost`, `0.0.0.0`, `169.254.169.254` (as a metadata address that must be
*refused*), `10.0.0.5`, `100.0.0.1`, `224.0.0.1`, and `::1` — all in security tests or
documentation.

**Domains.** All are RFC 2606 reserved (`example.org`, `.test`, `.invalid`, `.example`),
Docker Compose service names, `localhost`, or well-known public sites (`json-schema.org`,
`python.org`, `github.com`, `shields.io`, `contributor-covenant.org`).

## 5. Source reuse

**Result: no direct reuse. Every component was independently implemented.**

`private-notes/REUSE_CANDIDATES.md` (gitignored) lists 26 files across the reference
repositories that were *considered* for direct reuse. Every one is recorded as
**DECLINED — reimplemented** or **DECLINED — omitted**. No reuse authorization was requested,
because none was needed.

The method — behavioral specification first, implementation second — is documented publicly in
`docs/CLEAN_ROOM_PLAN.md` and ADR 0009.

Deliberate divergences that make the independence demonstrable:

| Area | This project |
|---|---|
| Contracts | Newly authored, with a `contract_version` envelope and different field names |
| Access model | Generic roles (`support-agent`, `support-lead`, `auditor`) |
| Method coverage | GET, POST, PUT, PATCH, DELETE, including JSON request bodies |
| Provider adapters | Two, so neutrality is testable |
| Persistence | SQLAlchemy ORM, new schema, one newly authored migration |
| Model provider | Deterministic and offline |
| Demo domain | Support ticket management, invented for this repository |
| Language | English throughout |

**Reference repositories were not modified.** Verified by comparing HEAD revisions before and
after: all four are at their original commits, with no new branches, tags, or working-tree
changes.

## 6. Clean-room compliance

| Requirement | Status |
|---|---|
| No source copied, renamed, translated, or lightly edited | ✅ |
| No internal schemas, database schemas, or migrations reused | ✅ |
| No private API specifications reused | ✅ |
| No UI components, styling, copy, or assets reused | ✅ |
| No test fixtures, seed data, or evaluation cases reused | ✅ |
| No deployment configuration, CI pipelines, or registries reused | ✅ |
| No architecture documents or ADRs reused | ✅ |
| No Git history transferred | ✅ |
| Method documented publicly before implementation | ✅ `docs/CLEAN_ROOM_PLAN.md`, committed first |

## 7. Dependency licenses

**Result: all permissive. No copyleft obligation.**

| Package | Version | License |
|---|---|---|
| fastapi | 0.141.1 | MIT |
| uvicorn | 0.52.1 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| jsonschema | 4.26.0 | MIT |
| PyYAML | 6.0.3 | MIT |
| SQLAlchemy | 2.0.51 | MIT |
| alembic | 1.19.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |

Console dependencies (React, Vite, TypeScript, ESLint, Vitest) are MIT or Apache-2.0.

Nothing constrains the intended MIT license.

## 8. Test results

```
180 passed in 3.5s
```

| Suite | Tests | Protects |
|---|---|---|
| `tests/unit` | 102 | Conversion rules and refusals, policy decisions, contract invariants |
| `tests/contract` | 26 | That the two services still agree |
| `tests/integration` | 22 | Lifecycle rules across wired components |
| `tests/security` | 28 | Every control that must refuse something |
| `tests/e2e` | 2 | The full documented flow |

Console: **5 passed** (Vitest). Total **185**.

The suite runs entirely offline — no API key, no network egress. Outbound calls are dispatched
into the demo API in-process, so the real validator, the real policy engine, and the real demo
API all execute while no socket is opened.

**One defect was found and fixed by these tests during development.** The contract suite caught
the Pydantic models supplying defaults for fields the JSON Schema marked required: a document
omitting `policy` was accepted by one representation and rejected by the other, and the default
was *permissive*. The models were changed to require what the schema requires.

## 9. Lint and type check

```
ruff check .          All checks passed!
ruff format --check   87 files already formatted
mypy packages apps    Success: no issues found in 50 source files   (strict mode)
```

Console:

```
eslint .        clean
tsc --noEmit    clean
vite build      succeeded (168 kB, 53 kB gzipped)
```

Two lint rules are disabled with a stated reason in `pyproject.toml`: `B008` (FastAPI expresses
dependency injection through argument defaults) and `N818` (exception classes are named for the
outcome they describe — `PolicyDenied`, `ConfirmationRequired` — which reads correctly at the
raise site).

## 10. Clean-clone and reproducibility

A fresh `git clone` of the branch into an empty directory:

| Step | Result |
|---|---|
| `private-notes/` present in the clone | ❌ absent, as required |
| `make setup` | ✅ found Python 3.11, created the venv, installed |
| `make test` | ✅ 180 passed |
| `make demo` (default ports) | ✅ complete, every control rejected as documented |
| `make demo` (overridden ports) | ✅ complete |

**Two real defects were found and fixed by this exercise:**

1. `make setup` failed opaquely when the default `python3` was 3.9. It now searches for a
   supported interpreter and prints an actionable message.
2. `make demo` failed when ports were overridden, because the connector inherited the
   specification's hard-coded `servers` URL. The demo now supplies the base URL explicitly —
   which is also the more correct behavior, since where *this* deployment calls an API is a
   separate decision from where its author publishes it.

## 11. Container configuration

**Status: statically validated. A Docker daemon was not available in this environment.**

| Check | Result |
|---|---|
| `docker-compose.yml` parses; 4 services, 1 volume | ✅ |
| Every path a Dockerfile `COPY`s exists | ✅ 12/12 |
| Services run as a non-root user | ✅ uid 10001 |
| Health checks defined on all three Python services | ✅ |
| `depends_on` uses `condition: service_healthy` | ✅ |
| Runtime allowlist names an exact origin, no wildcard | ✅ `http://demo-api:8081` |
| `.dockerignore` excludes `private-notes`, `.venv`, `.git`, `data` | ✅ |

**Remaining human step:** run `docker compose up -d --build && make demo-docker` on a machine
with Docker and confirm it completes.

## 12. Documentation consistency

| Check | Result |
|---|---|
| Every relative link in the README resolves | ✅ |
| Every test named in the README exists | ✅ |
| All 9 ADRs are linked | ✅ |
| Every `make` target the README mentions exists | ✅ |
| Cross-links between `docs/` files resolve | ✅ |
| The claimed test count matches reality | ✅ 180 Python + 5 console = 185 |

Required wording is present verbatim:

- "The runtime is provided as a reference implementation rather than a full chatbot product."
- "ToolLayer AI is an independent portfolio implementation built from first principles using
  public standards and general software architecture patterns."
- "It does not contain proprietary source code, internal assets, confidential data, or private
  infrastructure configuration from any employer."

The README does **not** describe the project as production-ready, and does **not** imply the
canonical format is an industry standard — both are explicitly disclaimed.

## 13. Commit hygiene

```
authors        : its-spark-dev <its.spark.dev@gmail.com>
                 Sang Park <its.spark.dev@gmail.com>     (pre-existing initial commits)
trailers       : no Co-authored-by, Co-Author, Generated-by, Created-by,
                 Assisted-by, Signed-off-by, or AI/tool attribution in any commit
private-notes/ : never appears in any commit on any ref
force-push     : none
history rewrite: none
```

A CI job enforces the trailer and `private-notes/` rules on every push.

## 14. Known limitations

Stated in the README, `docs/threat-model.md` §7, and `docs/deployment.md` §10:

- Static bearer tokens; no rotation, expiry, or per-actor identity.
- The runtime enforces roles the client asserts; it does not authenticate anyone.
- Disablement is not immediate revocation — it applies at the next snapshot refresh.
- The console ships its admin token in the browser bundle; acceptable only for a local demo.
- A time-of-check-to-time-of-use gap exists between DNS resolution and connection.
- One tool per request; no chaining, memory, or streaming.
- Single-tenant; no rate limiting, metrics, or tracing.
- Local-development escape hatches widen the destination policy; all default to off.

## 15. Remaining human review items

Automation cannot decide these.

| # | Item | Who |
|---|---|---|
| 1 | Read `docs/CLEAN_ROOM_PLAN.md` and confirm the method is accurately described | Author |
| 2 | Confirm publishing is compatible with the employment agreement | Author, possibly with counsel |
| 3 | Spot-check the README's claims against the code | Author |
| 4 | Confirm the limitations sections are honest and complete | Author |
| 5 | Run `make demo-docker` on a machine with Docker | Author |
| 6 | Approve the MIT license and add the copyright line | Author |
| 7 | Decide on repository visibility | Author |
| 8 | Capture the screenshots/GIFs the README marks as placeholders | Author |

Item 2 is the one that actually gates publication. Everything else is verification.

## 16. Recommendation

**Recommend: READY FOR HUMAN REVIEW → then publication approval.**

The automated portion of the audit is clean:

- No secrets, in the tree or in history.
- No employer or internal identifiers, in the tree or in history.
- No source reuse; every component independently implemented against a documented method.
- All dependency licenses permissive.
- 185 tests passing, lint clean, strict type checking clean.
- Reproducible from a clean clone, including the end-to-end demonstration.
- Documentation consistent with the implementation.
- No attribution trailers; `private-notes/` never committed.
- All four reference repositories unmodified.

The audit also *found* three real defects — a contract drift, a setup failure, and a demo
port-binding bug — which were fixed rather than documented around.

The remaining gate is human judgement about publishing, not a technical finding.

**Status after human sign-off: READY FOR PUBLICATION APPROVAL.**
