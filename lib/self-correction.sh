#!/usr/bin/env bash
# mini-ork mid-iter self-correction (L6 — Tier 1 of autonomous-delivery roadmap).
#
# When reviewer says REQUEST_CHANGES with ≤N issues + no scope violations,
# instead of dispatching a full worker (cost: ~$0.50, ~5min) we run a
# narrow self-correction agent that produces the minimal patch to resolve
# just those issues. Cost: ~$0.10, ~2min.
#
# Skipped for: scope-violations, too many issues (>3 default),
# high-severity issues (blockers).
#
# Source from dispatch.sh.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Improvement B: Synthesize a BDD-derived verdict.json for L6 consumption.
# When reviewer APPROVE + BDD FAIL, rewrite verdict.json as REQUEST_CHANGES
# with BDD failures as a single issue (severity=warning, category=bdd_failure),
# feeding the rich reflection output as feedback_to_worker.
#
# Args: epic iter
# Returns: 0 if synthesis succeeded, non-zero if it skipped.
mo_synthesize_bdd_verdict_for_l6() {
  local epic="$1" iter="$2"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local bdd_path="$iter_dir/bdd-verdict.json"
  local verdict_path="$iter_dir/verdict.json"
  local reflection_path="$iter_dir/reflection.md"

  [ -f "$bdd_path" ] || return 1
  [ -f "$verdict_path" ] || return 1

  # Only synthesize when BDD actually failed AND we have ≤10 scenarios
  # failing. More than that suggests the implementation is fundamentally
  # off-track and needs full worker re-run, not a targeted patch.
  local bv scenarios_failed
  bv=$(jq -r '.verdict' "$bdd_path" 2>/dev/null)
  scenarios_failed=$(jq -r '.scenarios_failed // 0' "$bdd_path" 2>/dev/null)
  [ "$bv" = "FAIL" ] || return 2
  [ "$scenarios_failed" -le 10 ] 2>/dev/null || return 3

  # Cap at 3 unique error patterns (the L6 small-batch rule).
  local unique_errors
  unique_errors=$(jq -r '
    [.failures[]? | (.error // "" | .[0:80])] | unique | length
  ' "$bdd_path" 2>/dev/null)
  unique_errors="${unique_errors:-0}"
  if [ "$unique_errors" -gt 3 ] 2>/dev/null; then
    echo "[mini-ork] BDD synth skip: $unique_errors unique error patterns (>3 cap)" >&2
    return 4
  fi

  local issues_json
  issues_json=$(jq -c '
    [.failures[]? | {
      severity: "warning",
      category: "bdd_failure",
      file: (.spec // "e2e spec"),
      description: ("BDD scenario \"" + (.title // "unknown") + "\" failed: " +
                   ((.error // "") | gsub("\\u001b\\[[0-9;]*m"; "") | .[0:300]))
    }] | unique_by(.description) | .[0:3]
  ' "$bdd_path" 2>/dev/null)
  [ -n "$issues_json" ] || issues_json="[]"

  local reflection_body=""
  if [ -f "$reflection_path" ]; then
    reflection_body=$(cat "$reflection_path")
  fi

  # Preserve original APPROVE verdict for traceability.
  cp "$verdict_path" "$iter_dir/verdict-original-approve.json"

  jq --argjson issues "$issues_json" \
     --arg refl "$reflection_body" \
     '. + {
       verdict: "REQUEST_CHANGES",
       issues: $issues,
       feedback_to_worker: ("BDD scenarios failed despite reviewer APPROVE.\n\n" +
                             "Reflection-refiner root-cause analysis (apply targeted fix):\n\n" +
                             $refl),
       _bdd_synthesized: true,
       _original_verdict: .verdict
     }' "$verdict_path" > "$verdict_path.tmp" && mv "$verdict_path.tmp" "$verdict_path"

  echo "[mini-ork] BDD-derived verdict synthesized for L6: $epic iter=$iter ($(echo "$issues_json" | jq 'length') issue(s) from $scenarios_failed failed scenarios)" >&2
  return 0
}

# Whether self-correction is eligible for this iter's verdict.
# Returns: 0 (eligible) or non-zero (skip).
# Args: epic iter
mo_self_correction_eligible() {
  local epic="$1" iter="$2"
  if [ "${MO_SKIP_SELF_CORRECTION:-0}" -eq 1 ]; then return 1; fi

  # Trivial epics skip L6 — the worker already touched ≤3 files;
  # one more reviewer turn is cheaper than spinning up a self-correction flow.
  local _db="${MINI_ORK_DB:-$AGENTFLOW_DIR/state.db}"
  local _wt
  _wt=$(sqlite3 "$_db" \
    "SELECT worktree_path FROM epics WHERE id='$epic';" 2>/dev/null)
  if [ -n "$_wt" ] && declare -F mo_should_skip_for_trivial >/dev/null 2>&1; then
    if mo_should_skip_for_trivial "$epic" "$_wt" "self-correction"; then return 1; fi
  fi

  local verdict_path
  verdict_path="$(mo_run_dir "$epic")/iter-$iter/verdict.json"
  [ -f "$verdict_path" ] || return 1

  local v
  v=$(jq -r '.verdict' "$verdict_path" 2>/dev/null)
  [ "$v" = "REQUEST_CHANGES" ] || return 1

  # Skip if any scope-violation category.
  local scope_count
  scope_count=$(jq -r '[.issues[]? | select(.category == "scope")] | length' "$verdict_path" 2>/dev/null)
  scope_count="${scope_count:-0}"
  [ "$scope_count" -eq 0 ] || return 1

  # Skip if blocker-severity issues.
  local blocker_count
  blocker_count=$(jq -r '[.issues[]? | select(.severity == "blocker")] | length' "$verdict_path" 2>/dev/null)
  blocker_count="${blocker_count:-0}"
  [ "$blocker_count" -eq 0 ] || return 1

  local total_count
  total_count=$(jq -r '.issues | length' "$verdict_path" 2>/dev/null)
  total_count="${total_count:-0}"
  local cap="${MO_SELF_CORRECTION_MAX_ISSUES:-3}"
  [ "$total_count" -le "$cap" ] || return 1
  [ "$total_count" -gt 0 ] || return 1

  return 0
}

# Run the self-correction agent. Args: epic worktree iter
# Returns 0 on success (commits added), 1 on parse failure / escalate.
mo_run_self_correction() {
  # Patch-only mode (ReflexiCoder + IRTD): when MO_L6_PATCH_ONLY=1, route
  # to the patch-only variant which asks the LLM to emit a unified diff
  # instead of running Edit/Write tool turns. ~80% token reduction.
  if [ "${MO_L6_PATCH_ONLY:-0}" -eq 1 ]; then
    mo_run_self_correction_patch "$@"
    return $?
  fi

  local epic="$1" worktree="$2" iter="$3"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local verdict_path="$iter_dir/verdict.json"
  local prompt_path="$iter_dir/self-correction-prompt.md"
  local log_path="$iter_dir/self-correction.log"

  local _db="${MINI_ORK_DB:-$AGENTFLOW_DIR/state.db}"
  local kickoff_rel
  kickoff_rel=$(sqlite3 "$_db" \
    "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
  local kickoff_abs="$REPO_ROOT/$kickoff_rel"

  local kickoff_body
  kickoff_body=$(cat "$kickoff_abs" 2>/dev/null || echo "")

  local reviewer_feedback
  reviewer_feedback=$(jq -r '
    "Reviewer verdict: " + .verdict + "\n\n" +
    "Issues to resolve (in order):\n\n" +
    ([.issues[] | "- [\(.severity) · \(.category)] \(.file): \(.description)"] | join("\n")) +
    "\n\nDirect feedback from reviewer:\n\n" +
    (.feedback_to_worker // "(none)")
  ' "$verdict_path" 2>/dev/null)

  local current_diff
  current_diff=$(git -C "$worktree" diff main..HEAD 2>/dev/null | head -300)

  local _prompts_dir="${MINI_ORK_DIR:-$MINI_ORK_ROOT}/prompts"
  local template="$_prompts_dir/self-correction.md"
  if [ ! -f "$template" ]; then
    echo "[self-correction] prompt template missing: $template" >&2
    return 2
  fi

  {
    cat "$template"
    echo
    echo "## KICKOFF_BODY"
    echo
    echo "$kickoff_body"
    echo
    echo "## REVIEWER_FEEDBACK"
    echo
    echo "$reviewer_feedback"
    echo
    echo "## CURRENT_DIFF (first 300 lines)"
    echo
    echo '```diff'
    echo "$current_diff"
    echo '```'
  } > "$prompt_path"

  local lane="${MO_L6_LANE:-glm}"
  echo "[mini-ork] self-correction epic=$epic iter=$iter ($lane; $(jq -r '.issues | length' "$verdict_path") issues)" >&2

  local scripts_dir="${AGENT_SCRIPTS_DIR:-$MINI_ORK_ROOT/lib/providers}"
  local env_script="$scripts_dir/cl_${lane}.sh"
  if [ ! -f "$env_script" ]; then
    echo "[mini-ork] self-correction: env script missing for lane=$lane → $env_script" >&2
    return 5
  fi
  local sha_before
  sha_before=$(git -C "$worktree" rev-parse HEAD)

  local _budget_flag=()
  mo_emit_budget_flag _budget_flag "$lane" "${MO_L6_BUDGET_USD:-1.50}"
  (
    set -uo pipefail
    [ -f "$env_script" ] && source "$env_script"
    export CLAUDE_CODE_EFFORT_LEVEL="${MO_L6_EFFORT:-medium}"
    export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${MO_L6_MAX_OUTPUT_TOKENS:-8000}"
    cd "$worktree" || exit 1
    local _TO="${MO_L6_WALL_CLOCK_SECS:-300}"
    local _TIMEOUT_BIN=""
    command -v gtimeout >/dev/null 2>&1 && _TIMEOUT_BIN=gtimeout
    command -v timeout >/dev/null 2>&1 && [ -z "$_TIMEOUT_BIN" ] && _TIMEOUT_BIN=timeout
    local _cache_flag=()
    mo_emit_cache_flags _cache_flag 2>/dev/null || true
    ${_TIMEOUT_BIN:-} ${_TIMEOUT_BIN:+--kill-after=30s $_TO} claude -p \
      "${_cache_flag[@]}" \
      "${_budget_flag[@]}" \
      --output-format stream-json \
      --verbose \
      --include-partial-messages \
      --dangerously-skip-permissions \
      --permission-mode acceptEdits \
      "$(cat "$prompt_path")" \
      > "$log_path" 2>&1
  ) || true

  if grep -q '<<<ESCALATE>>>' "$log_path" 2>/dev/null; then
    echo "[mini-ork] self-correction ESCALATED — operator review needed" >&2
    return 3
  fi

  local sha_after
  sha_after=$(git -C "$worktree" rev-parse HEAD)
  if [ "$sha_before" = "$sha_after" ]; then
    echo "[mini-ork] self-correction made NO commits (HEAD unchanged) — falling back to full worker re-dispatch next iter" >&2
    return 4
  fi

  local n_new
  n_new=$(git -C "$worktree" log --oneline "${sha_before}..${sha_after}" 2>/dev/null | wc -l | tr -d ' ')
  echo "[mini-ork] self-correction OK: +$n_new commits" >&2
  return 0
}

# ─── L6 patch-only variant (ReflexiCoder + IRTD, arXiv 2603.05863 / 2604.23989)
# Asks the LLM to emit a unified diff in markers instead of running Edit/Write
# turns. Orchestrator extracts the diff and applies it via `git apply`.
# Token cost ~20% of the full-worker variant; wall-clock ~30s vs 2-5min.
#
# Returns: 0 on success (diff applied + commit), 3 on ESCALATE marker, 4 if
# the LLM produced no diff or the diff failed to apply, 5 on env-script missing.
#
# Args: epic worktree iter
mo_run_self_correction_patch() {
  local epic="$1" worktree="$2" iter="$3"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local verdict_path="$iter_dir/verdict.json"
  local prompt_path="$iter_dir/self-correction-patch-prompt.md"
  local log_path="$iter_dir/self-correction-patch.log"
  local diff_path="$iter_dir/self-correction.patch"

  local _db="${MINI_ORK_DB:-$AGENTFLOW_DIR/state.db}"
  local kickoff_rel
  kickoff_rel=$(sqlite3 "$_db" \
    "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
  local kickoff_abs="$REPO_ROOT/$kickoff_rel"
  local kickoff_body
  kickoff_body=$(cat "$kickoff_abs" 2>/dev/null || echo "")

  local reviewer_feedback
  reviewer_feedback=$(jq -r '
    "Reviewer verdict: " + .verdict + "\n\n" +
    "Issues to resolve (in order):\n\n" +
    ([.issues[] | "- [\(.severity) · \(.category)] \(.file): \(.description)"] | join("\n")) +
    "\n\nDirect feedback from reviewer:\n\n" +
    (.feedback_to_worker // "(none)")
  ' "$verdict_path" 2>/dev/null)

  local current_diff
  current_diff=$(git -C "$worktree" diff main..HEAD 2>/dev/null | head -300)

  local _prompts_dir="${MINI_ORK_DIR:-$MINI_ORK_ROOT}/prompts"
  local template="$_prompts_dir/self-correction-patch.md"
  if [ ! -f "$template" ]; then
    echo "[self-correction-patch] prompt template missing: $template" >&2
    return 2
  fi

  {
    cat "$template"
    echo
    echo "## KICKOFF_BODY"
    echo
    echo "$kickoff_body"
    echo
    echo "## REVIEWER_FEEDBACK"
    echo
    echo "$reviewer_feedback"
    echo
    echo "## CURRENT_DIFF (first 300 lines)"
    echo
    echo '```diff'
    echo "$current_diff"
    echo '```'
  } > "$prompt_path"

  local lane="${MO_L6_LANE:-glm}"
  echo "[mini-ork] L6 patch-only epic=$epic iter=$iter ($lane; $(jq -r '.issues | length' "$verdict_path") issues)" >&2

  local scripts_dir="${AGENT_SCRIPTS_DIR:-$MINI_ORK_ROOT/lib/providers}"
  local env_script="$scripts_dir/cl_${lane}.sh"
  if [ ! -f "$env_script" ]; then
    echo "[mini-ork] L6 patch-only: env script missing for lane=$lane → $env_script" >&2
    return 5
  fi

  local sha_before
  sha_before=$(git -C "$worktree" rev-parse HEAD)

  local _budget_flag=()
  mo_emit_budget_flag _budget_flag "$lane" "${MO_L6_BUDGET_USD:-0.50}"
  (
    set -uo pipefail
    [ -f "$env_script" ] && source "$env_script"
    export CLAUDE_CODE_EFFORT_LEVEL="${MO_L6_EFFORT:-medium}"
    export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${MO_L6_MAX_OUTPUT_TOKENS:-8000}"
    cd "$worktree" || exit 1
    local _TO="${MO_L6_PATCH_WALL_CLOCK_SECS:-180}"
    local _TIMEOUT_BIN=""
    command -v gtimeout >/dev/null 2>&1 && _TIMEOUT_BIN=gtimeout
    command -v timeout >/dev/null 2>&1 && [ -z "$_TIMEOUT_BIN" ] && _TIMEOUT_BIN=timeout
    local _cache_flag=()
    mo_emit_cache_flags _cache_flag 2>/dev/null || true
    ${_TIMEOUT_BIN:-} ${_TIMEOUT_BIN:+--kill-after=30s $_TO} claude -p \
      "${_cache_flag[@]}" \
      "${_budget_flag[@]}" \
      --output-format stream-json \
      --verbose \
      --dangerously-skip-permissions \
      --disallowedTools "Edit Write Bash NotebookEdit" \
      "$(cat "$prompt_path")" \
      > "$log_path" 2>&1
  ) || true

  local result_text
  result_text=$(grep '"type":"result"' "$log_path" 2>/dev/null | tail -1 | jq -r '.result // empty' 2>/dev/null)

  if echo "$result_text" | grep -q '<<<ESCALATE>>>'; then
    echo "[mini-ork] L6 patch-only ESCALATED (LLM declined patch path)" >&2
    return 3
  fi

  local diff_body
  diff_body=$(echo "$result_text" | awk '
    /<<<DIFF>>>/ { capture = 1; next }
    /<<<END_DIFF>>>/ { capture = 0 }
    capture { print }
  ')
  if [ -z "$diff_body" ]; then
    echo "[mini-ork] L6 patch-only: no diff in LLM output — see $log_path" >&2
    return 4
  fi
  printf '%s\n' "$diff_body" > "$diff_path"

  if ! (cd "$worktree" && git apply --check "$diff_path" 2>>"$log_path"); then
    echo "[mini-ork] L6 patch-only: git apply --check rejected diff — see $diff_path + $log_path" >&2
    return 4
  fi

  (cd "$worktree" && git apply "$diff_path") || {
    echo "[mini-ork] L6 patch-only: git apply failed (post-check) — corrupt diff?" >&2
    return 4
  }

  (cd "$worktree" \
    && git add -A \
    && git commit -m "fix($epic): L6 patch-only iter-$iter

Generated by mini-ork self-correction patch-only mode.
Issues addressed: $(jq -r '[.issues[] | .description] | join("; ")' "$verdict_path" 2>/dev/null | head -c 200)
" >> "$log_path" 2>&1) || {
    echo "[mini-ork] L6 patch-only: commit failed — diff applied but uncommitted" >&2
    return 4
  }

  local sha_after
  sha_after=$(git -C "$worktree" rev-parse HEAD)
  if [ "$sha_before" = "$sha_after" ]; then
    echo "[mini-ork] L6 patch-only: HEAD unchanged after apply — empty patch?" >&2
    return 4
  fi
  echo "[mini-ork] L6 patch-only OK: +1 commit ($(echo "$diff_body" | wc -l | tr -d ' ') lines of diff)" >&2
  return 0
}
