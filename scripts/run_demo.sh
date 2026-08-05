#!/usr/bin/env bash
#
# Start the three services locally, run the demonstration, and shut everything down.
#
# Docker is the other way to run this (`make demo-docker`). This script exists so the demo
# also works on a machine without Docker, and so the logs of all three services land in one
# place when something goes wrong.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

VENV_BIN="${REPO_ROOT}/.venv/bin"
LOG_DIR="${REPO_ROOT}/.demo-logs"

CONTROL_PLANE_PORT="${CONTROL_PLANE_PORT:-8080}"
DEMO_API_PORT="${DEMO_API_PORT:-8081}"
RUNTIME_PORT="${RUNTIME_PORT:-8082}"

if [[ ! -x "${VENV_BIN}/uvicorn" ]]; then
  echo "The virtualenv is not set up. Run 'make setup' first." >&2
  exit 1
fi

export PYTHONPATH="packages/contracts:packages/openapi-converter:packages/policy-engine:packages/mock-llm:apps/control-plane/backend:apps/runtime:apps/demo-api"

# The demo calls a service on localhost, so the loopback and plaintext escape hatches are
# enabled here explicitly. Both are off by default; docs/threat-model.md explains why they
# exist and why a real deployment leaves them off.
export TOOLLAYER_CONTROL_PLANE_DATABASE_URL="sqlite:///${REPO_ROOT}/data/demo.db"
export TOOLLAYER_ADMIN_TOKEN="${TOOLLAYER_ADMIN_TOKEN:-dev-admin-token-change-me}"
export TOOLLAYER_SERVICE_TOKEN="${TOOLLAYER_SERVICE_TOKEN:-dev-service-token-change-me}"
export TOOLLAYER_CONTROL_PLANE_URL="http://localhost:${CONTROL_PLANE_PORT}"
export TOOLLAYER_DEPLOYMENT_KEY="demo-workspace"
export TOOLLAYER_ALLOWED_ORIGINS="http://localhost:${DEMO_API_PORT}"
export TOOLLAYER_ALLOW_PLAINTEXT_HTTP="true"
export TOOLLAYER_ALLOW_LOOPBACK_DESTINATIONS="true"
export TOOLLAYER_SNAPSHOT_REFRESH_SECONDS="5"

mkdir -p "${LOG_DIR}" "${REPO_ROOT}/data"
rm -f "${REPO_ROOT}/data/demo.db"

PIDS=()

cleanup() {
  local status=$?
  for pid in "${PIDS[@]:-}"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
  done
  if [[ ${status} -ne 0 ]]; then
    echo ""
    echo "The demo failed. Service logs are in ${LOG_DIR}/." >&2
  fi
  exit "${status}"
}
trap cleanup EXIT INT TERM

start() {
  local name="$1" module="$2" port="$3"
  echo "starting ${name} on port ${port}…"
  "${VENV_BIN}/uvicorn" "${module}" --host 127.0.0.1 --port "${port}" --log-level warning \
    >"${LOG_DIR}/${name}.log" 2>&1 &
  PIDS+=("$!")
}

wait_for_health() {
  local name="$1" port="$2"
  for _ in $(seq 1 60); do
    if curl --silent --fail "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "${name} did not become healthy; see ${LOG_DIR}/${name}.log" >&2
  return 1
}

start demo-api demo_api.main:app "${DEMO_API_PORT}"
start control-plane control_plane.main:app "${CONTROL_PLANE_PORT}"
start runtime runtime_service.main:app "${RUNTIME_PORT}"

wait_for_health demo-api "${DEMO_API_PORT}"
wait_for_health control-plane "${CONTROL_PLANE_PORT}"
wait_for_health runtime "${RUNTIME_PORT}"

"${VENV_BIN}/python" scripts/demo.py \
  --control-plane-url "http://127.0.0.1:${CONTROL_PLANE_PORT}" \
  --runtime-url "http://127.0.0.1:${RUNTIME_PORT}" \
  --demo-api-url "http://localhost:${DEMO_API_PORT}" \
  --admin-token "${TOOLLAYER_ADMIN_TOKEN}"
