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
    # Fix #1+#2 (2026-06-03): switch to --output-format json + --json-schema.
    # Stream-json with thinking_delta sequences caused all 4 extraction strategies
    # to fall through to parse_error:true (insforge memory id=1253). Single-result
    # json wrapper with schema constraint forces the model into structured output
    # so the extractor has one canonical path (jq -r '.result' log_path).
    local _RUBRIC_SCHEMA='{"type":"object","properties":{"pass":{"type":"boolean"},"score":{"type":"integer","minimum":0,"maximum":8},"items":{"type":"array","items":{"type":"object","properties":{"label":{"type":"string"},"verdict":{"type":"string","enum":["PASS","FAIL","SKIP"]},"note":{"type":"string"}},"required":["label","verdict"]}}},"required":["pass","score","items"]}'
    ${_TIMEOUT_BIN:-} ${_TIMEOUT_BIN:+--kill-after=30s $_TO} claude -p \
      "${_cache_flag[@]}" \
      "${_budget_flag[@]}" \
      --output-format json \
      --json-schema "$_RUBRIC_SCHEMA" \
      --dangerously-skip-permissions \
      --permission-mode acceptEdits \
      "$(cat "$prompt_path")" \
      > "$log_path" 2>&1
  ) || true

  # Extract JSON. Primary path: --output-format json wrapper has model output
  # in .result field. Fallback: legacy stream-json shape (in case of mixed
  # deployment).
  local result_text
  result_text=$(jq -r '.result // empty' "$log_path" 2>/dev/null)
  if [ -z "$result_text" ]; then
    result_text=$(jq -r '
      select(.type=="assistant")
      | .message.content[]?
      | select(.type=="text")
      | .text
    ' "$log_path" 2>/dev/null)
  fi
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
    # 2026-06-02: Preserve LLM output snippet in parse_error case so the
    # operator can diagnose why all 4 extraction strategies missed.
    # Forensic context: the host application WAVE 3a + 3b shipped 8/8 sub-epics via
    # manual squash-merge rescue because every iter aborted at this exact
    # branch with no diagnostic. See docs/fixes/20260602-reviewer-silent-die.md
    # for the broader 4-fix cascade (stream-json → json, json-schema, soft-fail,
    # this diagnostic). This is the smallest fix, applied first as a
    # no-regression-risk down payment.
    local _diag_snippet=""
    if [ -n "$result_text" ]; then
      _diag_snippet=$(printf '%s' "$result_text" | tail -c 800)
    fi
    jq -n --arg diag "$_diag_snippet" --arg log_path "$log_path" \
      '{pass: false, score: -1, parse_error: true, items: [],
        parse_error_diagnostic: $diag,
        parse_error_log_hint: ("inspect last 200 lines of " + $log_path)}' \
      > "$rubric_path"
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

# ── mini-ork run-lifecycle entry point ───────────────────────────────────────
# mo_rubric_run_score <kickoff_path> <run_dir> <task_class>
#
# Run-shaped sibling of mo_run_rubric_prescreen (which is epic/iter-shaped
# for the mini-orch deliver flow and needs the epics table + worktrees).
# Dispatches through llm_dispatch so cost/telemetry land in llm_calls.
#
# Writes:
#   <run_dir>/rubric.json          {pass, score 0-8, items[]}
#   <run_dir>/panel-verdict.json   {panel_score 0-100, ...} — consumed by
#                                  lib/promotion_gate.sh (was fail-open
#                                  forever because nothing wrote this file)
#   execution_traces row           status=success|failure with the rubric
#                                  JSON in verifier_output, so auto-reflect
#                                  (gradient_extract) turns FAIL items into
#                                  gradient_records → injected into future
#                                  prompts of the same task_class.
#
# Advisory: always returns 0 unless inputs are missing. A bad rubric run
# must never fail the lifecycle.
mo_rubric_run_score() {
  local kickoff_path="${1:?kickoff_path required}"
  local run_dir="${2:?run_dir required}"
  local task_class="${3:-generic}"

  [ -f "$kickoff_path" ] || { echo "rubric: kickoff not found: $kickoff_path" >&2; return 1; }
  [ -d "$run_dir" ]      || { echo "rubric: run_dir not found: $run_dir" >&2; return 1; }
  declare -f llm_dispatch >/dev/null 2>&1 || { echo "rubric: llm_dispatch not loaded" >&2; return 1; }

  local template="$MINI_ORK_ROOT/prompts/rubric-prescreen.md"
  [ -f "$template" ] || { echo "rubric: template missing: $template" >&2; return 1; }

  local rubric_path="$run_dir/rubric.json"
  local verdict_path="$run_dir/panel-verdict.json"

  # Bounded work-product summary: artifact list + head of each text file.
  # Plays the role {{DIFF_SUMMARY}} plays in the epic flow.
  local artifact_summary
  artifact_summary=$(python3 - "$run_dir" <<'PY'
import os, sys
run_dir = sys.argv[1]
lines = []
for name in sorted(os.listdir(run_dir)):
    path = os.path.join(run_dir, name)
    if not os.path.isfile(path) or name.startswith("."):
        continue
    size = os.path.getsize(path)
    lines.append(f"### {name} ({size} bytes)")
    if name.endswith((".md", ".json", ".txt", ".yaml", ".log")) and size > 0:
        try:
            with open(path, errors="replace") as f:
                head = "".join(f.readlines()[:25])
            lines.append(head[:2000].rstrip())
        except Exception:
            pass
    lines.append("")
print("\n".join(lines)[:12000])
PY
)
  [ -n "$artifact_summary" ] || artifact_summary="(run dir contains no readable artifacts)"

  local prompt_text
  prompt_text=$(python3 - "$template" "$kickoff_path" <<'PY' "$artifact_summary"
import sys
template, kickoff = sys.argv[1], sys.argv[2]
artifacts = sys.argv[3]
body = open(template, errors="replace").read()
body = body.replace("{{KICKOFF_BODY}}", open(kickoff, errors="replace").read())
body = body.replace("{{DIFF_SUMMARY}}", artifacts)
print(body)
PY
)

  echo "  rubric: scoring run artifacts (task_class=$task_class)" >&2
  local raw rc=0
  raw=$(llm_dispatch \
    --task-class "$task_class" \
    --node-type rubric \
    --prompt-text "$prompt_text" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  rubric: dispatch failed (rc=$rc): $(printf '%s' "$raw" | tail -c 300)" >&2
    jq -n '{pass: false, score: -1, parse_error: true, items: []}' > "$rubric_path"
    return 0
  fi

  # Extract the {"pass":...} object (model may wrap it in prose/fences).
  local extracted
  extracted=$(RESULT_TEXT="$raw" python3 - <<'PY' 2>/dev/null
import re, sys, json, os
text = os.environ.get("RESULT_TEXT", "")
starts = [m.start() for m in re.finditer(r'\{[^{]*?"pass"\s*:', text)]
for start in reversed(starts):
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if esc: esc = False; continue
        if c == '\\': esc = True; continue
        if c == '"': in_str = not in_str; continue
        if in_str: continue
        if c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                cand = text[start:i+1]
                try:
                    json.loads(cand); print(cand); sys.exit(0)
                except Exception:
                    break
PY
)

  if [ -n "$extracted" ] && echo "$extracted" | jq -e '.pass != null and .score != null' >/dev/null 2>&1; then
    echo "$extracted" | jq -c '.' > "$rubric_path"
  else
    jq -n --arg diag "$(printf '%s' "$raw" | tail -c 800)" \
      '{pass: false, score: -1, parse_error: true, items: [], parse_error_diagnostic: $diag}' \
      > "$rubric_path"
  fi

  local score pass
  score=$(jq -r '.score' "$rubric_path")
  pass=$(jq -r '.pass' "$rubric_path")
  echo "  rubric: pass=$pass score=$score/8 → $rubric_path" >&2

  # Panel verdict for the promotion gate: 0-8 → 0-100.
  if [ "$score" != "-1" ]; then
    jq -n --argjson score "$score" --argjson pass "$pass" \
          --arg src "rubric-prescreen" --arg tc "$task_class" \
      '{panel_score: ($score * 12.5), pass: $pass, source: $src,
        task_class: $tc, scale: "rubric 0-8 mapped to 0-100"}' \
      > "$verdict_path"
  fi

  # Learning hook: persist as an execution trace so reflection_extract_
  # gradients (auto-reflect at end of run) sees the rubric outcome and
  # mints gradients from FAIL items. gradient_store joins task_class from
  # this row via evidence=trace_id.
  if declare -f trace_write >/dev/null 2>&1; then
    # trace_write's ${MINI_ORK_DB:?} expansion kills the whole shell when
    # unset — even under `|| true` — so resolve the default here.
    export MINI_ORK_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-$MINI_ORK_ROOT/.mini-ork}/state.db}"
    local _trace_id="tr-rubric-$(date +%s)-$$"
    local _status="failure"
    [ "$pass" = "true" ] && _status="success"
    local _payload
    _payload=$(jq -n --arg tid "$_trace_id" --arg tc "$task_class" \
                     --arg st "$_status" --arg ref "$rubric_path" \
                     --slurpfile rub "$rubric_path" \
      '{trace_id: $tid, task_class: $tc, status: $st,
        final_artifact_ref: $ref, verifier_output: $rub[0]}')
    trace_write "$_payload" >/dev/null 2>&1 || true
  fi
  return 0
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
