#!/usr/bin/env bash
#
# Generate a CycloneDX software bill of materials for the locked dependency graph.
#
# Generated from requirements.lock rather than from the installed environment, so the SBOM
# describes what a clean install produces rather than what happens to be on this machine.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

OUT_DIR="${SBOM_OUT_DIR:-${REPO_ROOT}/dist/sbom}"
mkdir -p "${OUT_DIR}"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PY_BIN="${REPO_ROOT}/.venv/bin/python"
else
  PY_BIN="$(command -v python3 || command -v python)"
fi

"${PY_BIN}" -m pip install --quiet "cyclonedx-bom>=4.4"

# The `requirements` input reads the lockfile directly. `--no-validate` is deliberately not
# passed: a SBOM that does not validate against its own schema is not evidence of anything.
"${PY_BIN}" -m cyclonedx_py requirements requirements.lock \
  --output-format JSON \
  --output-reproducible \
  -o "${OUT_DIR}/python-dependencies.cdx.json"

echo "wrote ${OUT_DIR}/python-dependencies.cdx.json"

# The console's dependency graph is separate and comes from its own lockfile.
if command -v npm >/dev/null 2>&1; then
  (
    cd apps/control-plane/frontend
    npm ls --all --json --package-lock-only >"${OUT_DIR}/console-dependencies.json" 2>/dev/null || true
  )
  echo "wrote ${OUT_DIR}/console-dependencies.json"
fi
