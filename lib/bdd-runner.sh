#!/usr/bin/env bash
# mini-ork BDD runner — runs Playwright specs scoped to one epic, emits
# bdd-verdict.json next to the reviewer's verdict.json.
#
# Phase A.1 of the v2 BDD-first design.
# Phase A.1 only: accepts hand-written specs at e2e/<EPIC>_*.spec.ts
# (no spec-author agent yet — that's A.2). If no spec is found, returns
# pass=true skipped=true so BE-only epics aren't penalized.
#
# Source from dispatch.sh; not meant to run alone.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Caller must have set: REPO_ROOT, AGENTFLOW_DIR, JOB_ID

# Phase 14.3 mo_events emitter (no-op if missing).
source "${AGENTFLOW_DIR:-$REPO_ROOT/${MINI_ORK_HOME:-.mini-ork}}/lib/mo-event.sh" 2>/dev/null || true

# ─── Spec discovery ─────────────────────────────────────────────────────
# Returns space-separated list of spec paths under e2e/ matching the epic.
# Convention: <EPIC-ID>_*.spec.ts (e.g. IM-A_style_panel.spec.ts).
mo_bdd_specs_for_epic() {
  local epic="$1"
  # Search relative to repo root. Worktree-specific specs are intentionally
  # NOT scanned — we run the canonical spec from the worker's worktree (so
  # the worker's commits are exercised, but the spec text comes from the
  # checked-in tree).
  local worktree="$2"
  local pattern="${worktree}/e2e/${epic}_"
  ls -1 "${pattern}"*.spec.ts 2>/dev/null || true
}

# ─── BDD run (foreground, scoped to one epic) ───────────────────────────
# Emits: <run-dir>/iter-<N>/bdd-verdict.json
# Args: epic worktree iter
mo_run_bdd() {
  local epic="$1" worktree="$2" iter="$3"
  local run_dir
  run_dir="$(mo_run_dir "$epic")"
  local iter_dir="$run_dir/iter-$iter"
  local verdict_path="$iter_dir/bdd-verdict.json"
  local log_path="$iter_dir/bdd-runner.log"

  mkdir -p "$iter_dir"

  local specs
  specs="$(mo_bdd_specs_for_epic "$epic" "$worktree")"

  if [ -z "$specs" ]; then
    echo "[mini-ork] BDD skip epic=$epic iter=$iter (no spec at e2e/${epic}_*.spec.ts)" >&2
    cat > "$verdict_path" <<EOF
{
  "verdict": "PASS",
  "skipped": true,
  "reason": "no spec found at e2e/${epic}_*.spec.ts (BE-only epic or spec missing)",
  "epic": "$epic",
  "iter": $iter,
  "scenarios_run": 0,
  "scenarios_passed": 0,
  "scenarios_failed": 0,
  "duration_ms": 0,
  "ran_at": "$(date -u +%FT%TZ)"
}
EOF
    return 0
  fi

  # ── Cross-cutting BDD gate (bdd_role + depends_on) ───────────────────
  # Decompose plans tag each sub-epic with bdd_role: leaf|integration|spec.
  #   leaf        — never run BDD (component too small to test alone)
  #   integration — run BDD only after all depends_on are merged
  #   spec        — same gating as integration
  # Without this gate, leaf worktrees fail BDD against incomplete code
  # and cascade into wasted reflection-refiner + L6 cycles.
  #
  # Disabled with MO_BDD_IGNORE_DEPENDS=1.
  local _db="${MINI_ORK_DB:-$AGENTFLOW_DIR/state.db}"
  if [ "${MO_BDD_IGNORE_DEPENDS:-0}" -ne 1 ]; then
    local kickoff_path
    kickoff_path=$(sqlite3 "$_db" \
      "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
    local decompose_json
    decompose_json="$(dirname "$REPO_ROOT/$kickoff_path")/$(echo "$epic" | sed -E 's/-[A-Z]+$//').decompose.json"
    if [ -f "$decompose_json" ]; then
      local bdd_role
      bdd_role=$(jq -r --arg eid "$epic" '
        .sub_epics[] | select(.id == $eid) | .bdd_role // "integration"
      ' "$decompose_json" 2>/dev/null)
      bdd_role="${bdd_role:-integration}"

      # leaf → always skip
      if [ "$bdd_role" = "leaf" ]; then
        echo "[mini-ork] BDD skip epic=$epic iter=$iter — bdd_role=leaf (validated via DoD probes, not e2e)" >&2
        cat > "$verdict_path" <<EOF
{
  "verdict": "PASS",
  "skipped": true,
  "reason": "bdd_role=leaf — component scope too small for standalone e2e; validated via integration epic's spec",
  "epic": "$epic",
  "iter": $iter,
  "scenarios_run": 0, "scenarios_passed": 0, "scenarios_failed": 0,
  "duration_ms": 0, "ran_at": "$(date -u +%FT%TZ)"
}
EOF
        return 0
      fi

      # integration / spec → only run if all depends_on are merged
      local unmerged_deps
      unmerged_deps=$(jq -r --arg eid "$epic" '
        .sub_epics[] | select(.id == $eid) | .depends_on[]?
      ' "$decompose_json" 2>/dev/null | while read -r dep; do
        [ -z "$dep" ] && continue
        local dep_status
        dep_status=$(sqlite3 "$_db" \
          "SELECT status FROM epics WHERE id='$dep';" 2>/dev/null)
        [ "$dep_status" = "done" ] || echo "$dep"
      done | tr '\n' ',' | sed 's/,$//')

      if [ -n "$unmerged_deps" ]; then
        echo "[mini-ork] BDD skip epic=$epic iter=$iter — bdd_role=$bdd_role, deps not merged: $unmerged_deps" >&2
        cat > "$verdict_path" <<EOF
{
  "verdict": "PASS",
  "skipped": true,
  "reason": "bdd_role=$bdd_role, depends_on=[$unmerged_deps] not yet merged — defer until integration is whole",
  "epic": "$epic",
  "iter": $iter,
  "scenarios_run": 0, "scenarios_passed": 0, "scenarios_failed": 0,
  "duration_ms": 0, "ran_at": "$(date -u +%FT%TZ)"
}
EOF
        return 0
      fi
    fi
  fi

  # T4: cache lookup. Hash = spec_bodies + worker_HEAD. If neither the
  # spec nor the worker's commits changed since the last BDD run, the
  # verdict is identical — skip the ~2-min preview+test cycle.
  if [ "${MO_SKIP_CACHE:-0}" -ne 1 ]; then
    local specs_concat=""
    for s in $specs; do specs_concat="$specs_concat$(cat "$s")"$'\x1e'; done
    local worker_head
    worker_head=$(git -C "$worktree" rev-parse HEAD 2>/dev/null || echo "")
    local cache_hash
    cache_hash=$(printf '%s\x1e%s' "$specs_concat" "$worker_head" | mo_cache_input_hash)
    local cached
    cached=$(mo_cache_lookup bdd-runner "$epic" "$iter" "$cache_hash")
    if [ -n "$cached" ] && [ -f "$cached" ]; then
      cp "$cached" "$verdict_path"
      mo_cache_record_hit bdd-runner "$epic" "$iter" "$cache_hash"
      local v
      v=$(jq -r '.verdict' "$verdict_path")
      echo "[mini-ork] CACHE HIT: bdd-runner epic=$epic iter=$iter verdict=$v (skipped Playwright)" >&2
      return 0
    fi
  fi

  echo "[mini-ork] BDD run epic=$epic iter=$iter (specs: $(echo "$specs" | wc -w | tr -d ' '))" >&2

  # ─── Stub-TODO detector ─────────────────────────────────────────────
  # If any trace-spec.yaml referenced by the specs still contains the
  # decompose-emitted placeholder pattern (`<...>` style), refuse to
  # run Playwright. Why: a worker can leave the stub unfilled, the
  # spec passes (assertTraceSpec walks an empty contract), and the
  # reviewer marks trace_status='pass' on a false-pass.
  #
  # Disable with MO_BDD_SKIP_STUB_CHECK=1.
  if [ "${MO_BDD_SKIP_STUB_CHECK:-0}" -ne 1 ]; then
    local _stub_problems=""
    for _s in $specs; do
      local _refd_specs
      _refd_specs=$(grep -oE "e2e/_specs/[a-zA-Z0-9._-]+\.trace-spec\.yaml" "$_s" 2>/dev/null | sort -u || true)
      for _spec_yaml in $_refd_specs; do
        local _abs="${REPO_ROOT:-$(pwd)}/$_spec_yaml"
        [ -f "$_abs" ] || continue
        if grep -qE '<[a-z_]+>|TODO|FILL IN|<must_have_attrs>' "$_abs" 2>/dev/null; then
          _stub_problems="${_stub_problems}${_spec_yaml}: contains unfilled placeholders\n"
        fi
      done
    done
    if [ -n "$_stub_problems" ]; then
      cat > "$verdict_path" <<EOF
{
  "verdict": "FAIL",
  "skipped": false,
  "reason": "trace-spec stub still has placeholder TODOs — fill them in before running",
  "epic": "$epic",
  "iter": $iter,
  "scenarios_run": 0, "scenarios_passed": 0, "scenarios_failed": 1,
  "duration_ms": 0,
  "stub_problems": "$(printf '%b' "$_stub_problems" | tr '\n' ';' | sed 's/"/\\"/g')",
  "ran_at": "$(date -u +%FT%TZ)"
}
EOF
      echo "[mini-ork] BDD blocked — trace-spec stubs unfilled:" >&2
      printf '%b' "$_stub_problems" | sed 's/^/    /' >&2
      return 0
    fi
  fi

  # ─── Live-env spawn for observability specs ──────────────────────────
  # Specs that import `_observability-helpers` need a stable, no-reload BE.
  # Detection: grep for `_observability-helpers` in any of the specs.
  # If found, spawn an isolated BE on a deterministic port via
  # ${AGENTFLOW_DIR}/observability/agentflow-live-env.sh (if present).
  # Disable globally with MO_BDD_LIVE_ENV=0.
  local live_env_job_id="" live_env_be_url="" live_env_fe_url=""
  local live_env_script="$AGENTFLOW_DIR/observability/agentflow-live-env.sh"
  if [ "${MO_BDD_LIVE_ENV:-1}" -ne 0 ] && [ -x "$live_env_script" ]; then
    local _needs_live_env=0
    for _s in $specs; do
      if grep -q '_observability-helpers' "$_s" 2>/dev/null; then
        _needs_live_env=1
        break
      fi
    done

    if [ "$_needs_live_env" = "1" ]; then
      live_env_job_id="$(echo "${epic}-iter${iter}" | tr '[:upper:]' '[:lower:]')"
      echo "[mini-ork] BDD live-env spawn job=$live_env_job_id" >&2
      bash "$live_env_script" start --job-id "$live_env_job_id" --no-fe >>"$log_path" 2>&1 || true
      if ! bash "$live_env_script" wait --job-id "$live_env_job_id" --timeout 60 >>"$log_path" 2>&1; then
        echo "[mini-ork] BDD live-env wait FAILED — see $log_path. Stopping live-env." >&2
        bash "$live_env_script" stop --job-id "$live_env_job_id" >>"$log_path" 2>&1 || true
        live_env_job_id=""
      else
        local _env_json="$AGENTFLOW_DIR/observability/envs/$live_env_job_id/env.json"
        if [ -f "$_env_json" ]; then
          live_env_be_url="$(jq -r '.be_url // ""' "$_env_json" 2>/dev/null)"
          live_env_fe_url="$(jq -r '.fe_url // ""' "$_env_json" 2>/dev/null)"
          [ "$live_env_fe_url" = "null" ] && live_env_fe_url=""
        fi
        # shellcheck disable=SC2064
        trap "bash '$live_env_script' stop --job-id '$live_env_job_id' >>'$log_path' 2>&1 || true" RETURN
        echo "[mini-ork] BDD live-env ready be=$live_env_be_url fe=${live_env_fe_url:-<none>}" >&2
      fi
    fi
  fi

  # Run Playwright from the worktree.
  local started_at_ms
  started_at_ms="$(date +%s)000"

  local exit_code=0
  local json_path="$iter_dir/bdd-results.json"

  (
    cd "$worktree" || exit 1
    # Build first if dist/ is missing (idempotent — Vite incremental).
    if [ ! -d "dist" ]; then
      if ! jq -e '.scripts."build:fast"' package.json >/dev/null 2>&1; then
        {
          echo "[mini-ork] BDD pre-build FAIL: 'build:fast' script missing in $(pwd)/package.json"
          echo "[mini-ork] worktree base is stale — main has it but this worktree was cut before it landed"
          echo "[mini-ork] fix: rebase the worker's branch onto main, then re-dispatch"
        } | tee -a "$log_path" >&2
        exit 3
      fi
      echo "[mini-ork] BDD pre-build (dist/ missing)" >&2
      npm run build:fast >>"$log_path" 2>&1 || exit 2
    fi
    PLAYWRIGHT_JSON_OUTPUT_NAME="$json_path" \
    E2E_BE_URL="${live_env_be_url:-${E2E_BE_URL:-}}" \
    PLAYWRIGHT_BASE_URL="${live_env_fe_url:-${PLAYWRIGHT_BASE_URL:-http://localhost:5173}}" \
    E2E_VERDICT_DIR="$iter_dir" \
    npx playwright test $specs --reporter=json >"$log_path" 2>&1
  )
  exit_code=$?

  local ended_at_ms
  ended_at_ms="$(date +%s)000"
  local duration_ms=$((ended_at_ms - started_at_ms))

  # Parse Playwright JSON if present.
  local total=0 passed=0 failed=0 skipped=0
  if [ -f "$json_path" ]; then
    total=$(jq -r '.stats.expected + .stats.unexpected + .stats.flaky + .stats.skipped' "$json_path" 2>/dev/null || echo 0)
    passed=$(jq -r '.stats.expected' "$json_path" 2>/dev/null || echo 0)
    failed=$(jq -r '.stats.unexpected + .stats.flaky' "$json_path" 2>/dev/null || echo 0)
    skipped=$(jq -r '.stats.skipped' "$json_path" 2>/dev/null || echo 0)
  fi

  local verdict
  if [ "$exit_code" -eq 0 ] && [ "$failed" -eq 0 ]; then
    verdict="PASS"
  else
    verdict="FAIL"
  fi

  # Extract concise failure summaries for feedback (max 5).
  local failures_json="[]"
  if [ -f "$json_path" ] && [ "$failed" -gt 0 ]; then
    failures_json=$(jq -c '
      [.suites[]?.suites[]?.specs[]?
        | select(.tests[]?.results[]?.status != "passed")
        | {
            spec: .file,
            title: .title,
            error: (.tests[0].results[0].error.message // "no error message")
          }
      ][0:5]
    ' "$json_path" 2>/dev/null || echo "[]")
    : "${failures_json:=[]}"
  fi

  jq -n \
    --arg verdict "$verdict" \
    --arg epic "$epic" \
    --argjson iter "$iter" \
    --argjson exit_code "$exit_code" \
    --argjson total "$total" \
    --argjson passed "$passed" \
    --argjson failed "$failed" \
    --argjson skipped "$skipped" \
    --argjson duration_ms "$duration_ms" \
    --argjson failures "$failures_json" \
    --arg ran_at "$(date -u +%FT%TZ)" \
    --arg live_env_job_id "${live_env_job_id:-}" \
    --arg live_env_be_url "${live_env_be_url:-}" \
    --arg live_env_fe_url "${live_env_fe_url:-}" \
    '{
      verdict: $verdict,
      skipped: false,
      epic: $epic,
      iter: $iter,
      exit_code: $exit_code,
      scenarios_run: $total,
      scenarios_passed: $passed,
      scenarios_failed: $failed,
      scenarios_skipped: $skipped,
      duration_ms: $duration_ms,
      failures: $failures,
      ran_at: $ran_at,
      live_env: (if $live_env_job_id == "" then null else {
        job_id: $live_env_job_id,
        be_url: $live_env_be_url,
        fe_url: (if $live_env_fe_url == "" then null else $live_env_fe_url end)
      } end)
    }' > "$verdict_path"

  echo "[mini-ork] BDD verdict epic=$epic iter=$iter verdict=$verdict total=$total passed=$passed failed=$failed" >&2

  # Phase 14.3: emit bdd_verdict event.
  if type mo_emit >/dev/null 2>&1; then
    export MO_EVENT_EPIC="$epic"
    export MO_EVENT_ITER="$iter"
    local _mo_status='ok'
    [ "$verdict" = "FAIL" ] && _mo_status='fail'
    [ "$verdict" = "SKIP" ] && _mo_status='skip'
    mo_emit "bdd_verdict" "orch" \
      "$(printf '{"verdict":"%s","total":%s,"passed":%s,"failed":%s,"duration_ms":%s}' \
          "$verdict" "$total" "$passed" "$failed" "$duration_ms")" \
      "$_mo_status" "$verdict_path" >/dev/null
  fi

  # Write runs.test_status + runs.trace_status
  if [ -f "$_db" ]; then
    local _test_status
    case "$verdict" in
      PASS) _test_status='pass' ;;
      FAIL) _test_status='fail' ;;
      *)    _test_status='error' ;;
    esac
    local _trace_status='skip'
    local _trace_verdict_file="$iter_dir/trace-verdict.json"
    if [ -f "$_trace_verdict_file" ]; then
      if jq -e '.pass == true' "$_trace_verdict_file" >/dev/null 2>&1; then
        _trace_status='pass'
      else
        _trace_status='fail'
      fi
    fi

    local _rel_run_dir="${run_dir#${REPO_ROOT:-$(pwd)}/}"
    local _run_id
    _run_id=$(sqlite3 "$_db" \
      "SELECT id FROM runs WHERE run_dir='${_rel_run_dir//\'/\'\'}' ORDER BY id DESC LIMIT 1;" \
      2>/dev/null)
    if [ -n "$_run_id" ]; then
      sqlite3 "$_db" "
        UPDATE runs SET
          test_status  = '$_test_status',
          trace_status = '$_trace_status'
        WHERE id = $_run_id;
      " 2>/dev/null
      echo "[mini-ork] runs.id=$_run_id test_status=$_test_status trace_status=$_trace_status" >&2
    else
      echo "[mini-ork] WARN: could not resolve runs.id for run_dir=$_rel_run_dir; test/trace status not written" >&2
    fi
  fi

  # ─── VLM judge on FAIL (Perceptual Self-Reflection, arXiv 2602.12311) ─
  # Off by default. Opt in with MO_VLM_JUDGE_ENABLED=1.
  if [ "$verdict" = "FAIL" ] && [ "${MO_VLM_JUDGE_ENABLED:-0}" -eq 1 ]; then
    local vlm_out="$iter_dir/vlm-judge.json"
    local vlm_classify_ts="$MINI_ORK_ROOT/lib/vlm_classify.ts"
    [ ! -f "$vlm_classify_ts" ] && vlm_classify_ts="$AGENTFLOW_DIR/mini-orch/lib/vlm_classify.ts"
    local screenshots
    mapfile -t screenshots < <(find "$REPO_ROOT/test-results" -name 'test-failed-*.png' -type f 2>/dev/null | sort -r | head -3)
    if [ "${#screenshots[@]}" -eq 0 ]; then
      echo "[mini-ork] VLM judge: no screenshots found (set MO_VLM_SCREENSHOT=1 before BDD run)" >&2
      printf '{"verdict":"no_screenshots","note":"set MO_VLM_SCREENSHOT=1 before BDD run"}\n' > "$vlm_out"
    else
      local first_err first_title
      first_err=$(jq -r '.failures[0].error // ""' "$verdict_path" 2>/dev/null)
      first_title=$(jq -r '.failures[0].title // ""' "$verdict_path" 2>/dev/null)
      local vlm_results="[]"
      for shot in "${screenshots[@]}"; do
        local one_result
        one_result=$(cd "$REPO_ROOT" && timeout 60 npx tsx "$vlm_classify_ts" \
          "$shot" "$first_err" "$first_title" 2>>"$iter_dir/vlm-judge.log" || echo '{"verdict":"timeout"}')
        if echo "$one_result" | jq -e . >/dev/null 2>&1; then
          vlm_results=$(jq --argjson r "$one_result" --arg shot "$shot" \
            '. + [$r + {screenshot: $shot}]' <<<"$vlm_results")
        fi
      done
      printf '%s\n' "$vlm_results" | jq '.' > "$vlm_out" 2>/dev/null || cp /dev/stdin "$vlm_out"
      local _vlm_summary
      _vlm_summary=$(jq -r '[.[] | .verdict] | join(",")' "$vlm_out" 2>/dev/null)
      echo "[mini-ork] VLM judge: classifications=$_vlm_summary (n=${#screenshots[@]})" >&2
    fi
  fi

  # T4: emit cache row. Cache PASS only.
  if [ "$verdict" = "PASS" ] && [ "${MO_SKIP_CACHE:-0}" -ne 1 ]; then
    local specs_concat=""
    for s in $specs; do specs_concat="$specs_concat$(cat "$s")"$'\x1e'; done
    local worker_head
    worker_head=$(git -C "$worktree" rev-parse HEAD 2>/dev/null || echo "")
    local cache_hash
    cache_hash=$(printf '%s\x1e%s' "$specs_concat" "$worker_head" | mo_cache_input_hash)
    mo_cache_emit bdd-runner "$epic" "$iter" "$cache_hash" "success" \
      "$verdict_path" "$log_path" 0 0 "$duration_ms"
  fi

  return "$exit_code"
}

# ─── Verdict accessors ──────────────────────────────────────────────────
mo_bdd_verdict() {
  local epic="$1" iter="$2"
  local v="$(mo_run_dir "$epic")/iter-$iter/bdd-verdict.json"
  if [ ! -f "$v" ]; then echo "MISSING"; return; fi
  jq -r '.verdict // "UNKNOWN"' "$v" 2>/dev/null || echo "UNKNOWN"
}

# Append BDD failures to the existing reviewer feedback file.
# No-op if BDD verdict is PASS or skipped.
mo_append_bdd_feedback() {
  local epic="$1" iter="$2" feedback_path="$3"
  local v="$(mo_run_dir "$epic")/iter-$iter/bdd-verdict.json"
  if [ ! -f "$v" ]; then return; fi

  local verdict
  verdict=$(jq -r '.verdict' "$v")
  if [ "$verdict" = "PASS" ]; then return; fi

  {
    echo
    echo "## BDD failures (Phase A.1 runner)"
    echo
    jq -r '
      "Verdict: " + .verdict + "\n" +
      "Scenarios: " + (.scenarios_passed | tostring) + " passed / " +
      (.scenarios_failed | tostring) + " failed / " +
      (.scenarios_skipped | tostring) + " skipped (of " + (.scenarios_run | tostring) + ")\n\n" +
      (if (.failures | length) > 0 then
        "### Failing scenarios (top 5)\n\n" +
        ([.failures[] | "- **" + .title + "** (" + .spec + ")\n  ```\n  " + (.error | gsub("\n"; "\n  ")) + "\n  ```"] | join("\n\n")) + "\n"
      else
        "(no parsed failures — see bdd-runner.log)\n"
      end)
    ' "$v"

    # Append VLM judge classification if it ran.
    local _vlm_path="$(mo_run_dir "$epic")/iter-$iter/vlm-judge.json"
    if [ -f "$_vlm_path" ] && jq -e 'type == "array" and length > 0' "$_vlm_path" >/dev/null 2>&1; then
      echo
      echo "### VLM judge (vision-LM screenshot classification)"
      echo
      echo "_The bdd-runner shipped failure screenshots to a vision-LM. Use these classifications to pick a fix path:_"
      echo
      jq -r '.[] | "- **" + (.verdict // "?") + "** (confidence: " + (.confidence // "?") + ") — " + (.observation // "") + "\n  Suggested fix path: " + (.suggested_fix_path // "")' "$_vlm_path"
      echo
    fi
  } >> "$feedback_path"
}
