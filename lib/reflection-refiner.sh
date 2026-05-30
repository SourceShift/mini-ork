#!/usr/bin/env bash
# mini-ork Reflection Refiner — Phase A.5
# Adapted from TENET paper (arXiv 2509.24148 §3.4). On BDD failure, runs
# a deepseek pass that consumes the worker's diff + failure log + kickoff
# and emits structured root-cause hypotheses for the next iter's feedback.
#
# Source from dispatch.sh.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Caller exports: AGENTFLOW_DIR, REPO_ROOT, MINI_ORK_HOME

# Args: epic worktree iter
# Reads: <iter-dir>/bdd-verdict.json
# Writes: <iter-dir>/reflection.md  (Markdown — appended to feedback)
mo_run_reflection_refiner() {
  local epic="$1" worktree="$2" iter="$3"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local bdd_verdict="$iter_dir/bdd-verdict.json"
  local prompt_path="$iter_dir/reflection-prompt.md"
  local log_path="$iter_dir/reflection.log"
  local refl_path="$iter_dir/reflection.md"

  if [ ! -f "$bdd_verdict" ]; then
    echo "[mini-ork] reflection-refiner: no bdd-verdict.json — skipping" >&2
    return 0
  fi

  local v
  v=$(jq -r '.verdict' "$bdd_verdict")
  if [ "$v" != "FAIL" ]; then
    echo "[mini-ork] reflection-refiner: bdd verdict=$v — nothing to refine" >&2
    return 0
  fi

  local _db="${MINI_ORK_DB:-$AGENTFLOW_DIR/state.db}"
  local kickoff_rel
  kickoff_rel=$(sqlite3 "$_db" \
    "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
  local kickoff_abs="$REPO_ROOT/$kickoff_rel"

  local diff_files
  diff_files=$(git -C "$worktree" diff --name-only main..HEAD 2>/dev/null | head -20)

  local failure_summary
  failure_summary=$(jq -r '
    "Total: " + (.scenarios_run | tostring) + " scenarios, " +
    (.scenarios_failed | tostring) + " failed.\n\n" +
    ([.failures[] | "- **" + .title + "** (" + .spec + "): " + (.error | gsub("\n"; " // "))] | join("\n"))
  ' "$bdd_verdict")

  local _prompts_dir="${MINI_ORK_DIR:-$MINI_ORK_ROOT}/prompts"
  local template="$_prompts_dir/reflection-refiner.md"
  local tmp_a="$iter_dir/.refl-a.tmp" tmp_b="$iter_dir/.refl-b.tmp" tmp_c="$iter_dir/.refl-c.tmp" tmp_d="$iter_dir/.refl-d.tmp"

  awk -v m='{{KICKOFF_BODY}}' '
    !found && index($0,m) { found=1; gsub(m,""); if(length($0)) print; next }
    !found { print > "/dev/stdout" }
    found  { print > "/dev/stderr" }
  ' "$template" >"$tmp_a" 2>"$tmp_b" || true
  awk -v m='{{DIFF_FILES}}' '
    !found && index($0,m) { found=1; gsub(m,""); if(length($0)) print; next }
    !found { print > "/dev/stdout" }
    found  { print > "/dev/stderr" }
  ' "$tmp_b" >"$tmp_b.h" 2>"$tmp_c" || true; mv "$tmp_b.h" "$tmp_b"
  awk -v m='{{FAILURE_SUMMARY}}' '
    !found && index($0,m) { found=1; gsub(m,""); if(length($0)) print; next }
    !found { print > "/dev/stdout" }
    found  { print > "/dev/stderr" }
  ' "$tmp_c" >"$tmp_c.h" 2>"$tmp_d" || true; mv "$tmp_c.h" "$tmp_c"

  for f in "$tmp_a" "$tmp_b" "$tmp_c" "$tmp_d"; do
    sed -i.bak -e "s|{{KICKOFF_PATH}}|${kickoff_rel//|/\\|}|g" "$f"
    rm -f "$f.bak"
  done

  {
    cat "$tmp_a"
    cat "$kickoff_abs"
    cat "$tmp_b"
    echo "$diff_files"
    cat "$tmp_c"
    echo "$failure_summary"
    cat "$tmp_d"
  } > "$prompt_path"
  rm -f "$tmp_a" "$tmp_b" "$tmp_c" "$tmp_d"

  local lane="${MO_REFLECTION_LANE:-glm}"
  echo "[mini-ork] reflection-refiner epic=$epic iter=$iter (model=$lane)" >&2

  local scripts_dir="${AGENT_SCRIPTS_DIR:-$MINI_ORK_ROOT/lib/providers}"
  local env_script="$scripts_dir/cl_${lane}.sh"
  if [ ! -f "$env_script" ]; then
    echo "[mini-ork] reflection-refiner: env script missing for lane=$lane → $env_script" >&2
    return 0  # advisory; non-fatal
  fi
  # Fix #1+#2: reflection is root-cause analysis — medium effort + tight budget.
  # Free lanes (coding plan) get no budget cap.
  local _budget_flag=()
  mo_emit_budget_flag _budget_flag "$lane" "${MO_REFLECTION_BUDGET_USD:-0.40}"
  (
    set -uo pipefail
    [ -f "$env_script" ] && source "$env_script"
    export CLAUDE_CODE_EFFORT_LEVEL="${MO_REFLECTION_EFFORT:-medium}"
    cd "$REPO_ROOT" || exit 1
    # PROFILE-MAKER-V11 hardening: cap reflection at 8min.
    local _TO="${MO_REFLECTION_TIMEOUT_SEC:-480}"
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

  # Extract markdown response. Reflection prompt asks for plain markdown;
  # extract from "## Reflection refiner" heading onwards.
  awk '/^## Reflection refiner/,/EOF/' "$log_path" 2>/dev/null > "$refl_path"
  if [ ! -s "$refl_path" ]; then
    # Fallback: prefix the failure summary itself as a basic refinement.
    cat > "$refl_path" <<EOF
## Reflection refiner — fallback (LLM output unparseable)

The reflection refiner did not produce parseable output. Raw failure summary:

$failure_summary

See iter-$iter/reflection.log for the full transcript.
EOF
  fi
}

# Append reflection to an existing feedback file (called by run.sh on
# REQUEST_CHANGES with BDD failure).
mo_append_reflection_to_feedback() {
  local epic="$1" iter="$2" feedback_path="$3"
  local refl="$(mo_run_dir "$epic")/iter-$iter/reflection.md"
  if [ -f "$refl" ]; then
    {
      echo
      cat "$refl"
    } >> "$feedback_path"
  fi
}
