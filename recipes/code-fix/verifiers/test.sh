#!/usr/bin/env bash
# verifiers/test.sh — run the project's test suite and emit structured JSON.
#
# Gating is BASELINE-RELATIVE (delta), not absolute-green: a patch is judged on
# whether it makes the suite WORSE, not on whether the suite is perfectly green.
# Pre-existing failures and broken test environments (wrong interpreter, shadow
# installs, collection/import errors in untouched code) are NOT the patch's fault
# and must not roll back a correct fix. This mirrors SWE-bench's own
# FAIL_TO_PASS / PASS_TO_PASS semantics and fixes the false-reject that discarded
# correct cheap-model patches (see docs: verifier net-negative diagnosis).
#
# Decision table (post = current patched tree, base = HEAD worktree without patch):
#   post green                         -> pass  (nothing to prove)
#   post red,  base ALSO red           -> pass  (pre-existing/env breakage, uninformative)
#   post red,  base green              -> fail  (the patch introduced a regression)
#   post red,  base indeterminate      -> fail  (fall back to absolute; never hide a regression)
#
# Exit codes:  0 pass   1 fail   (callers also read the JSON "pass" field)
#
# Env vars:
#   MINI_ORK_TEST_CMD    explicit command to run (skips auto-detect)
#   MINI_ORK_HOME        path to .mini-ork/ dir (default: .mini-ork)
#   MINI_ORK_RUN_ID      current run id (used in log path)
#   MO_TEST_BASELINE     set to 0 to disable baseline (revert to absolute gating)

set -Eeuo pipefail

MINI_ORK_HOME="${MINI_ORK_HOME:-.mini-ork}"
MINI_ORK_RUN_ID="${MINI_ORK_RUN_ID:-unknown-run}"
LOG_DIR="${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}"
mkdir -p "${LOG_DIR}"
LOG_PATH="${LOG_DIR}/verifier_test.log"
BASE_LOG="${LOG_DIR}/verifier_test_baseline.log"

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

# ── No test runner → skip (pass) ────────────────────────────────────────────────

if [ -z "${CMD}" ]; then
  echo "[test] no test command detected — skipping (pass)" >&2
  printf '{"verifier":"test","pass":true,"evidence_path":null,"error_summary":"no test runner detected — skipped"}\n'
  exit 0
fi

run_suite() { # $1=logfile ; echoes the exit code, never aborts under set -e
  local log="$1" rc
  set +e
  eval "${CMD}" > "${log}" 2>&1
  rc=$?
  set -e
  echo "${rc}"
}

first_fail_line() { # $1=logfile
  grep -m1 -E "(FAIL|Error|failed|assert|ImportError|ModuleNotFound|Interrupted)" "$1" 2>/dev/null \
    | sed 's/"/\\"/g' | head -c 200 || echo "see log"
}

# ── Post-patch run (current working tree = patched) ─────────────────────────────

echo "[test] running: ${CMD}" >&2
POST_RC=$(run_suite "${LOG_PATH}")

emit() { # $1=pass(true|false) $2=reason ; exits with the mapped code
  local pass="$1" reason="$2" summary
  if [ "${pass}" = "true" ]; then summary="${reason}"; else summary="${reason}: $(first_fail_line "${LOG_PATH}")"; fi
  printf '{"verifier":"test","pass":%s,"evidence_path":"%s","error_summary":"%s","post_rc":%s,"base_rc":"%s"}\n' \
    "${pass}" "${LOG_PATH}" "${summary}" "${POST_RC}" "${BASE_RC:-}"
  [ "${pass}" = "true" ] && exit 0 || exit 1
}

if [ "${POST_RC}" -eq 0 ]; then
  emit true "post-patch suite green"
fi

# ── Post-patch failed → establish a baseline to attribute blame ─────────────────
# Baseline = HEAD (the pre-patch tree) run in a throwaway worktree so the real
# working directory is never mutated. Only reached when post-patch is red.

BASE_RC=""
if [ "${MO_TEST_BASELINE:-1}" != "0" ] && git rev-parse --is-inside-work-tree &>/dev/null; then
  WT=""
  if WT=$(mktemp -d 2>/dev/null) && git worktree add -q --detach "${WT}" HEAD &>/dev/null; then
    ( cd "${WT}" && eval "${CMD}" ) > "${BASE_LOG}" 2>&1 && BASE_RC=0 || BASE_RC=$?
    git worktree remove --force "${WT}" &>/dev/null || rm -rf "${WT}" 2>/dev/null || true
  else
    [ -n "${WT}" ] && rm -rf "${WT}" 2>/dev/null || true
  fi
fi

if [ -z "${BASE_RC}" ]; then
  # Could not establish a baseline → fall back to absolute gating (do not hide a regression).
  emit false "post-patch failing; no baseline established (absolute gate)"
elif [ "${BASE_RC}" -ne 0 ]; then
  # Baseline ALSO fails → pre-existing failure / broken test env in untouched code.
  echo "[test] baseline (HEAD) also fails rc=${BASE_RC} — pre-existing/env, not a regression" >&2
  emit true "pre-existing failure: baseline (HEAD) also fails — uninformative test env, not caused by this patch"
else
  # Baseline green, post red → the patch broke something.
  emit false "regression: baseline (HEAD) passed but post-patch fails"
fi
