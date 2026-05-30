#!/usr/bin/env bash
# bdd-first-delivery — Playwright BDD verifier
#
# Runs the Playwright spec for one sub-epic from within the implementer's
# working directory. Emits a JSON verdict to stdout and writes it to
# the path specified by MINI_ORK_VERDICT_DIR (if set).
#
# Required env:
#   SUB_EPIC_ID     — e.g. "USER-SETTINGS-V2-B"
#   WORKDIR         — absolute path to the implementer's worktree
#
# Optional env:
#   MINI_ORK_PLAYWRIGHT_CMD   — override the playwright invocation
#                               (default: npx playwright test)
#   MINI_ORK_VERDICT_DIR      — directory to write bdd-verdict.json into
#   MINI_ORK_BDD_TIMEOUT_SEC  — wall-clock timeout for the playwright run
#                               (default: 300)
#
# Exit codes:
#   0  — verdict emitted (check .pass field for actual pass/fail)
#   1  — setup failure (missing env, workdir not found, etc.)

set -Eeuo pipefail

# ─── Validate required env ───────────────────────────────────────────────

if [ -z "${SUB_EPIC_ID:-}" ]; then
  printf '{"verifier":"playwright","error":"SUB_EPIC_ID not set","pass":false}\n'
  exit 1
fi

if [ -z "${WORKDIR:-}" ]; then
  printf '{"verifier":"playwright","error":"WORKDIR not set","pass":false}\n'
  exit 1
fi

if [ ! -d "${WORKDIR}" ]; then
  printf '{"verifier":"playwright","error":"WORKDIR does not exist: %s","pass":false}\n' "${WORKDIR}"
  exit 1
fi

# ─── Spec discovery ──────────────────────────────────────────────────────
# Spec must live at e2e/<SUB_EPIC_ID>_*.spec.ts in the worktree.

spec_pattern="${WORKDIR}/e2e/${SUB_EPIC_ID}_"
spec_file=""
# shellcheck disable=SC2086
spec_file=$(ls -1 ${spec_pattern}*.spec.ts 2>/dev/null | head -1 || true)

if [ -z "${spec_file}" ]; then
  # No spec found: this is a legitimate BE-only skip, not a failure.
  printf '{"verifier":"playwright","sub_epic_id":"%s","pass":true,"skipped":true,"reason":"no spec found at e2e/%s_*.spec.ts (BE-only sub-epic or spec not yet written)","scenarios_run":0,"scenarios_passed":0,"scenarios_failed":0,"duration_ms":0,"ran_at":"%s"}\n' \
    "${SUB_EPIC_ID}" "${SUB_EPIC_ID}" "$(date -u +%FT%TZ)"
  exit 0
fi

# ─── Configuration ───────────────────────────────────────────────────────

playwright_cmd="${MINI_ORK_PLAYWRIGHT_CMD:-npx playwright test}"
timeout_sec="${MINI_ORK_BDD_TIMEOUT_SEC:-300}"
verdict_dir="${MINI_ORK_VERDICT_DIR:-}"
json_results_file=""

if [ -n "${verdict_dir}" ]; then
  mkdir -p "${verdict_dir}"
  json_results_file="${verdict_dir}/playwright-results.json"
fi

# ─── Run ─────────────────────────────────────────────────────────────────

started_ms="$(date +%s)000"
exit_code=0
log_file=""

if [ -n "${verdict_dir}" ]; then
  log_file="${verdict_dir}/playwright-runner.log"
fi

(
  cd "${WORKDIR}" || exit 1

  timeout_bin=""
  command -v gtimeout >/dev/null 2>&1 && timeout_bin=gtimeout
  command -v timeout  >/dev/null 2>&1 && [ -z "${timeout_bin}" ] && timeout_bin=timeout

  reporter_args=()
  if [ -n "${json_results_file}" ]; then
    reporter_args=(--reporter=json)
    export PLAYWRIGHT_JSON_OUTPUT_NAME="${json_results_file}"
  fi

  if [ -n "${log_file}" ]; then
    ${timeout_bin:-} ${timeout_bin:+--kill-after=30s "${timeout_sec}"} \
      ${playwright_cmd} "${spec_file}" "${reporter_args[@]}" \
      >"${log_file}" 2>&1
  else
    ${timeout_bin:-} ${timeout_bin:+--kill-after=30s "${timeout_sec}"} \
      ${playwright_cmd} "${spec_file}" "${reporter_args[@]}"
  fi
) || exit_code=$?

ended_ms="$(date +%s)000"
duration_ms=$(( ended_ms - started_ms ))

# ─── Parse results ───────────────────────────────────────────────────────

total=0
passed=0
failed=0
skipped_count=0

if [ -n "${json_results_file}" ] && [ -f "${json_results_file}" ]; then
  total=$(jq -r '(.stats.expected // 0) + (.stats.unexpected // 0) + (.stats.flaky // 0) + (.stats.skipped // 0)' "${json_results_file}" 2>/dev/null || echo 0)
  passed=$(jq -r '.stats.expected // 0' "${json_results_file}" 2>/dev/null || echo 0)
  failed=$(jq -r '(.stats.unexpected // 0) + (.stats.flaky // 0)' "${json_results_file}" 2>/dev/null || echo 0)
  skipped_count=$(jq -r '.stats.skipped // 0' "${json_results_file}" 2>/dev/null || echo 0)
fi

# Determine pass/fail.
if [ "${exit_code}" -eq 0 ] && [ "${failed}" -eq 0 ]; then
  verdict_pass=true
else
  verdict_pass=false
fi

# Build failure summary array (top 5).
failure_summary="[]"
if [ -n "${json_results_file}" ] && [ -f "${json_results_file}" ] && [ "${failed}" -gt 0 ]; then
  failure_summary=$(jq -c '
    [.suites[]?.suites[]?.specs[]?
      | select(.tests[]?.results[]?.status != "passed")
      | {
          spec: .file,
          title: .title,
          error: (.tests[0].results[0].error.message // "no error message")
        }
    ][0:5]
  ' "${json_results_file}" 2>/dev/null || echo "[]")
fi

# ─── Emit verdict JSON ───────────────────────────────────────────────────

evidence_path="${json_results_file:-}"
[ -z "${evidence_path}" ] && [ -n "${log_file}" ] && evidence_path="${log_file}"

verdict_json=$(jq -n \
  --arg  verifier       "playwright" \
  --arg  sub_epic_id    "${SUB_EPIC_ID}" \
  --arg  spec_file      "${spec_file}" \
  --argjson pass        "${verdict_pass}" \
  --argjson exit_code   "${exit_code}" \
  --argjson total       "${total}" \
  --argjson passed      "${passed}" \
  --argjson failed      "${failed}" \
  --argjson skipped     "${skipped_count}" \
  --argjson duration_ms "${duration_ms}" \
  --argjson failures    "${failure_summary}" \
  --arg  evidence_path  "${evidence_path}" \
  --arg  ran_at         "$(date -u +%FT%TZ)" \
  '{
    verifier:        $verifier,
    sub_epic_id:     $sub_epic_id,
    pass:            $pass,
    skipped:         false,
    spec_file:       $spec_file,
    exit_code:       $exit_code,
    scenarios_run:   $total,
    scenarios_passed: $passed,
    scenarios_failed: $failed,
    scenarios_skipped: $skipped,
    duration_ms:     $duration_ms,
    failure_summary: $failures,
    evidence_path:   $evidence_path,
    ran_at:          $ran_at
  }')

# Write to verdict dir if set.
if [ -n "${verdict_dir}" ]; then
  printf '%s\n' "${verdict_json}" > "${verdict_dir}/bdd-verdict.json"
fi

# Always emit to stdout (orchestrator reads this).
printf '%s\n' "${verdict_json}"

# Exit 0 so the orchestrator can inspect the .pass field itself.
# The orchestrator decides whether a FAIL verdict blocks the pipeline.
exit 0
