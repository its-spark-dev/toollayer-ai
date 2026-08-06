# Deployment

This describes how to run ToolLayer AI **locally**. It is not a production deployment guide;
section 10 records exactly what has been verified, and section 11 says what is missing.

## 1. Requirements

| | Version | Needed for |
|---|---|---|
| Python | 3.11 or 3.12 | The three services |
| Node.js | 20+ | The console (optional) |
| Docker | any recent | `make demo-docker` (optional) |

No API key. No network egress. No external database.

## 2. Local development

```bash
make setup     # create .venv and install everything
make demo      # start all three services, run the demonstration, shut down
```

`make demo` prints each stage — register, review, publish, deploy, load, ask, reject — and
exits non-zero if any control fails to reject. It is a demonstration and a smoke test.

To run the services individually, in three terminals:

```bash
make run-demo-api        # :8081
make run-control-plane   # :8080
make run-runtime         # :8082
```

And the console:

```bash
cd apps/control-plane/frontend && npm install && npm run dev   # :5173
```

## 3. Docker Compose

```bash
docker compose up -d --build
make demo-docker
docker compose down -v
```

Topology:

```mermaid
flowchart LR
    subgraph net["compose network"]
        CP["control-plane<br/>:8080"]
        RT["runtime<br/>:8082"]
        API["demo-api<br/>:8081"]
        UI["console<br/>:80"]
    end
    VOL[("control-plane-data<br/>SQLite volume")]

    RT -->|"service token, read-only"| CP
    RT -->|"the one allowlisted origin"| API
    UI --> CP
    CP --> VOL
```

The three Python services share one image and differ only in their start command. Building one
image keeps them provably on the same code and the same contract version — which is exactly
the drift the contract tests exist to catch.

The runtime's `TOOLLAYER_ALLOWED_ORIGINS` names `http://demo-api:8081` explicitly. There is no
wildcard: adding a fourth service to the compose file does not make it reachable from a tool
call.

## 4. Ports

| Service | Port | Purpose |
|---|---|---|
| Control Plane | 8080 | Admin API, internal snapshot API |
| Demo API | 8081 | The synthetic upstream |
| Runtime | 8082 | Tool discovery and orchestration |
| Console | 5173 (dev) / 80 (container) | The review UI |

Override with `CONTROL_PLANE_PORT`, `DEMO_API_PORT`, `RUNTIME_PORT`.

## 5. Health and readiness

Both services distinguish the two, because they answer different questions.

| Endpoint | Question | Fails when |
|---|---|---|
| `/healthz` | Is the process alive? | The process is down. Deliberately does not touch the database, so a database outage does not trigger a restart loop. |
| `/readyz` | Can it serve requests? | Control Plane: the database does not answer. Runtime: no verified snapshot is loaded. |

The runtime's `/readyz` returning 503 with `no deployment snapshot` is the correct answer
before a snapshot exists — reporting ready and then failing every call would be worse.

`/healthz` on the runtime also reports `destination_policy_relaxed`, so a runtime running with
an escape hatch enabled identifies itself rather than looking identical to a locked-down one.

## 6. Configuration

Every setting is an environment variable, read once at startup and validated. See
`.env.example` for the annotated list. The ones that matter:

| Variable | Default | Notes |
|---|---|---|
| `TOOLLAYER_CONTROL_PLANE_DATABASE_URL` | SQLite file | PostgreSQL by connection string |
| `TOOLLAYER_ADMIN_TOKEN` | dev placeholder | Must differ from the service token |
| `TOOLLAYER_SERVICE_TOKEN` | dev placeholder | Read-only access to snapshots |
| `TOOLLAYER_ALLOWED_ORIGINS` | **empty** | Empty permits nothing. No wildcard. |
| `TOOLLAYER_ALLOW_PLAINTEXT_HTTP` | `false` | Local development only |
| `TOOLLAYER_ALLOW_LOOPBACK_DESTINATIONS` | `false` | Local development only |
| `TOOLLAYER_ALLOW_PRIVATE_ADDRESSES` | `false` | Local development only |
| `TOOLLAYER_SNAPSHOT_REFRESH_SECONDS` | `60` | Bounds how stale a snapshot may be |
| `TOOLLAYER_MAX_RESPONSE_BYTES` | `1048576` | Hard-capped at 16 MiB. Enforced while streaming. |
| `TOOLLAYER_SNAPSHOT_SIGNING_KEY` | **empty** | Control Plane. base64url Ed25519 seed. Empty publishes unsigned. |
| `TOOLLAYER_SNAPSHOT_SIGNING_KEY_ID` | **empty** | Must be set together with the key |
| `TOOLLAYER_SNAPSHOT_TRUSTED_KEYS` | **empty** | Runtime. `key_id:base64url_public_key`, comma separated |
| `TOOLLAYER_SNAPSHOT_VERIFICATION` | `required` | `required` or `disabled`. No other value is accepted. |
| `TOOLLAYER_CALLER_AUTH_MODE` | `asserted_header` | `asserted_header` or `verified_token` |
| `TOOLLAYER_CALLER_TOKEN_TRUSTED_KEYS` | **empty** | Required in `verified_token` mode |
| `TOOLLAYER_CALLER_TOKEN_ISSUER` | **empty** | Required in `verified_token` mode |
| `TOOLLAYER_CALLER_TOKEN_AUDIENCE` | **empty** | Required in `verified_token` mode |

The Control Plane refuses to start if the two tokens are equal, and logs a warning if either is
still the shipped placeholder. The Runtime refuses to start if verification is `required` and no
trusted key is configured, and if `verified_token` mode is selected without an issuer, an
audience, and at least one key. Both are deliberate: a security control that turns itself off
when it is hardest to configure is not a control.

### Snapshot signing keys

The digest and the signature answer different questions, and the key management below only
concerns the second. See `docs/threat-model.md` §5.11.

Generate a pair:

```bash
python scripts/generate_signing_key.py --key-id prod-2026-08
```

That prints three environment variables. The **private** one
(`TOOLLAYER_SNAPSHOT_SIGNING_KEY`) goes only to the Control Plane and belongs in a secret
manager; the **public** one (`TOOLLAYER_SNAPSHOT_TRUSTED_KEYS`) goes to every runtime and is
safe to put in ordinary configuration. Nothing writes a key to disk, and no key is committed
anywhere in this repository — `make demo` and `make demo-docker` generate an ephemeral pair per
run, and a CI job fails if a key-shaped file ever appears in the tree.

**Rotating.** `TOOLLAYER_SNAPSHOT_TRUSTED_KEYS` accepts several entries so a rotation does not
need every service to change in the same instant:

1. Add the new public key to every runtime's trusted list, alongside the old one.
2. Switch the Control Plane's signing key to the new pair. Existing snapshots stay valid; new
   ones are signed by the new key.
3. Once no runtime is serving a snapshot signed by the old key, remove it from the trusted
   lists.

Skipping step 1 makes every runtime refuse the next snapshot — which is the correct failure, and
why the overlap exists. `TestKeyRotation` covers both halves.

### Base image and dependency updates

Base images are pinned to a patch version (`python:3.12.8-slim`, `node:20.18.1-alpine`,
`nginx:1.27.3-alpine`) rather than a floating tag, so a rebuild is the build that was tested.
Pinning is only safe if something proposes the next pin: Dependabot watches `docker/`,
`uv`, npm, and the GitHub Actions used here, and opens a weekly grouped pull request. A pinned
image that nobody updates is worse than a floating one.

Python dependencies resolve through `uv.lock` and install from `requirements.lock` with hashes.
After changing `pyproject.toml`:

```bash
make lock
```

A CI job fails if the two lockfiles drift apart.

## 7. Persistence

SQLite by default, in a Docker volume or a local `data/` directory. PostgreSQL works by
changing the connection string and installing the `postgres` extra; nothing in the model
depends on SQLite.

Two SQLite pragmas are set on every connection: `foreign_keys=ON` (off by default there, which
would silently disable every cascade the model relies on) and `journal_mode=WAL`.

Schema comes from Alembic. `create_schema()` exists for tests and first local runs where the
database starts empty every time.

## 8. Startup order

The runtime depends on the control plane being reachable, but does not require it at startup —
it retries and reports `not ready` until a snapshot is available. In Compose, `depends_on` with
`condition: service_healthy` makes the ordering explicit anyway.

A cold start therefore looks like: services up → `/readyz` 503 on the runtime → an
administrator publishes and snapshots → the runtime's next refresh → `/readyz` 200.

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `readyz` 503, `no deployment snapshot` | Nothing published yet | Run `make demo`, or publish and snapshot through the console |
| `destination_not_allowed` | The origin is not allowlisted | Add the exact `scheme://host:port` to `TOOLLAYER_ALLOWED_ORIGINS` |
| `private_address_blocked` | The destination resolves to a non-routable address | Local development: set `TOOLLAYER_ALLOW_LOOPBACK_DESTINATIONS=true`. Never in production. |
| `unauthenticated` on `/admin/v1/...` | Wrong or missing token | Check `TOOLLAYER_ADMIN_TOKEN` |
| Control Plane will not start | The two tokens are equal | Set different values |
| `revision_conflict` | The draft changed since it was read | Reload and reapply |
| `unsupported_spec_feature` | The document uses something the converter refuses | See the table in `docs/control-plane.md` §4 |
| CORS errors in the console | Origin not allowed | Add it to `TOOLLAYER_CONTROL_PLANE_CORS_ORIGINS` |

Service logs from `make demo` land in `.demo-logs/`.

## 10. Verification status

Being precise about this matters more than sounding finished.

| Path | Status |
|---|---|
| `make setup` on a clean clone | **Verified** — Python 3.11 and 3.12 |
| `make test` (292 Python tests) | **Verified** — clean clone and CI |
| `make lint`, `make typecheck` | **Verified** — Ruff check, Ruff format, strict mypy |
| `make demo` (three services, full flow) | **Verified** — default and overridden ports |
| `make capture` (Playwright asset capture) | **Verified** |
| Wheel build, clean install, module and schema import | **Verified** — CI `package` job |
| Docker image build | **Verified** — CI `docker` job builds every image on every run |
| `docker compose up` end to end | **Verified** — CI brings the topology up on its health checks and runs `make demo-docker` through it |
| Containers run as non-root | **Verified** — asserted per service in CI |
| No signing key in any image | **Verified** — build history and filesystem both checked in CI |
| Dependency vulnerability scan | **Verified** — `pip-audit` over `requirements.lock`, `npm audit` at `high` |
| Secret scan over full Git history | **Verified** — gitleaks with `fetch-depth: 0` |
| Static analysis | **Configured** — CodeQL workflow, `security-extended`, weekly and on change |

The Docker path is executed, not merely provided. The `docker` job validates the Compose file,
builds every image, waits on the health checks, runs the same assertion harness the local demo
runs, additionally asserts the runtime is *requiring* and *verifying* snapshot signatures, and
tears the topology down with `if: always()` so a failed assertion does not leave a volume behind.

To reproduce it yourself:

```bash
make demo-docker
```

That generates an ephemeral signing pair, brings the stack up, runs the demonstration, and
tears it down. `KEEP_UP=1 make demo-docker` leaves it running so you can open the console.

## 11. Production limitations

**This topology is not production-ready, and the gaps are specific:**

- **Static bearer tokens between the two services.** No rotation, no expiry, no per-actor
  identity. Snapshot *signing* keys do rotate through the trusted key ring; the admin and
  service tokens do not.
- **Caller identity is asserted, not verified, in this topology.** `verified_token` mode exists
  and is production-shaped, but the compose file runs `asserted_header` and `/healthz` says so.
  Populating the trusted key ring from a real identity provider's JWKS endpoint is not
  implemented.
- **The console ships its token in the browser bundle.** It must sit behind an authenticating
  proxy anywhere real.
- **No TLS in the compose file.** Services speak plaintext HTTP on a private network. Real
  deployment needs TLS termination and `TOOLLAYER_ALLOW_PLAINTEXT_HTTP=false`.
- **SQLite by default.** Fine for one process; use PostgreSQL for concurrent writers.
- **No horizontal scaling story.** The runtime is stateless and would scale, but nothing here
  addresses snapshot distribution across many instances.
- **No metrics or tracing.** Structured logs with redaction, and nothing else.
- **No tamper-evident audit log.** Publication records who and when as ordinary database rows.
  They are mutable by anyone with database access, so this is a record rather than evidence.
- **A DNS time-of-check-to-time-of-use gap remains.** Every resolved address is checked before
  the request is sent, but the transport resolves the name again when it connects. Closing it
  needs a transport that dials the exact verified address while preserving TLS SNI and the
  `Host` header. That is not implemented, and no pinned-IP protection is claimed.
- **Revocation is polling, not push.** Disabling a version takes effect at the consuming
  runtime's next snapshot refresh — bounded by `TOOLLAYER_SNAPSHOT_REFRESH_SECONDS`, not
  immediate.
- **No rate limiting, quotas, or backpressure.**
- **No secret management.** Secrets come from the environment. There is no vault integration
  and no encryption at rest.
- **Single-tenant.**

These are honest gaps in a portfolio project, not an oversight to be discovered later.
`docs/PORTFOLIO_STRATEGY.md` records which were deliberate scope decisions.
