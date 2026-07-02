#!/usr/bin/env bash
# verifiers/typecheck.sh — run the project's type-checker and emit structured JSON.
#
# Exit codes:
#   0  typecheck passed
#   1  typecheck failed
#
# Env vars:
#   MINI_ORK_TYPECHECK_CMD   explicit command to run (skips auto-detect)
#   MINI_ORK_HOME            path to .mini-ork/ dir (default: .mini-ork)
#   MINI_ORK_RUN_ID          current run id (used in log path)

set -Eeuo pipefail

MINI_ORK_HOME="${MINI_ORK_HOME:-.mini-ork}"
MINI_ORK_RUN_ID="${MINI_ORK_RUN_ID:-unknown-run}"
LOG_DIR="${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}"
mkdir -p "${LOG_DIR}"
LOG_PATH="${LOG_DIR}/verifier_typecheck.log"

# ── Detect command ─────────────────────────────────────────────────────────────

# Returns 0 if the cwd looks like a TypeScript project, else 1.
# Marker rules: tsconfig.json present, OR package.json declares typescript
# (dep/devDep), OR package.json has a typecheck-style script.
# A globally-installed tsc is NOT a marker — gate it on real project intent
# (regression: bash/Python repos with tsc on PATH short-circuited on bare tsc).
_has_ts_marker() {
  if [ -f "tsconfig.json" ]; then return 0; fi
  if [ -f "package.json" ] && command -v jq &>/dev/null; then
    if jq -e '.dependencies.typescript // .devDependencies.typescript // empty' package.json &>/dev/null; then
      return 0
    fi
    local scripts
    scripts=$(jq -r '.scripts // {} | keys[]' package.json 2>/dev/null || true)
    for candidate in typecheck type-check tsc check; do
      if echo "${scripts}" | grep -qx "${candidate}"; then return 0; fi
    done
  fi
  return 1
}

# Returns 0 if the cwd has a CONFIGURED mypy setup, else 1.
# Marker rules: mypy.ini present, OR setup.cfg with a [mypy] section, OR
# pyproject.toml with a [tool.mypy] section. A bare pyproject.toml is NOT a
# marker — nearly every Python repo has one, and `mypy .` on an unconfigured
# tree scans fixtures/vendored code and false-fails (regression: this bash repo
# has a mini_ork/ package + pyproject.toml but no mypy config, so `mypy .`
# tripped on duplicate fixture modules).
_has_mypy_marker() {
  if [ -f "mypy.ini" ]; then return 0; fi
  if [ -f "setup.cfg" ] && grep -q '^\[mypy\]' setup.cfg 2>/dev/null; then return 0; fi
  if [ -f "pyproject.toml" ] && grep -q '^\[tool\.mypy\]' pyproject.toml 2>/dev/null; then return 0; fi
  return 1
}

detect_typecheck_cmd() {
  # Explicit override wins.
  if [ -n "${MINI_ORK_TYPECHECK_CMD:-}" ]; then
    echo "${MINI_ORK_TYPECHECK_CMD}"
    return
  fi

  # npm / pnpm / yarn — check package.json scripts first.
  if [ -f "package.json" ]; then
    if command -v jq &>/dev/null; then
      local scripts
      scripts=$(jq -r '.scripts // {} | keys[]' package.json 2>/dev/null || true)
      for candidate in typecheck type-check tsc check; do
        if echo "${scripts}" | grep -qx "${candidate}"; then
          if command -v pnpm &>/dev/null; then echo "pnpm run ${candidate}"; return; fi
          if command -v npm  &>/dev/null; then echo "npm run ${candidate}";  return; fi
        fi
      done
    fi
  fi

  # TypeScript project marker required before we trust a tsc binary.
  if _has_ts_marker; then
    if command -v tsc &>/dev/null; then echo "tsc --noEmit"; return; fi
    if [ -x "./node_modules/.bin/tsc" ]; then echo "./node_modules/.bin/tsc --noEmit"; return; fi
  fi

  # Python mypy — require a configured mypy, not just any pyproject.toml.
  if command -v mypy &>/dev/null && _has_mypy_marker; then
    echo "mypy ."; return
  fi

  # Rust
  if command -v cargo &>/dev/null && [ -f "Cargo.toml" ]; then echo "cargo check"; return; fi

  # Go
  if command -v go &>/dev/null && [ -f "go.mod" ]; then echo "go build ./..."; return; fi

  # Nothing found — skip and pass
  echo ""
}

CMD=$(detect_typecheck_cmd)

# ── Run ────────────────────────────────────────────────────────────────────────

if [ -z "${CMD}" ]; then
  echo "[typecheck] no typecheck command detected — skipping (pass)" >&2
  printf '{"verifier":"typecheck","pass":true,"evidence_path":null,"error_summary":"no typecheck tool detected — skipped"}\n'
  exit 0
fi

echo "[typecheck] running: ${CMD}" >&2
set +e
eval "${CMD}" > "${LOG_PATH}" 2>&1
EXIT_CODE=$?
set -e

# ── Emit JSON ──────────────────────────────────────────────────────────────────

if [ "${EXIT_CODE}" -eq 0 ]; then
  PASS="true"
  ERROR_SUMMARY=""
else
  PASS="false"
  # Extract first error line for the summary (portable, no jq required here).
  ERROR_SUMMARY=$(grep -m1 "error" "${LOG_PATH}" 2>/dev/null \
    | sed 's/"/\\"/g' \
    | head -c 200 \
    || echo "see log")
fi

printf '{"verifier":"typecheck","pass":%s,"evidence_path":"%s","error_summary":"%s"}\n' \
  "${PASS}" \
  "${LOG_PATH}" \
  "${ERROR_SUMMARY}"

exit "${EXIT_CODE}"
