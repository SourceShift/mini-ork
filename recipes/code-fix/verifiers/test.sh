#!/usr/bin/env bash
# verifiers/test.sh — run the project's test suite and emit structured JSON.
#
# Exit codes:
#   0  tests passed
#   1  tests failed
#
# Env vars:
#   MINI_ORK_TEST_CMD    explicit command to run (skips auto-detect)
#   MINI_ORK_HOME        path to .mini-ork/ dir (default: .mini-ork)
#   MINI_ORK_RUN_ID      current run id (used in log path)

set -Eeuo pipefail

MINI_ORK_HOME="${MINI_ORK_HOME:-.mini-ork}"
MINI_ORK_RUN_ID="${MINI_ORK_RUN_ID:-unknown-run}"
LOG_DIR="${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}"
mkdir -p "${LOG_DIR}"
LOG_PATH="${LOG_DIR}/verifier_test.log"

# ── Detect command ─────────────────────────────────────────────────────────────

detect_test_cmd() {
  # Explicit override wins.
  if [ -n "${MINI_ORK_TEST_CMD:-}" ]; then
    echo "${MINI_ORK_TEST_CMD}"
    return
  fi

  # npm / pnpm / yarn — check package.json scripts first.
  if [ -f "package.json" ]; then
    if command -v jq &>/dev/null; then
      local scripts
      scripts=$(jq -r '.scripts // {} | keys[]' package.json 2>/dev/null || true)
      for candidate in test "test:unit" "test:ci"; do
        if echo "${scripts}" | grep -qx "${candidate}"; then
          if command -v pnpm &>/dev/null; then echo "pnpm run ${candidate}"; return; fi
          if command -v npm  &>/dev/null; then echo "npm test";               return; fi
        fi
      done
    fi
    # Fallback: npm test without jq
    if command -v pnpm &>/dev/null; then echo "pnpm test"; return; fi
    if command -v npm  &>/dev/null; then echo "npm test";  return; fi
  fi

  # Python pytest
  if command -v pytest &>/dev/null; then echo "pytest"; return; fi
  if command -v python3 &>/dev/null && python3 -m pytest --version &>/dev/null 2>&1; then
    echo "python3 -m pytest"; return
  fi

  # Rust
  if command -v cargo &>/dev/null && [ -f "Cargo.toml" ]; then echo "cargo test"; return; fi

  # Go
  if command -v go &>/dev/null && [ -f "go.mod" ]; then echo "go test ./..."; return; fi

  # Ruby
  if command -v bundle &>/dev/null && [ -f "Gemfile" ]; then echo "bundle exec rake test"; return; fi

  # Nothing found — skip and pass
  echo ""
}

CMD=$(detect_test_cmd)

# ── Run ────────────────────────────────────────────────────────────────────────

if [ -z "${CMD}" ]; then
  echo "[test] no test command detected — skipping (pass)" >&2
  printf '{"verifier":"test","pass":true,"evidence_path":null,"error_summary":"no test runner detected — skipped"}\n'
  exit 0
fi

echo "[test] running: ${CMD}" >&2
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
  # Capture first failure line for the summary.
  ERROR_SUMMARY=$(grep -m1 -E "(FAIL|Error|failed|assert)" "${LOG_PATH}" 2>/dev/null \
    | sed 's/"/\\"/g' \
    | head -c 200 \
    || echo "see log")
fi

printf '{"verifier":"test","pass":%s,"evidence_path":"%s","error_summary":"%s"}\n' \
  "${PASS}" \
  "${LOG_PATH}" \
  "${ERROR_SUMMARY}"

exit "${EXIT_CODE}"
