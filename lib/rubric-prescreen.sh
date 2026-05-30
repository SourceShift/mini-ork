#!/usr/bin/env bash
# mini-ork Agentic Rubric Pre-Screen — Phase A.5
# Adapted from Agentic Rubrics paper (arXiv 2601.04171). Cheap context-
# grounded checklist of 8 items; runs BEFORE the BDD test execution.
# Advisory only — surfaces as a note in the reviewer's verdict feedback.
#
# Source from dispatch.sh.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Caller exports: MINI_ORK_HOME, REPO_ROOT, MINI_ORK_HOME

# Args: epic worktree iter
# Writes: <iter-dir>/rubric.json
mo_run_rubric_prescreen() {
  local epic="$1" worktree="$2" iter="$3"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local prompt_path="$iter_dir/rubric-prompt.md"
  local log_path="$iter_dir/rubric.log"
  local rubric_path="$iter_dir/rubric.json"
  mkdir -p "$iter_dir"

  local _db="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"
  local kickoff_rel
  kickoff_rel=$(sqlite3 "$_db" \
    "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
  local kickoff_abs="$REPO_ROOT/$kickoff_rel"

  # Diff summary: file list + per-file +/- LOC. Cheaper than full diff.
  local diff_summary
  diff_summary=$(git -C "$worktree" diff --stat main..HEAD 2>/dev/null | head -30)

  local _prompts_dir="${MINI_ORK_DIR:-$MINI_ORK_ROOT}/prompts"

  # T3: cache lookup. Hash = kickoff_body + diff_summary + template content.
  # Re-dispatches with no new commits will hit instantly.
  if [ "${MO_SKIP_CACHE:-0}" -ne 1 ]; then
    local cache_hash
    cache_hash=$(printf '%s\x1e%s\x1e%s' "$(cat "$kickoff_abs")" "$diff_summary" "$(cat "$_prompts_dir/rubric-prescreen.md" 2>/dev/null | mo_cache_input_hash)" | mo_cache_input_hash)
    local cached
    cached=$(mo_cache_lookup rubric "$epic" "$iter" "$cache_hash")
    if [ -n "$cached" ] && [ -f "$cached" ]; then
      cp "$cached" "$rubric_path"
      mo_cache_record_hit rubric "$epic" "$iter" "$cache_hash"
      local pass score
      pass=$(jq -r '.pass' "$rubric_path")
      score=$(jq -r '.score' "$rubric_path")
      echo "[mini-ork] CACHE HIT: rubric epic=$epic iter=$iter pass=$pass score=$score" >&2
      return 0
    fi
  fi

  local template="$_prompts_dir/rubric-prescreen.md"
  local tmp_a="$iter_dir/.rub-a.tmp" tmp_b="$iter_dir/.rub-b.tmp" tmp_c="$iter_dir/.rub-c.tmp"
  awk -v m='{{KICKOFF_BODY}}' '
    !found && index($0,m) { found=1; gsub(m,""); if(length($0)) print; next }
    !found { print > "/dev/stdout" }
    found  { print > "/dev/stderr" }
  ' "$template" >"$tmp_a" 2>"$tmp_b" || true
  awk -v m='{{DIFF_SUMMARY}}' '
    !found && index($0,m) { found=1; gsub(m,""); if(length($0)) print; next }
    !found { print > "/dev/stdout" }
    found  { print > "/dev/stderr" }
  ' "$tmp_b" >"$tmp_b.h" 2>"$tmp_c" || true; mv "$tmp_b.h" "$tmp_b"

  {
    cat "$tmp_a"
    cat "$kickoff_abs"
    cat "$tmp_b"
    echo "$diff_summary"
    cat "$tmp_c"
  } > "$prompt_path"
  rm -f "$tmp_a" "$tmp_b" "$tmp_c"

  local lane="${MO_RUBRIC_LANE:-kimi}"
  echo "[mini-ork] rubric pre-screen epic=$epic iter=$iter (model=$lane)" >&2

  local scripts_dir="${AGENT_SCRIPTS_DIR:-$MINI_ORK_ROOT/lib/providers}"
  local env_script="$scripts_dir/cl_${lane}.sh"
  if [ ! -f "$env_script" ]; then
    echo "[mini-ork] rubric: env script missing for lane=$lane → $env_script" >&2
    jq -n '{pass: false, score: -1, parse_error: true, items: []}' > "$rubric_path"
    return 0  # advisory; don't block pipeline
  fi
  # Fix #1+#2: rubric is an 8-item yes/no checklist — low effort + cheapest budget.
  # Free lanes (coding plan) get no budget cap.
  local _budget_flag=()
  mo_emit_budget_flag _budget_flag "$lane" "${MO_RUBRIC_BUDGET_USD:-0.60}"
  (
    set -uo pipefail
    [ -f "$env_script" ] && source "$env_script"
    export CLAUDE_CODE_EFFORT_LEVEL="${MO_RUBRIC_EFFORT:-low}"
    export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${MO_RUBRIC_MAX_OUTPUT_TOKENS:-2000}"
    cd "$REPO_ROOT" || exit 1
    # PROFILE-MAKER-V11 incident: rubric pre-screen for F (big BE diff)
    # ran 6+ min then hit kimi rate-limit retry storm. Cap at 8min.
    local _TO="${MO_RUBRIC_TIMEOUT_SEC:-480}"
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

  # Extract JSON via all-turns scan (see spec-reviewer.sh comment about
  # stop-hook clobbering .result with checklist text).
  local result_text
  result_text=$(jq -r '
    select(.type=="assistant")
    | .message.content[]?
    | select(.type=="text")
    | .text
  ' "$log_path" 2>/dev/null)
  if [ -z "$result_text" ]; then
    result_text=$(grep '"type":"result"' "$log_path" | tail -1 | jq -r '.result // empty' 2>/dev/null)
  fi
  local extracted=""
  if [ -n "$result_text" ]; then
    RESULT_TEXT="$result_text" extracted=$(python3 - <<'PY' 2>/dev/null
import re, sys, json, os
text = os.environ.get("RESULT_TEXT", "")
starts = [m.start() for m in re.finditer(r'\{[^{]*?"pass"\s*:', text)]
for start in reversed(starts):
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if esc: esc = False; continue
        if c == '\\': esc = True; continue
        if c == '"' and not esc: in_str = not in_str; continue
        if in_str: continue
        if c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                cand = text[start:i+1]
                try: json.loads(cand); print(cand); sys.exit(0)
                except Exception: break
PY
)
  fi
  if [ -z "$extracted" ]; then
    extracted=$(awk '
      /\{[[:space:]]*"pass"[[:space:]]*:/ {
        buf = $0
        while ((getline next_line) > 0) buf = buf "\n" next_line
        print buf; exit
      }
    ' "$log_path" 2>/dev/null)
  fi

  if echo "$extracted" | jq -e '.pass' >/dev/null 2>&1; then
    echo "$extracted" | jq -c '.' > "$rubric_path"
  else
    jq -n '{pass: false, score: -1, parse_error: true, items: []}' > "$rubric_path"
  fi

  local score pass
  score=$(jq -r '.score' "$rubric_path")
  pass=$(jq -r '.pass' "$rubric_path")
  echo "[mini-ork] rubric epic=$epic iter=$iter pass=$pass score=$score" >&2

  # T3: emit cache row.
  if [ "${MO_SKIP_CACHE:-0}" -ne 1 ]; then
    local cache_hash
    cache_hash=$(printf '%s\x1e%s\x1e%s' "$(cat "$kickoff_abs")" "$diff_summary" "$(cat "$_prompts_dir/rubric-prescreen.md" 2>/dev/null | mo_cache_input_hash)" | mo_cache_input_hash)
    read -r cost turns dur <<< "$(mo_cache_costline_from_log "$log_path")"
    mo_cache_emit rubric "$epic" "$iter" "$cache_hash" "success" \
      "$rubric_path" "$log_path" "$cost" "$turns" "$dur"
  fi
}

# Append rubric findings to feedback (advisory only).
mo_append_rubric_to_feedback() {
  local epic="$1" iter="$2" feedback_path="$3"
  local rub="$(mo_run_dir "$epic")/iter-$iter/rubric.json"
  if [ ! -f "$rub" ]; then return; fi

  local pass
  pass=$(jq -r '.pass' "$rub")
  if [ "$pass" = "true" ]; then return; fi

  {
    echo
    echo "## Rubric pre-screen (advisory — Phase A.5)"
    echo
    jq -r '
      "Score: " + (.score | tostring) + "/8 (need ≥6 to PASS)\n\n" +
      ([.items[] | select(.verdict != "PASS") | "- **[" + .verdict + "]** " + .label + " — " + .note] | join("\n"))
    ' "$rub"
  } >> "$feedback_path"
}
