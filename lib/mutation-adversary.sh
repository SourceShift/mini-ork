#!/usr/bin/env bash
# mini-ork Mutation Adversary — Phase A.3
# Generates 5 plausible buggy implementations of the epic's feature, each
# as a small unified-diff patch. The Spec Author's spec must catch ≥80%
# of them; otherwise the spec is downgraded with feedback.
#
# Source from dispatch.sh.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Args: epic worktree iter spec_path
# Writes: <run-dir>/iter-<N>/mutations.json + per-mutation .patch files
mo_run_mutation_adversary() {
  local epic="$1" worktree="$2" iter="$3" spec_path="$4"
  local run_dir
  run_dir="$(mo_run_dir "$epic")"
  local iter_dir="$run_dir/iter-$iter"
  local prompt_path="$iter_dir/mutation-adversary-prompt.md"
  local log_path="$iter_dir/mutation-adversary.log"
  local mutations_json="$iter_dir/mutations.json"
  mkdir -p "$iter_dir"

  local _db="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"
  local kickoff_rel
  kickoff_rel=$(sqlite3 "$_db" \
    "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
  local kickoff_abs="$REPO_ROOT/$kickoff_rel"

  local _prompts_dir="${MINI_ORK_DIR:-$MINI_ORK_ROOT}/prompts"

  # T3: cache lookup. Hash = kickoff_body + spec_body.
  if [ "${MO_SKIP_CACHE:-0}" -ne 1 ] && [ -f "$spec_path" ]; then
    local _spec_body=$(cat "$spec_path")
    local cache_hash
    cache_hash=$(printf '%s\x1e%s\x1e%s' "$(cat "$kickoff_abs")" "$_spec_body" "$(cat "$_prompts_dir/mutation-adversary.md" 2>/dev/null | mo_cache_input_hash)" | mo_cache_input_hash)
    local cached
    cached=$(mo_cache_lookup mutation-adversary "$epic" "$iter" "$cache_hash")
    if [ -n "$cached" ] && [ -f "$cached" ]; then
      cp "$cached" "$mutations_json"
      mo_cache_record_hit mutation-adversary "$epic" "$iter" "$cache_hash"
      local saved
      saved=$(sqlite3 "$_db" "
        SELECT printf('%.2f', cost_usd) FROM mini_orch_sessions
        WHERE epic_id='$epic' AND iter=$iter AND stage='mutation-adversary'
          AND input_hash='$cache_hash' AND status='success'
        ORDER BY updated_at DESC LIMIT 1;
      " 2>/dev/null)
      echo "[mini-ork] CACHE HIT: mutation-adversary epic=$epic iter=$iter saved=\$$saved" >&2
      return 0
    fi
  fi

  local template="$_prompts_dir/mutation-adversary.md"

  # Same file-split substitution pattern as spec-author.sh.
  local tmp_a="$iter_dir/.mut-a.tmp" tmp_b="$iter_dir/.mut-b.tmp" tmp_c="$iter_dir/.mut-c.tmp"
  awk -v m='{{KICKOFF_BODY}}' '
    !found && index($0,m) { found=1; gsub(m,""); if(length($0)) print; next }
    !found { print > "/dev/stdout" }
    found  { print > "/dev/stderr" }
  ' "$template" >"$tmp_a" 2>"$tmp_b" || true
  awk -v m='{{SPEC_BODY}}' '
    !found && index($0,m) { found=1; gsub(m,""); if(length($0)) print; next }
    !found { print > "/dev/stdout" }
    found  { print > "/dev/stderr" }
  ' "$tmp_b" >"$tmp_b.head" 2>"$tmp_c" || true
  mv "$tmp_b.head" "$tmp_b"

  for f in "$tmp_a" "$tmp_b" "$tmp_c"; do
    sed -i.bak \
      -e "s|{{SPEC_PATH}}|${spec_path//|/\\|}|g" \
      -e "s|{{KICKOFF_PATH}}|${kickoff_rel//|/\\|}|g" \
      "$f"
    rm -f "$f.bak"
  done

  {
    cat "$tmp_a"
    cat "$kickoff_abs"
    cat "$tmp_b"
    if [ -f "$spec_path" ]; then cat "$spec_path"; else echo "(MISSING)"; fi
    cat "$tmp_c"
  } > "$prompt_path"
  rm -f "$tmp_a" "$tmp_b" "$tmp_c"

  local lane="${MO_MUTATION_LANE:-kimi}"
  echo "[mini-ork] mutation-adversary epic=$epic iter=$iter (model=$lane)" >&2

  local scripts_dir="${AGENT_SCRIPTS_DIR:-$MINI_ORK_ROOT/lib/providers}"
  local env_script="$scripts_dir/cl_${lane}.sh"
  if [ ! -f "$env_script" ]; then
    echo "[mini-ork] mutation-adversary: env script missing for lane=$lane → $env_script" >&2
    return 5
  fi

  # mutation-adversary is mechanical creativity — low effort + tight budget.
  local _budget_flag=()
  mo_emit_budget_flag _budget_flag "$lane" "${MO_MUTATION_BUDGET_USD:-1.20}"
  (
    set -uo pipefail
    # shellcheck disable=SC1090
    [ -f "$env_script" ] && source "$env_script"
    export CLAUDE_CODE_EFFORT_LEVEL="${MO_MUTATION_EFFORT:-low}"
    export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${MO_MUTATION_MAX_OUTPUT_TOKENS:-8000}"
    cd "$REPO_ROOT" || exit 1
    local _TO="${MO_MUTATION_TIMEOUT_SEC:-600}"
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
  )
  local rc=$?

  # Extract JSON — scan all assistant text blocks (handles stop-hook clobbering).
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
    extracted=$(RESULT_TEXT="$result_text" python3 -c '
import os, re, sys, json
text = os.environ.get("RESULT_TEXT", "")
starts = [m.start() for m in re.finditer(r"\{[^{]*?\"mutations\"", text)]
for start in reversed(starts):
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if esc: esc = False; continue
        if c == "\\": esc = True; continue
        if c == "\"" and not esc: in_str = not in_str; continue
        if in_str: continue
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                cand = text[start:i+1]
                try: json.loads(cand); print(cand); sys.exit(0)
                except Exception: break
' 2>/dev/null)
  fi
  if [ -z "$extracted" ]; then
    extracted=$(awk '
      /\{[[:space:]]*"mutations"[[:space:]]*:/ {
        buf = $0
        while ((getline next_line) > 0) buf = buf "\n" next_line
        print buf; exit
      }
    ' "$log_path" 2>/dev/null)
  fi

  if echo "$extracted" | jq -e '.mutations' >/dev/null 2>&1; then
    echo "$extracted" | jq -c '.' > "$mutations_json"
  else
    jq -n '{mutations: [], parse_error: true, skipped: false}' > "$mutations_json"
  fi

  # T3: cache emit.
  if [ "$rc" -eq 0 ] && [ "${MO_SKIP_CACHE:-0}" -ne 1 ] && [ -f "$spec_path" ]; then
    local _spec_body=$(cat "$spec_path")
    local cache_hash
    cache_hash=$(printf '%s\x1e%s\x1e%s' "$(cat "$kickoff_abs")" "$_spec_body" "$(cat "$_prompts_dir/mutation-adversary.md" 2>/dev/null | mo_cache_input_hash)" | mo_cache_input_hash)
    read -r cost turns dur <<< "$(mo_cache_costline_from_log "$log_path")"
    mo_cache_emit mutation-adversary "$epic" "$iter" "$cache_hash" "success" \
      "$mutations_json" "$log_path" "$cost" "$turns" "$dur"
  fi

  return "$rc"
}

# Apply each mutation as a patch, run the spec, count which ones the spec
# catches. Output: <iter-dir>/mutation-results.json with per-mutation
# {id, applied, spec_caught, expected_target}.
#
# WARNING: this modifies the worker's worktree temporarily. We git apply -R
# each mutation after testing. If git is dirty going in, we abort.
mo_run_mutation_validator() {
  local epic="$1" worktree="$2" iter="$3" spec_path="$4"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local mutations_json="$iter_dir/mutations.json"
  local results_json="$iter_dir/mutation-results.json"
  local log_path="$iter_dir/mutation-validator.log"

  if [ ! -f "$mutations_json" ]; then
    echo "[mini-ork] mutation-validator: no mutations.json — skipping" >&2
    echo "MISSING"
    return 0
  fi

  local skipped
  skipped=$(jq -r '.skipped // false' "$mutations_json")
  if [ "$skipped" = "true" ]; then
    echo "[mini-ork] mutation-validator: epic skipped per adversary (BE-only)" >&2
    jq -n '{kill_rate: 1.0, skipped: true, results: []}' > "$results_json"
    echo "SKIP"
    return 0
  fi

  local count
  count=$(jq -r '.mutations | length' "$mutations_json" 2>/dev/null || echo 0)
  if [ "$count" -eq 0 ]; then
    echo "[mini-ork] mutation-validator: zero mutations to apply" >&2
    jq -n '{kill_rate: 0.0, skipped: false, results: [], note: "adversary returned zero mutations"}' > "$results_json"
    echo "EMPTY"
    return 0
  fi

  # Pre-check worktree state. Refuse if uncommitted changes.
  if ! git -C "$worktree" diff --quiet 2>/dev/null; then
    echo "[mini-ork] mutation-validator: worktree dirty — aborting (would unsafely stash worker changes)" >&2
    jq -n '{kill_rate: -1, skipped: false, error: "worktree dirty"}' > "$results_json"
    echo "ABORTED"
    return 1
  fi

  local results="[]"
  local killed=0
  for i in $(seq 0 $((count - 1))); do
    local mut
    mut=$(jq -c ".mutations[$i]" "$mutations_json")
    local mid; mid=$(echo "$mut" | jq -r '.id // ("M" + (." | tostring))')
    local target; target=$(echo "$mut" | jq -r '.target_scenario // ""')
    local patch_path="$iter_dir/${mid}.patch"
    echo "$mut" | jq -r '.diff // ""' > "$patch_path"

    local applied=false caught=false reason=""
    if (cd "$worktree" && git apply --check "$patch_path" 2>>"$log_path"); then
      (cd "$worktree" && git apply "$patch_path" 2>>"$log_path") && applied=true
      if [ "$applied" = "true" ]; then
        if (cd "$worktree" && timeout 60 npx playwright test "$spec_path" --reporter=line >>"$log_path" 2>&1); then
          caught=false
          reason="spec passed under mutation (mutation NOT caught)"
        else
          caught=true
          reason="spec failed → mutation detected"
          killed=$((killed + 1))
        fi
        (cd "$worktree" && git apply -R "$patch_path" 2>>"$log_path") || true
      else
        reason="git apply succeeded check but failed apply"
      fi
    else
      reason="git apply --check refused; not a valid diff against current tree"
    fi

    results=$(echo "$results" | jq -c \
      --arg id "$mid" \
      --arg target "$target" \
      --argjson applied "$applied" \
      --argjson caught "$caught" \
      --arg reason "$reason" \
      '. + [{id: $id, target_scenario: $target, applied: $applied, caught: $caught, reason: $reason}]')
  done

  local total="$count"
  local kill_rate
  if [ "$total" -gt 0 ]; then
    kill_rate=$(awk -v k="$killed" -v t="$total" 'BEGIN{ printf "%.3f", k/t }')
  else
    kill_rate=0
  fi

  jq -n \
    --argjson results "$results" \
    --argjson kill_rate "$kill_rate" \
    --argjson total "$total" \
    --argjson killed "$killed" \
    '{kill_rate: $kill_rate, total: $total, killed: $killed, skipped: false, results: $results}' \
    > "$results_json"

  echo "[mini-ork] mutation-validator epic=$epic kill_rate=$kill_rate ($killed / $total)" >&2

  # Threshold: ≥80% per TDAD paper §3.3.
  local threshold_pass
  threshold_pass=$(awk -v r="$kill_rate" 'BEGIN{ if (r >= 0.8) print "PASS"; else print "FAIL" }')
  echo "$threshold_pass"
}
