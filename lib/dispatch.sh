#!/usr/bin/env bash
# mini-ork dispatch lib — core dispatch functions.
# Source from bin/mini-ork-run; not meant to run alone.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Phase A.1 — load BDD runner alongside the existing reviewer wrapper.
# Phase A.2 — load spec author + spec reviewer.
# Phase A.3 — load mutation adversary + visible/hidden split utilities.
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/bdd-runner.sh"
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/spec-author.sh"
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/spec-reviewer.sh"
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/mutation-adversary.sh"
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/spec-split.sh"
# Phase A.4 — Behavioral Contract enforcement + drift telemetry.
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/contract.sh"
# Phase A.5 — reflection refiner + agentic rubric pre-screen.
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/reflection-refiner.sh"
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/rubric-prescreen.sh"
# Phase A T0 — stage-cache helpers + bootstrap schema once.
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/cache.sh"
mo_cache_init_schema
mo_cache_gc
# L1 (Tier 1): epic decomposer.
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/decomposer.sh"
# L6 (Tier 1): mid-iter self-correction.
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/self-correction.sh"
# Unified-tracking: keeps runs table in sync at dispatch start, per iter, and at close.
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/runs-tracker.sh"

# State DB path — callers must set MINI_ORK_DB or AGENTFLOW_DIR
_mo_state_db() {
  echo "${MINI_ORK_DB:-${AGENTFLOW_DIR:-${MINI_ORK_HOME:-.mini-ork}}/state.db}"
}

# ─── Spec synthesis sub-loop (Phase A.2) ────────────────────────────────
# Author → Reviewer, max 2 sub-iters. If APPROVE_SPEC → returns 0 with
# spec path on stdout. If REQUEST_CHANGES_SPEC after max iters → returns 2.
# If ESCALATE → returns 3. If MISSING → 4. If SKIPPED (BE-only) → 0 + "SKIPPED".
#
# Args: epic worktree
mo_synth_spec() {
  local epic="$1" worktree="$2"
  local sub_iter=1
  local feedback=""
  local max_sub_iter=2

  while [ "$sub_iter" -le "$max_sub_iter" ]; do
    if ! mo_run_spec_author "$epic" "$worktree" "$sub_iter" "$feedback"; then
      echo "[mini-ork] synth-spec: author infra failure at sub-iter $sub_iter" >&2
      return 5
    fi

    local outcome
    outcome=$(mo_spec_author_outcome "$epic" "$worktree" "$sub_iter")
    case "$outcome" in
      "SKIPPED "*)
        local reason="${outcome#SKIPPED }"
        echo "[mini-ork] synth-spec: epic=$epic SKIPPED ($reason)" >&2
        echo "SKIPPED"
        return 0
        ;;
      "WRITTEN "*)
        local spec_path="${outcome#WRITTEN }"
        if ! mo_run_spec_reviewer "$epic" "$worktree" "$sub_iter" "$spec_path"; then
          echo "[mini-ork] synth-spec: reviewer infra failure at sub-iter $sub_iter" >&2
          return 6
        fi
        local verdict
        verdict=$(mo_spec_verdict "$epic" "$sub_iter")
        echo "[mini-ork] synth-spec: epic=$epic sub-iter=$sub_iter verdict=$verdict" >&2
        case "$verdict" in
          APPROVE_SPEC)
            if [ "${MO_SKIP_MUTATION:-0}" -ne 1 ] && \
               ! mo_should_skip_for_trivial "$epic" "$worktree" "mutation-adversary"; then
              if mo_run_mutation_adversary "$epic" "$worktree" "$sub_iter" "$spec_path"; then
                local mut_result
                mut_result=$(mo_run_mutation_validator "$epic" "$worktree" "$sub_iter" "$spec_path")
                case "$mut_result" in
                  PASS|SKIP|EMPTY|MISSING)
                    echo "[mini-ork] synth-spec: mutation gate $mut_result for $epic" >&2
                    ;;
                  FAIL)
                    echo "[mini-ork] synth-spec: mutation kill_rate < 0.8 — requesting spec strengthening" >&2
                    feedback=$(mo_spec_feedback_file "$epic" "$sub_iter")
                    echo "" >> "$feedback"
                    echo "## Mutation adversary feedback (Phase A.3)" >> "$feedback"
                    jq -r '.results[] | select(.caught == false) | "- " + .id + " (" + .target_scenario + "): " + .reason' \
                      "$(mo_run_dir "$epic")/iter-$sub_iter/mutation-results.json" >> "$feedback" 2>/dev/null || true
                    sub_iter=$((sub_iter + 1))
                    continue
                    ;;
                  ABORTED)
                    echo "[mini-ork] synth-spec: mutation validator aborted — proceeding without gate" >&2
                    ;;
                esac
              fi
            fi
            mo_split_visible_hidden "$epic" "$sub_iter" "$spec_path" || true
            echo "$spec_path"
            return 0
            ;;
          REQUEST_CHANGES_SPEC)
            feedback=$(mo_spec_feedback_file "$epic" "$sub_iter")
            sub_iter=$((sub_iter + 1))
            continue
            ;;
          ESCALATE)
            echo "[mini-ork] synth-spec: epic=$epic ESCALATE at sub-iter $sub_iter" >&2
            return 3
            ;;
          *)
            echo "[mini-ork] synth-spec: unknown verdict '$verdict' — defaulting to escalate" >&2
            return 3
            ;;
        esac
        ;;
      "MISSING")
        echo "[mini-ork] synth-spec: author wrote no file and no SKIPPED marker — aborting" >&2
        return 4
        ;;
    esac
  done

  echo "[mini-ork] synth-spec: epic=$epic exhausted $max_sub_iter sub-iters without APPROVE_SPEC" >&2
  return 2
}

# ─── State ──────────────────────────────────────────────────────────────
mo_run_dir() {
  local epic="$1"
  printf '%s/runs/%s/%s\n' "$MINI_ORCH_DIR" "$JOB_ID" "$epic"
}

mo_epic_status() {
  local epic="$1"
  sqlite3 "$(_mo_state_db)" \
    "SELECT status FROM epics WHERE id = '$epic';" 2>/dev/null
}

mo_set_epic_status() {
  local epic="$1" status="$2"
  local try=0 max_try=3 backoff=1 out rc
  while [ $try -lt $max_try ]; do
    out=$(sqlite3 -cmd ".timeout 30000" "$(_mo_state_db)" \
      "UPDATE epics SET status = '$status', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = '$epic';" 2>&1)
    rc=$?
    if [ $rc -eq 0 ]; then return 0; fi
    case "$out" in
      *"database is locked"*)
        sleep $backoff
        backoff=$((backoff * 2))
        try=$((try + 1))
        ;;
      *"trg_epics_no_revert_done"*)
        echo "[mini-ork] mo_set_epic_status: blocked $epic done→$status revert (H1 trigger fired)" >&2
        return 0
        ;;
      *)
        echo "[mini-ork] mo_set_epic_status FAIL ($out)" >&2
        return 1
        ;;
    esac
  done
  echo "[mini-ork] mo_set_epic_status: gave up after $max_try tries (db locked) for $epic→$status" >&2
  return 1
}

# ─── Worker dispatch ────────────────────────────────────────────────────
# Spawns one worker for one (epic, iter). nohup'd. Returns PID.
# Args: epic worktree iter [feedback-file]
mo_spawn_worker() {
  local epic="$1" worktree="$2" iter="$3" feedback="${4:-}"

  local run_dir
  run_dir="$(mo_run_dir "$epic")"
  mkdir -p "$run_dir/iter-$iter"

  # Resolve agent from agents.yaml. AGENTFLOW_DIR is compat alias for MINI_ORK_HOME dir.
  local config_dir="${AGENTFLOW_DIR:-${MINI_ORK_HOME:-.mini-ork}}/config"
  local agents_yaml="${MINI_ORK_AGENTS_FILE:-$config_dir/agents.yaml}"
  local agent
  agent=$(awk -v id="$epic" '
    /^epics:/ { in_epics = 1; next }
    in_epics && $0 ~ "^  " id ":" { in_epic = 1; next }
    in_epic && /^    worker:/ {
      sub(/^    worker:[[:space:]]*/, ""); sub(/[[:space:]]*#.*$/, ""); print; exit
    }
    in_epic && /^  [A-Za-z]/ { in_epic = 0 }
  ' "$agents_yaml" 2>/dev/null)
  : "${agent:=glm}"

  local launcher="$MINI_ORK_ROOT/bin/_worker-launcher.sh"
  [ -x "$launcher" ] || launcher="$MINI_ORCH_DIR/lib/_worker-launcher.sh"
  local log="$run_dir/iter-$iter/worker.log"
  local pidfile="$run_dir/iter-$iter/worker.pid"

  # ─── Resume-on-timeout detection ────────────────────────────────────
  local resume_session=""
  local prev_iter=$((iter - 1))
  local max_resumes="${MO_MAX_RESUME_PER_EPIC:-1}"
  if [ "$prev_iter" -ge 1 ] && [ -f "$run_dir/iter-$prev_iter/worker.exit" ]; then
    local prev_rc; prev_rc=$(cat "$run_dir/iter-$prev_iter/worker.exit" 2>/dev/null || echo "")
    local prev_verdict=""
    [ -f "$run_dir/iter-$prev_iter/verdict.json" ] && \
      prev_verdict=$(jq -r '.verdict // ""' "$run_dir/iter-$prev_iter/verdict.json" 2>/dev/null || echo "")
    local _should_resume=0
    if [ "$prev_rc" = "124" ] || [ "$prev_rc" = "137" ]; then
      _should_resume=1
    elif [ "${MO_RESUME_ON_REQUEST_CHANGES:-0}" = "1" ] && [ "$prev_verdict" = "REQUEST_CHANGES" ]; then
      _should_resume=1
      echo "    resume-trigger: REQUEST_CHANGES iter (MO_RESUME_ON_REQUEST_CHANGES=1)" >&2
    fi
    if [ "$_should_resume" = "1" ]; then
      local prev_session="" prev_hash="" prev_count=0
      [ -f "$run_dir/iter-$prev_iter/worker.session_id" ] && prev_session=$(cat "$run_dir/iter-$prev_iter/worker.session_id" 2>/dev/null)
      [ -f "$run_dir/iter-$prev_iter/worker.kickoff_hash" ] && prev_hash=$(cat "$run_dir/iter-$prev_iter/worker.kickoff_hash" 2>/dev/null)
      [ -f "$run_dir/iter-$prev_iter/worker.resume_count" ] && prev_count=$(cat "$run_dir/iter-$prev_iter/worker.resume_count" 2>/dev/null || echo 0)
      local kickoff_rel; kickoff_rel=$(sqlite3 "$(_mo_state_db)" "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
      local cur_hash=""
      if [ -n "$kickoff_rel" ] && [ -f "$REPO_ROOT/$kickoff_rel" ]; then
        cur_hash=$(shasum -a 256 "$REPO_ROOT/$kickoff_rel" 2>/dev/null | awk '{print $1}')
      fi
      if [ -z "$prev_session" ]; then
        echo "    resume: SKIP (prev iter timed out but no session_id captured)" >&2
      elif [ -z "$prev_hash" ] || [ "$prev_hash" != "$cur_hash" ]; then
        echo "    resume: SKIP (kickoff drifted: prev=$prev_hash cur=$cur_hash)" >&2
      elif [ "$prev_count" -ge "$max_resumes" ]; then
        echo "    resume: SKIP (resume_count=$prev_count >= MO_MAX_RESUME_PER_EPIC=$max_resumes — model likely stuck)" >&2
      else
        resume_session="$prev_session"
        echo "    resume: ACTIVE — claude --resume $resume_session (attempt $((prev_count + 1))/$max_resumes)" >&2
      fi
    fi
  fi

  echo "[mini-ork] dispatch epic=$epic iter=$iter agent=$agent worktree=$worktree" >&2
  echo "    log:  $log" >&2
  echo "    feedback: ${feedback:-(none — fresh kickoff)}" >&2

  # ─── Plan-first dispatch ────────────────────────────────────────────
  local plan_file=""
  if [ "${MO_PLAN_FIRST_DISPATCH:-1}" = "1" ] && [ -z "$resume_session" ]; then
    # Check framework lib first, then AGENTFLOW_DIR (project-specific override)
    local pre_planner=""
    [ -x "$MINI_ORK_ROOT/lib/pre-planner.sh" ] && pre_planner="$MINI_ORK_ROOT/lib/pre-planner.sh"
    [ -z "$pre_planner" ] && [ -n "${AGENTFLOW_DIR:-}" ] && \
      [ -x "$AGENTFLOW_DIR/lib/pre-planner.sh" ] && pre_planner="$AGENTFLOW_DIR/lib/pre-planner.sh"
    if [ -n "$pre_planner" ]; then
      echo "    plan: invoking pre-planner (~30-60s)..." >&2
      local plan_log="$run_dir/iter-$iter/plan.log"
      plan_file=$(bash "$pre_planner" "$epic" "$worktree" "$run_dir" "$iter" 2>"$plan_log" | tail -1)
      if [ -n "$plan_file" ] && [ -f "$plan_file" ]; then
        local plan_kb; plan_kb=$(($(wc -c < "$plan_file") / 1024))
        echo "    plan: written ${plan_kb}KB → $plan_file" >&2
      else
        echo "    plan: skipped (pre-planner unavailable or returned empty)" >&2
        plan_file=""
      fi
    fi
  fi

  MO_EPIC="$epic" \
  MO_AGENT="$agent" \
  MO_WORKTREE="$worktree" \
  MO_RUN_DIR="$run_dir" \
  MO_ITER="$iter" \
  MO_FEEDBACK="$feedback" \
  MO_AGENTFLOW_DIR="${AGENTFLOW_DIR:-${MINI_ORK_HOME:-.mini-ork}}" \
  MO_JOB="$JOB_ID" \
  MO_RESUME_SESSION_ID="$resume_session" \
  MO_PLAN_FILE="$plan_file" \
  nohup bash "$launcher" > "$log" 2>&1 &

  local pid=$!
  echo "$pid" > "$pidfile"
  echo "$pid"
}

# ─── Reviewer complexity heuristic (L4 — Tier 1) ────────────────────────
mo_epic_complexity() {
  local epic="$1" worktree="$2"
  if [ "${MO_REVIEWER_AUTO_SELECT:-1}" -eq 0 ]; then
    echo "complex"; return
  fi
  local risky_count
  risky_count=$(git -C "$worktree" diff --name-only main..HEAD 2>/dev/null | grep -cE '(migrations/|database/|auth/|secret|password|crypto/|/security/|\.env)')
  risky_count="${risky_count:-0}"
  if [ "$risky_count" -gt 0 ] 2>/dev/null; then echo "complex"; return; fi
  local touched
  touched=$(git -C "$worktree" diff --name-only main..HEAD 2>/dev/null | wc -l | tr -d ' ')
  if [ "$touched" -le 3 ]; then echo "trivial"; else echo "complex"; fi
}

# ─── Auto-skip routing for trivial epics ───────────────────────────────
mo_should_skip_for_trivial() {
  local epic="$1" worktree="$2" stage="$3"
  if [ "${MO_AUTO_SKIP_TRIVIAL:-1}" -eq 0 ]; then return 1; fi
  local complexity
  complexity=$(mo_epic_complexity "$epic" "$worktree")
  if [ "$complexity" = "trivial" ]; then
    echo "[mini-ork] auto-skip-trivial: epic=$epic stage=$stage skipped (complexity=trivial)" >&2
    return 0
  fi
  return 1
}

# ─── TypeScript compile gate (Phase A.6) ────────────────────────────────
# Advisory only — reviewer reads the resulting typecheck.json.
# Disable: MO_SKIP_TYPECHECK=1
mo_run_typecheck_gate() {
  local epic="$1" worktree="$2" iter="$3"
  local run_dir
  run_dir="$(mo_run_dir "$epic")"
  local iter_dir="$run_dir/iter-$iter"
  mkdir -p "$iter_dir"
  local out="$iter_dir/typecheck.json"
  local log="$iter_dir/typecheck.log"

  if [ ! -f "$worktree/package.json" ] || ! grep -q '"type-check"' "$worktree/package.json" 2>/dev/null; then
    printf '{"passed": true, "skipped": "no type-check script", "errors": []}\n' > "$out"
    return 0
  fi

  # Skip for doc-only diffs
  local _baseline_sha _changed_files _non_doc_changes
  _baseline_sha=$(cat "$run_dir/baseline.sha" 2>/dev/null \
    || git -C "$worktree" merge-base main HEAD 2>/dev/null \
    || echo "")
  if [ -n "$_baseline_sha" ]; then
    _changed_files=$(git -C "$worktree" diff --name-only "$_baseline_sha"..HEAD 2>/dev/null)
    if [ -n "$_changed_files" ]; then
      _non_doc_changes=$(echo "$_changed_files" \
        | grep -vE '^docs/|^[A-Z][A-Z_-]*\.md$|^README\.md$|^CHANGELOG\.md$' \
        | head -1)
      if [ -z "$_non_doc_changes" ]; then
        printf '{"passed": true, "skipped": "doc-only diff (no .ts files changed)", "errors": []}\n' > "$out"
        echo "[mini-ork] typecheck epic=$epic iter=$iter SKIP (doc-only diff)" >&2
        return 0
      fi
    fi
  fi

  echo "[mini-ork] typecheck gate epic=$epic iter=$iter" >&2
  local rc=0

  # Scoped typecheck: only walk touched .ts/.tsx files + transitive imports
  if [ "${MO_TYPECHECK_SCOPED:-1}" -eq 1 ] && [ -n "${_changed_files:-}" ] \
     && [ -x "$REPO_ROOT/.husky/_typecheck-touched.sh" ]; then
    local _ts_changed
    _ts_changed=$(echo "$_changed_files" | grep -E '\.(ts|tsx)$' | head -50)
    if [ -n "$_ts_changed" ]; then
      ( cd "$worktree" && bash "$REPO_ROOT/.husky/_typecheck-touched.sh" $_ts_changed 2>&1 ) > "$log" || rc=$?
    else
      printf '{"passed": true, "skipped": "no .ts changes in diff", "errors": []}\n' > "$out"
      echo "[mini-ork] typecheck epic=$epic iter=$iter SKIP (no .ts changes)" >&2
      return 0
    fi
  else
    ( cd "$worktree" && npm run -s type-check 2>&1 ) > "$log" || rc=$?
  fi

  if [ "$rc" -eq 0 ]; then
    printf '{"passed": true, "errors": []}\n' > "$out"
    echo "[mini-ork] typecheck epic=$epic iter=$iter PASS" >&2
    return 0
  fi

  local raw_errors_json
  raw_errors_json=$(grep -E "error TS[0-9]+:" "$log" 2>/dev/null | jq -R -s -c 'split("\n") | map(select(length > 0))')
  : "${raw_errors_json:=[]}"
  local n_raw
  n_raw=$(echo "$raw_errors_json" | jq 'length')

  # Touched-files filter: drop errors whose file path is NOT in the worker's diff
  local touched_errors_json="$raw_errors_json"
  local n_dropped=0
  local n_touched_files=0
  local filtered=false
  if [ "${MO_SKIP_TYPECHECK_FILTER:-0}" != "1" ]; then
    local _bs
    _bs=$(cat "$run_dir/baseline.sha" 2>/dev/null \
      || git -C "$worktree" merge-base main HEAD 2>/dev/null \
      || echo "")
    if [ -n "$_bs" ]; then
      local touched_files
      touched_files=$(git -C "$worktree" diff --name-only "$_bs"..HEAD 2>/dev/null | sort -u)
      n_touched_files=$(echo "$touched_files" | grep -c . || echo 0)
      if [ -n "$touched_files" ]; then
        touched_errors_json=$(echo "$raw_errors_json" \
          | jq -c --arg files "$touched_files" '
              ($files | split("\n") | map(select(length > 0))) as $f
              | map(select(
                  . as $e
                  | $f | any(. as $tf | $e | startswith($tf + "("))
                ))')
        : "${touched_errors_json:=[]}"
        n_dropped=$(( n_raw - $(echo "$touched_errors_json" | jq 'length') ))
        filtered=true
      fi
    fi
  fi

  local n_new
  n_new=$(echo "$touched_errors_json" | jq 'length')
  local display_errors_json
  display_errors_json=$(echo "$touched_errors_json" | jq -c '.[0:20]')

  if [ "$n_new" -eq 0 ] && [ "$filtered" = "true" ] && [ "$n_dropped" -gt 0 ]; then
    printf '{"passed": true, "rc": %d, "errors": [], "errors_dropped": %d, "errors_raw_count": %d, "touched_files": %d, "filtered": true}\n' \
      "$rc" "$n_dropped" "$n_raw" "$n_touched_files" > "$out"
    echo "[mini-ork] typecheck epic=$epic iter=$iter PASS (dropped $n_dropped error(s) in untouched files)" >&2
    return 0
  fi

  printf '{"passed": false, "rc": %d, "errors": %s, "errors_dropped": %d, "errors_raw_count": %d, "touched_files": %d, "filtered": %s}\n' \
    "$rc" "$display_errors_json" "$n_dropped" "$n_raw" "$n_touched_files" "$filtered" \
    > "$out"
  if [ "$n_dropped" -gt 0 ]; then
    echo "[mini-ork] typecheck epic=$epic iter=$iter FAIL ($n_new in worker-touched files, $n_dropped suppressed)" >&2
  else
    echo "[mini-ork] typecheck epic=$epic iter=$iter FAIL ($n_new error(s))" >&2
  fi
  return 0
}

# ─── Reviewer dispatch ──────────────────────────────────────────────────
# Runs review.sh in foreground (~1 min/review).
# Output: <run-dir>/iter-<N>/verdict.json
# L4: auto-selects reviewer model based on epic complexity.
mo_run_review() {
  local epic="$1" worktree="$2" iter="$3"
  local run_dir
  run_dir="$(mo_run_dir "$epic")"

  # Resolve review.sh — prefer project's AGENTFLOW_DIR/lib/review.sh, else
  # framework's own lib/review.sh (placeholder for project-generic version).
  local review_sh=""
  [ -n "${AGENTFLOW_DIR:-}" ] && [ -x "$AGENTFLOW_DIR/lib/review.sh" ] && \
    review_sh="$AGENTFLOW_DIR/lib/review.sh"
  [ -z "$review_sh" ] && [ -x "$MINI_ORK_ROOT/lib/review.sh" ] && \
    review_sh="$MINI_ORK_ROOT/lib/review.sh"
  if [ -z "$review_sh" ]; then
    echo "[mini-ork] review: review.sh not found — skipping (set AGENTFLOW_DIR or place lib/review.sh)" >&2
    return 1
  fi

  # Lane-based reviewer selection
  local complexity lane model budget env_script
  complexity=$(mo_epic_complexity "$epic" "$worktree")
  if [ "$complexity" = "trivial" ]; then
    lane="${MO_REVIEWER_LANE_TRIVIAL:-glm}"
    budget="${MO_REVIEWER_BUDGET_TRIVIAL:-2.00}"
  else
    lane="${MO_REVIEWER_LANE:-glm}"
    budget="${MO_REVIEWER_BUDGET_USD:-5.00}"
  fi

  # Provider env scripts from MINI_ORK_ROOT/lib/providers or override via AGENT_SCRIPTS_DIR
  local _scripts_dir="${AGENT_SCRIPTS_DIR:-$MINI_ORK_ROOT/lib/providers}"
  # Retire deepseek alias → glm
  local _ds_fb="${MO_DEEPSEEK_FALLBACK_LANE:-glm}"
  [ "$lane" = "deepseek" ] && lane="$_ds_fb"
  case "$lane" in
    opus)     model="claude-opus-4-7";    env_script="" ;;
    sonnet)   model="claude-sonnet-4-6";  env_script="" ;;
    glm)      model="GLM-5.1";            env_script="$_scripts_dir/cl_glm.sh" ;;
    kimi)     model="kimi-k2.6";          env_script="$_scripts_dir/cl_kimi.sh" ;;
    minimax)  model="MiniMax-M2.7";       env_script="$_scripts_dir/cl_minimax.sh" ;;
    *)
      echo "[mini-ork] review: unknown lane=$lane — falling back to opus" >&2
      lane=opus; model="claude-opus-4-7"; env_script=""
      ;;
  esac

  if [ -n "$env_script" ] && [ ! -f "$env_script" ]; then
    echo "[mini-ork] review: env script missing for lane=$lane → $env_script — falling back to opus" >&2
    lane=opus; model="claude-opus-4-7"; env_script=""
  fi

  echo "[mini-ork] review epic=$epic iter=$iter (complexity=$complexity lane=$lane model=$model budget=\$$budget)" >&2

  local _kickoff_rel _kickoff_abs=""
  _kickoff_rel=$(sqlite3 "$(_mo_state_db)" \
    "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
  [ -n "$_kickoff_rel" ] && [ -f "$REPO_ROOT/$_kickoff_rel" ] && _kickoff_abs="$REPO_ROOT/$_kickoff_rel"

  (
    if [ -n "$env_script" ]; then
      # shellcheck disable=SC1090
      source "$env_script"
    fi
    REVIEWER_MODEL="$model" \
    REVIEWER_BUDGET_USD="$budget" \
    MO_KICKOFF_PATH="$_kickoff_abs" \
      bash "$review_sh" \
        "$epic" "$worktree" "$run_dir" "$iter" \
        2>&1 | tee "$run_dir/iter-$iter/review.log"
  )
}

# ─── Cascading reviewer (CascadeDebate, arXiv 2604.12262) ───────────────
mo_run_review_with_cascade() {
  local epic="$1" worktree="$2" iter="$3"
  local run_dir
  run_dir="$(mo_run_dir "$epic")"
  local verdict_path="$run_dir/iter-$iter/verdict.json"

  # Result deduplication
  if [ "${MO_REVIEWER_DEDUP_DISABLED:-0}" != "1" ] && [ "$iter" -gt 1 ]; then
    local prev_iter=$((iter - 1))
    local prev_verdict="$run_dir/iter-$prev_iter/verdict.json"
    local curr_diff_hash=""
    if [ -f "$prev_verdict" ]; then
      curr_diff_hash=$(git -C "$worktree" diff main..HEAD 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
      local prev_hash_file="$run_dir/iter-$prev_iter/diff.sha256"
      if [ -f "$prev_hash_file" ]; then
        local prev_diff_hash
        prev_diff_hash=$(cat "$prev_hash_file")
        if [ -n "$curr_diff_hash" ] && [ "$curr_diff_hash" = "$prev_diff_hash" ]; then
          echo "[mini-ork] review-dedup: epic=$epic iter=$iter worker output identical to iter-$prev_iter — copying verdict" >&2
          mkdir -p "$run_dir/iter-$iter"
          cp "$prev_verdict" "$verdict_path"
          jq '. + {dedup_from_iter: '"$prev_iter"'}' "$verdict_path" > "$verdict_path.tmp" && mv "$verdict_path.tmp" "$verdict_path"
          return 0
        fi
      fi
    fi
    [ -n "$curr_diff_hash" ] && echo "$curr_diff_hash" > "$run_dir/iter-$iter/diff.sha256"
  fi

  mo_run_review "$epic" "$worktree" "$iter"
  local rc=$?
  if [ "$rc" -ne 0 ]; then return "$rc"; fi

  if [ "${MO_REVIEWER_CASCADE:-1}" -eq 0 ]; then return 0; fi
  [ -f "$verdict_path" ] || return 0

  local first_lane="${MO_REVIEWER_LANE:-glm}"
  if [ "$first_lane" = "opus" ]; then return 0; fi

  local v_status blocker_count
  v_status=$(jq -r '.verdict // "UNKNOWN"' "$verdict_path" 2>/dev/null)
  blocker_count=$(jq -r '[.issues[]? | select(.severity == "blocker")] | length' "$verdict_path" 2>/dev/null)
  blocker_count="${blocker_count:-0}"

  local should_cascade=0
  case "$v_status" in
    REQUEST_CHANGES) [ "$blocker_count" -gt 0 ] && should_cascade=1 ;;
    ESCALATE)        should_cascade=1 ;;
  esac

  if [ "$should_cascade" -eq 0 ]; then
    echo "[mini-ork] review-cascade: epic=$epic iter=$iter verdict=$v_status blockers=$blocker_count → no second opinion needed" >&2
    return 0
  fi

  echo "[mini-ork] review-cascade: epic=$epic iter=$iter cheap-tier=$first_lane verdict=$v_status → escalating to opus" >&2
  cp "$verdict_path" "$run_dir/iter-$iter/verdict-cascade-tier1.json"
  cp "$run_dir/iter-$iter/review.log" "$run_dir/iter-$iter/review-tier1.log" 2>/dev/null || true

  if ! MO_REVIEWER_LANE=opus MO_REVIEWER_AUTO_SELECT=0 mo_run_review "$epic" "$worktree" "$iter"; then
    echo "[mini-ork] review-cascade: opus second-opinion failed; restoring tier-1 verdict" >&2
    cp "$run_dir/iter-$iter/verdict-cascade-tier1.json" "$verdict_path"
    return 0
  fi

  if jq -e . "$verdict_path" >/dev/null 2>&1; then
    jq --arg t1l "$first_lane" --arg t1v "$v_status" --argjson t1b "$blocker_count" \
      '. + {cascade: {tier1_lane: $t1l, tier1_verdict: $t1v, tier1_blockers: $t1b}}' \
      "$verdict_path" > "$verdict_path.tmp" && mv "$verdict_path.tmp" "$verdict_path"
  fi
  return 0
}

# ─── Verdict parsing ────────────────────────────────────────────────────
mo_verdict() {
  local epic="$1" iter="$2"
  local v="$(mo_run_dir "$epic")/iter-$iter/verdict.json"
  if [ ! -f "$v" ]; then echo "UNKNOWN"; return; fi
  jq -r '.verdict // "UNKNOWN"' "$v" 2>/dev/null || echo "UNKNOWN"
}

mo_extract_review_verdict() {
  local raw_path="$1"
  [ -f "$raw_path" ] || return 1
  local raw_verdict verdict_json=""
  raw_verdict=$(cat "$raw_path")
  if echo "$raw_verdict" | jq -e '.structured_output.verdict' >/dev/null 2>&1; then
    verdict_json=$(echo "$raw_verdict" | jq -c '.structured_output')
  elif echo "$raw_verdict" | jq -e '.result' >/dev/null 2>&1; then
    local result_str
    result_str=$(echo "$raw_verdict" | jq -r '.result')
    if echo "$result_str" | jq -e '.verdict' >/dev/null 2>&1; then
      verdict_json="$result_str"
    else
      verdict_json=$(echo "$result_str" | awk '/```json/,/```/' | sed '1d;$d')
    fi
  fi
  [ -z "$verdict_json" ] && verdict_json="$raw_verdict"
  if ! echo "$verdict_json" | jq -e '.verdict and .issues and .rationale' >/dev/null 2>&1; then
    return 2
  fi
  echo "$verdict_json" | jq -c '.'
}

mo_replay_extract_review_verdict() {
  local epic="$1" iter="$2"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local raw="$iter_dir/verdict-raw.txt"
  local out="$iter_dir/verdict.json"
  if [ ! -f "$raw" ]; then
    echo "[mini-ork] mo_replay_extract_review_verdict: no verdict-raw.txt at $raw" >&2
    return 1
  fi
  local extracted=""
  extracted=$(mo_extract_review_verdict "$raw")
  local rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$extracted" ]; then
    echo "[mini-ork] mo_replay_extract_review_verdict: parse failed (rc=$rc)" >&2
    return 2
  fi
  [ -f "$out" ] && cp "$out" "$out.replay-bak"
  printf '%s\n' "$extracted" > "$out"
  echo "[mini-ork] replayed reviewer verdict for $epic iter=$iter → verdict=$(jq -r '.verdict' "$out")" >&2
}

mo_feedback_file() {
  local epic="$1" iter="$2"
  local v="$(mo_run_dir "$epic")/iter-$iter/verdict.json"
  local out="$(mo_run_dir "$epic")/iter-$((iter + 1))/feedback.md"
  mkdir -p "$(dirname "$out")"
  jq -r '
    "# Reviewer feedback (iter " + ($ARGS.named.iter | tostring) + ")\n" +
    "\nVerdict: " + .verdict + "\n" +
    "\n## Rationale\n\n" + (.rationale // "") + "\n" +
    "\n## Issues\n\n" + ((.issues // []) | map("- [" + .severity + " · " + .category + "] " + .file + " — " + .description) | join("\n")) +
    "\n\n## Direct feedback\n\n" + (.feedback_to_worker // "(none)")
  ' --argjson iter "$iter" "$v" > "$out"

  local sr="$(mo_run_dir "$epic")/iter-$iter/scope-revert.json"
  if [ -f "$sr" ]; then
    local _deleted_new
    _deleted_new=$(jq -r '
      [.reverted // [] | .[] | select(test("\\(deleted, was new\\)$"))]
        | map(sub(" \\(deleted, was new\\)$"; ""))
        | unique
    ' "$sr" 2>/dev/null)
    if [ -n "$_deleted_new" ] && [ "$_deleted_new" != "[]" ] && [ "$(echo "$_deleted_new" | jq -r 'length')" -gt 0 ]; then
      {
        echo
        echo "## Files auto-deleted by scope-revert (re-create if needed)"
        echo
        echo "These files were created by your previous iter but removed because they"
        echo "fell outside this epic's scope-globs. If a kickoff DoD requires them,"
        echo "**re-create them** — the orchestrator's scope config has since been updated."
        echo
        echo "$_deleted_new" | jq -r '.[]' | sed 's|^|- `|; s|$|`|'
      } >> "$out"
    fi
  fi

  echo "$out"
}

# ─── PID watch ──────────────────────────────────────────────────────────
mo_alive() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

mo_wait_all() {
  local pids=("$@")
  local alive=1
  local tick=0
  local poll_interval="${MO_WAIT_POLL_INTERVAL:-30}"
  local stale_threshold="${MO_WORKER_STALE_THRESHOLD_SEC:-300}"
  while [ "$alive" -gt 0 ]; do
    alive=0
    local stale_pids=""
    for p in "${pids[@]}"; do
      if ! mo_alive "$p"; then continue; fi
      alive=$((alive + 1))
      local log_path="" log_size=0 log_mtime_ago=0
      while IFS= read -r pid_file; do
        [ -f "$pid_file" ] || continue
        if [ "$(cat "$pid_file" 2>/dev/null)" = "$p" ]; then
          log_path="${pid_file%/worker.pid}/worker.log"
          break
        fi
      done < <(find "$MINI_ORCH_DIR/runs/$JOB_ID" -name 'worker.pid' -mmin -1440 2>/dev/null)
      if [ -f "$log_path" ]; then
        log_size=$(wc -c < "$log_path" 2>/dev/null || echo 0)
        local log_mtime
        log_mtime=$(stat -f %m "$log_path" 2>/dev/null || stat -c %Y "$log_path" 2>/dev/null || echo 0)
        local now; now=$(date +%s)
        log_mtime_ago=$((now - log_mtime))
        if [ "$log_mtime_ago" -gt "$stale_threshold" ]; then
          stale_pids="$stale_pids $p(${log_mtime_ago}s)"
        fi
      fi
    done
    if [ "$alive" -gt 0 ]; then
      tick=$((tick + 1))
      local sample_size=0 sample_ago="-"
      for p in "${pids[@]}"; do
        if mo_alive "$p"; then
          while IFS= read -r pid_file; do
            [ -f "$pid_file" ] || continue
            if [ "$(cat "$pid_file" 2>/dev/null)" = "$p" ]; then
              local lp="${pid_file%/worker.pid}/worker.log"
              if [ -f "$lp" ]; then
                sample_size=$(($(wc -c < "$lp" 2>/dev/null || echo 0) / 1024))
                local lm; lm=$(stat -f %m "$lp" 2>/dev/null || stat -c %Y "$lp" 2>/dev/null || echo 0)
                local now; now=$(date +%s)
                sample_ago="$((now - lm))s"
              fi
              break
            fi
          done < <(find "$MINI_ORCH_DIR/runs/$JOB_ID" -name 'worker.pid' -mmin -1440 2>/dev/null)
          break
        fi
      done
      if [ -n "$stale_pids" ]; then
        echo "[mini-ork] waiting on $alive worker(s) [tick=$tick log=${sample_size}KB last=${sample_ago}] STALE:${stale_pids}" >&2
      else
        echo "[mini-ork] waiting on $alive worker(s) [tick=$tick log=${sample_size}KB last=${sample_ago}]" >&2
      fi
      sleep "$poll_interval"
    fi
  done
}
