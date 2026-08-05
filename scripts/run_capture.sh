#!/usr/bin/env bash
#
# Start every service, build and serve the console, capture the README's assets, and shut
# everything down again.
#
# The whole point is reproducibility: `make capture` from a clean clone regenerates every
# image in docs/assets/ from the current code, so an image can never quietly drift away from
# what the application does.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

FRONTEND="${REPO_ROOT}/apps/control-plane/frontend"
LOG_DIR="${REPO_ROOT}/.capture-logs"

CONTROL_PLANE_PORT="${CONTROL_PLANE_PORT:-8080}"
DEMO_API_PORT="${DEMO_API_PORT:-8081}"
RUNTIME_PORT="${RUNTIME_PORT:-8082}"
CONSOLE_PORT="${CONSOLE_PORT:-5173}"

# Prefer the project virtualenv, but fall back to whatever is on PATH. CI installs into the
# runner's Python rather than creating a .venv, and a demo script that only works one of those
# two ways is a demo script that quietly stops being run.
if [[ -x "${REPO_ROOT}/.venv/bin/uvicorn" ]]; then
  PY_BIN="${REPO_ROOT}/.venv/bin/python"
  UVICORN_BIN="${REPO_ROOT}/.venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
  PY_BIN="$(command -v python3 || command -v python)"
  UVICORN_BIN="$(command -v uvicorn)"
else
  echo "Neither .venv nor an installed uvicorn was found. Run 'make setup' first." >&2
  exit 1
fi

if ! "${PY_BIN}" -c "import playwright" 2>/dev/null; then
  echo "Playwright is not installed. Run:" >&2
  echo "    pip install -e '.[capture]' && playwright install chromium" >&2
  exit 1
fi

export PYTHONPATH="packages/contracts:packages/openapi-converter:packages/policy-engine:packages/mock-llm:apps/control-plane/backend:apps/runtime:apps/demo-api"

# A dedicated database so a capture never depends on, or disturbs, whatever the demo left
# behind. It is deleted first so the run always starts from the same state.
export TOOLLAYER_CONTROL_PLANE_DATABASE_URL="sqlite:///${REPO_ROOT}/data/capture.db"
export TOOLLAYER_ADMIN_TOKEN="${TOOLLAYER_ADMIN_TOKEN:-dev-admin-token-change-me}"
export TOOLLAYER_SERVICE_TOKEN="${TOOLLAYER_SERVICE_TOKEN:-dev-service-token-change-me}"
export TOOLLAYER_CONTROL_PLANE_URL="http://localhost:${CONTROL_PLANE_PORT}"
export TOOLLAYER_DEPLOYMENT_KEY="demo-workspace"
export TOOLLAYER_ALLOWED_ORIGINS="http://localhost:${DEMO_API_PORT}"
export TOOLLAYER_ALLOW_PLAINTEXT_HTTP="true"
export TOOLLAYER_ALLOW_LOOPBACK_DESTINATIONS="true"
export TOOLLAYER_SNAPSHOT_REFRESH_SECONDS="5"
# Both spellings of the loopback host: the browser treats them as different origins, and a
# mismatch here surfaces as a silent CORS failure rather than an error the console can show.
export TOOLLAYER_CONTROL_PLANE_CORS_ORIGINS="http://localhost:${CONSOLE_PORT},http://127.0.0.1:${CONSOLE_PORT}"
export TOOLLAYER_RUNTIME_CORS_ORIGINS="http://localhost:${CONSOLE_PORT},http://127.0.0.1:${CONSOLE_PORT}"

export VITE_CONTROL_PLANE_URL="http://localhost:${CONTROL_PLANE_PORT}"
export VITE_RUNTIME_URL="http://localhost:${RUNTIME_PORT}"
export VITE_ADMIN_TOKEN="${TOOLLAYER_ADMIN_TOKEN}"

mkdir -p "${LOG_DIR}" "${REPO_ROOT}/data" "${REPO_ROOT}/docs/assets"
rm -f "${REPO_ROOT}/data/capture.db"

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
    echo "" >&2
    echo "Capture failed. Service logs are in ${LOG_DIR}/." >&2
  fi
  exit "${status}"
}
trap cleanup EXIT INT TERM

start_service() {
  local name="$1" module="$2" port="$3"
  echo "starting ${name} on port ${port}…"
  "${UVICORN_BIN}" "${module}" --host 127.0.0.1 --port "${port}" --log-level warning \
    >"${LOG_DIR}/${name}.log" 2>&1 &
  PIDS+=("$!")
}

wait_for() {
  local name="$1" url="$2"
  for _ in $(seq 1 90); do
    if curl --silent --fail "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "${name} did not become available at ${url}" >&2
  return 1
}

start_service demo-api demo_api.main:app "${DEMO_API_PORT}"
start_service control-plane control_plane.main:app "${CONTROL_PLANE_PORT}"
start_service runtime runtime_service.main:app "${RUNTIME_PORT}"

wait_for demo-api "http://127.0.0.1:${DEMO_API_PORT}/healthz"
wait_for control-plane "http://127.0.0.1:${CONTROL_PLANE_PORT}/healthz"
wait_for runtime "http://127.0.0.1:${RUNTIME_PORT}/healthz"

# The console is captured from a production build rather than the dev server, so the images
# show what a visitor would actually see — no dev overlay, no unminified timing differences.
echo "building the console…"
if [[ ! -d "${FRONTEND}/node_modules" ]]; then
  (cd "${FRONTEND}" && npm ci --silent)
fi
(cd "${FRONTEND}" && npm run build --silent)

echo "serving the console on port ${CONSOLE_PORT}…"
"${PY_BIN}" -m http.server "${CONSOLE_PORT}" \
  --directory "${FRONTEND}/dist" --bind 127.0.0.1 \
  >"${LOG_DIR}/console.log" 2>&1 &
PIDS+=("$!")
wait_for console "http://127.0.0.1:${CONSOLE_PORT}/"

"${PY_BIN}" scripts/capture_portfolio_assets.py \
  --console-url "http://localhost:${CONSOLE_PORT}" \
  --control-plane-url "http://127.0.0.1:${CONTROL_PLANE_PORT}" \
  --runtime-url "http://127.0.0.1:${RUNTIME_PORT}" \
  --demo-api-url "http://localhost:${DEMO_API_PORT}" \
  --admin-token "${TOOLLAYER_ADMIN_TOKEN}" \
  "$@"
