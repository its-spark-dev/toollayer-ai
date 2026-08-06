#!/usr/bin/env bash
#
# Bring the Compose topology up, wait for it, run the demonstration against it, and tear it
# down — including when something fails, so a failed run does not leave containers behind.
#
# The signing key pair is generated here and passed in through the environment. Nothing is
# written to disk and nothing is baked into an image: `docker compose config` would show the
# private half, which is exactly why it is ephemeral and per-run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

CONTROL_PLANE_PORT="${CONTROL_PLANE_PORT:-8080}"
RUNTIME_PORT="${RUNTIME_PORT:-8082}"
KEEP_UP="${KEEP_UP:-0}"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PY_BIN="${REPO_ROOT}/.venv/bin/python"
else
  PY_BIN="$(command -v python3 || command -v python)"
fi

export PYTHONPATH="packages/contracts:packages/openapi-converter:packages/policy-engine:packages/mock-llm:apps/control-plane/backend:apps/runtime:apps/demo-api"

eval "$("${PY_BIN}" scripts/generate_signing_key.py --key-id docker-demo-key)"
echo "signing snapshots with ephemeral key ${TOOLLAYER_SNAPSHOT_SIGNING_KEY_ID}"

cleanup() {
  local status=$?
  if [[ ${status} -ne 0 ]]; then
    echo ""
    echo "The Docker demo failed. Service logs follow." >&2
    docker compose logs --no-color --tail 200 >&2 || true
  fi
  if [[ "${KEEP_UP}" != "1" ]]; then
    docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  exit "${status}"
}
trap cleanup EXIT INT TERM

docker compose up -d --build --wait --wait-timeout 240

# `--wait` gates on the healthchecks, which prove the processes answer. This additionally
# proves the runtime authenticated the artifact it is serving — a runtime that fell back to
# accepting an unsigned snapshot would be healthy and wrong.
echo "checking that the runtime verified its snapshot…"
"${PY_BIN}" - "$RUNTIME_PORT" <<'PYTHON'
import json
import sys
import time
import urllib.error
import urllib.request

port = sys.argv[1]
deadline = time.monotonic() + 60
health = {}
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/healthz", timeout=5) as response:
            health = json.load(response)
        break
    except (urllib.error.URLError, TimeoutError):
        time.sleep(1)
else:
    raise SystemExit("the runtime never answered /healthz")

if health.get("snapshot_verification") != "required":
    raise SystemExit(
        f"the runtime is not requiring signatures: {health.get('snapshot_verification')!r}"
    )
print(f"    runtime trusts key(s): {', '.join(health.get('snapshot_trusted_key_ids') or ['none'])}")
print(f"    caller identity mode : {health.get('caller_authentication')}")
PYTHON

"${PY_BIN}" scripts/demo.py \
  --control-plane-url "http://localhost:${CONTROL_PLANE_PORT}" \
  --runtime-url "http://localhost:${RUNTIME_PORT}" \
  --demo-api-url "http://demo-api:8081" \
  --admin-token "${TOOLLAYER_ADMIN_TOKEN:-dev-admin-token-change-me}"

echo ""
echo "verifying the runtime is serving a signed snapshot…"
"${PY_BIN}" - "$RUNTIME_PORT" <<'PYTHON'
import json
import sys
import urllib.request

port = sys.argv[1]
with urllib.request.urlopen(f"http://localhost:{port}/readyz", timeout=10) as response:
    ready = json.load(response)
if not ready.get("snapshot_signed"):
    raise SystemExit("the runtime is serving an unsigned snapshot")
print(f"    snapshot revision {ready['snapshot_revision']} signed by {ready['snapshot_signing_key_id']}")
PYTHON
