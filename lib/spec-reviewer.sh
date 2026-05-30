#!/usr/bin/env bash
# mini-ork Spec Reviewer — Phase A.2
# Invokes a model to review the BDD spec the author just wrote. Emits
# spec-verdict.json next to spec-author-prompt.md.
#
# Foreground only. Source from dispatch.sh.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Caller exports: REPO_ROOT, MINI_ORK_DIR, AGENTFLOW_DIR

# Args: epic worktree iter spec_path
# Writes: <run-dir>/iter-<N>/spec-verdict.json
mo_run_spec_reviewer() {
  local epic="$1" worktree="$2" iter="$3" spec_path="$4"
  local run_dir
  run_dir="$(mo_run_dir "$epic")"
  local iter_dir="$run_dir/iter-$iter"
  mkdir -p "$iter_dir"

  local prompt_path="$iter_dir/spec-reviewer-prompt.md"
  local log_path="$iter_dir/spec-reviewer.log"
  local verdict_path="$iter_dir/spec-verdict.json"
  local _db="${MINI_ORK_DB:-$AGENTFLOW_DIR/state.db}"

  local kickoff_rel
  kickoff_rel=$(sqlite3 "$_db" \
    "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
  if [ -z "$kickoff_rel" ]; then
    echo "[mini-ork] spec-reviewer: no kickoff for $epic" >&2
    return 2
  fi
  local kickoff_abs="$REPO_ROOT/$kickoff_rel"
  local _prompts_dir="${MINI_ORK_DIR:-$MINI_ORK_ROOT}/prompts"
  local template="$_prompts_dir/spec-reviewer.md"

  # T2: cache lookup — kickoff_body + spec_body.
  if [ "${MO_SKIP_CACHE:-0}" -ne 1 ] && [ -f "$spec_path" ]; then
    local spec_body=$(cat "$spec_path")
    local cache_hash
    cache_hash=$(printf '%s\x1e%s\x1e%s' "$(cat "$kickoff_abs")" "$spec_body" "$(cat "$template" 2>/dev/null | mo_cache_input_hash)" | mo_cache_input_hash)
    local cached_verdict
    cached_verdict=$(mo_cache_lookup spec-reviewer "$epic" "$iter" "$cache_hash")
    if [ -n "$cached_verdict" ] && [ -f "$cached_verdict" ]; then
      cp "$cached_verdict" "$verdict_path"
      mo_cache_record_hit spec-reviewer "$epic" "$iter" "$cache_hash"
      local saved
      saved=$(sqlite3 "$_db" "
        SELECT printf('%.2f', cost_usd) FROM mini_orch_sessions
        WHERE epic_id='$epic' AND iter=$iter AND stage='spec-reviewer'
          AND input_hash='$cache_hash' AND status='success'
        ORDER BY updated_at DESC LIMIT 1;
      " 2>/dev/null)
      local v
      v=$(jq -r '.verdict' "$verdict_path")
      echo "[mini-ork] CACHE HIT: spec-reviewer epic=$epic iter=$iter verdict=$v saved=\$$saved" >&2
      return 0
    fi
  fi

  # Compose by file split — two big markers: {{KICKOFF_BODY}} and {{SPEC_BODY}}.
  local tmp_a="$iter_dir/.spec-rev-a.tmp" tmp_b="$iter_dir/.spec-rev-b.tmp" tmp_c="$iter_dir/.spec-rev-c.tmp"
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
    if [ -f "$spec_path" ]; then
      cat "$spec_path"
    else
      echo "(SPEC FILE MISSING — author claimed to write but file absent)"
    fi
    cat "$tmp_c"
  } > "$prompt_path"

  rm -f "$tmp_a" "$tmp_b" "$tmp_c"

  # Lane-based spec-reviewer (default GLM since most deployments have a free coding plan).
  # Override via MO_SPEC_REVIEWER_LANE=opus.
  local spec_lane="${MO_SPEC_REVIEWER_LANE:-glm}"
  local spec_model spec_env_script
  # deepseek lane alias → glm
  local _ds_fb="${MO_DEEPSEEK_FALLBACK_LANE:-glm}"
  [ "$spec_lane" = "deepseek" ] && spec_lane="$_ds_fb"
  local scripts_dir="${AGENT_SCRIPTS_DIR:-$MINI_ORK_ROOT/lib/providers}"
  case "$spec_lane" in
    opus)     spec_model="claude-opus-4-7";    spec_env_script="" ;;
    sonnet)   spec_model="claude-sonnet-4-6";  spec_env_script="" ;;
    glm)      spec_model="GLM-5.1";            spec_env_script="$scripts_dir/cl_glm.sh" ;;
    kimi)     spec_model="kimi-k2.6";          spec_env_script="$scripts_dir/cl_kimi.sh" ;;
    minimax)  spec_model="MiniMax-M2.7";       spec_env_script="$scripts_dir/cl_minimax.sh" ;;
    *)
      echo "[mini-ork] spec-reviewer: unknown lane=$spec_lane — falling back to opus" >&2
      spec_lane=opus; spec_model="claude-opus-4-7"; spec_env_script=""
      ;;
  esac
  if [ -n "$spec_env_script" ] && [ ! -f "$spec_env_script" ]; then
    echo "[mini-ork] spec-reviewer: env script missing for lane=$spec_lane → $spec_env_script — falling back to opus" >&2
    spec_lane=opus; spec_model="claude-opus-4-7"; spec_env_script=""
  fi
  echo "[mini-ork] spec-reviewer epic=$epic iter=$iter (lane=$spec_lane model=$spec_model)" >&2

  local _budget_flag=()
  mo_emit_budget_flag _budget_flag "$spec_lane" "${MO_SPEC_REVIEWER_BUDGET_USD:-5.00}"
  (
    if [ -n "$spec_env_script" ]; then
      # shellcheck disable=SC1090
      source "$spec_env_script"
    fi
    export CLAUDE_CODE_EFFORT_LEVEL="${MO_SPEC_REVIEWER_EFFORT:-low}"
    export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${MO_SPEC_REVIEWER_MAX_OUTPUT_TOKENS:-6000}"
    cd "$REPO_ROOT" || exit 1
    local _TO="${MO_SPEC_REVIEWER_TIMEOUT_SEC:-600}"
    local _TIMEOUT_BIN=""
    command -v gtimeout >/dev/null 2>&1 && _TIMEOUT_BIN=gtimeout
    command -v timeout >/dev/null 2>&1 && [ -z "$_TIMEOUT_BIN" ] && _TIMEOUT_BIN=timeout
    local _cache_flag=()
    mo_emit_cache_flags _cache_flag 2>/dev/null || true
    ${_TIMEOUT_BIN:-} ${_TIMEOUT_BIN:+--kill-after=30s $_TO} claude -p \
      "${_cache_flag[@]}" \
      --output-format stream-json \
      --verbose \
      --include-partial-messages \
      --model "$spec_model" \
      "${_budget_flag[@]}" \
      --dangerously-skip-permissions \
      --permission-mode acceptEdits \
      "$(cat "$prompt_path")" \
      > "$log_path" 2>&1
  )
  local rc=$?

  mo_extract_spec_verdict "$log_path" "$verdict_path"

  # ─── Deterministic post-process gates ──────────────────────────────
  # Encode load-bearing rules deterministically so model-compliance issues
  # can't ship a broken spec.
  if [ -f "$spec_path" ]; then
    local spec_body
    spec_body=$(cat "$spec_path")
    local hard_issues=""

    # Rule 1: protected-route specs MUST call mockApiCatchAll BEFORE goto.
    if echo "$spec_body" | grep -qE "page\.goto\(['\"]\s*/lw/" \
       && ! echo "$spec_body" | grep -q "mockApiCatchAll"; then
      hard_issues="$hard_issues
{\"severity\":\"blocker\",\"category\":\"test_setup\",\"file\":\"${spec_path#$REPO_ROOT/}\",\"description\":\"Missing mockApiCatchAll(page) in beforeEach. Without it, fake JWT can't satisfy real BE 401s; the spec redirects to /login and every assertion fails. Add: import { mockApiCatchAll } from './_helpers'; await mockApiCatchAll(page); in beforeEach AFTER seedAuth.\"}"
    fi

    # Rule 2: public-route specs MUST NOT call seedAuth.
    if echo "$spec_body" | grep -qE "page\.goto\(['\"]\s*/(login|register|forgot-password)" \
       && echo "$spec_body" | grep -q "seedAuth"; then
      hard_issues="$hard_issues
{\"severity\":\"blocker\",\"category\":\"test_setup\",\"file\":\"${spec_path#$REPO_ROOT/}\",\"description\":\"seedAuth(page) called in a spec that targets a public route (/login, /register, /forgot-password). PublicRoute redirects authenticated users away — your test target never mounts. Remove seedAuth from beforeEach for this spec.\"}"
    fi

    if [ -n "$hard_issues" ]; then
      hard_issues=$(echo "$hard_issues" | sed '/^$/d' | jq -c -s '.')
      jq --argjson hi "$hard_issues" '
        .verdict = "REQUEST_CHANGES_SPEC"
        | .issues = (.issues // []) + $hi
        | .feedback_to_author = ((.feedback_to_author // "") + "\n\nDETERMINISTIC GATE (spec-reviewer post-process): the spec violates a project-wide invariant that breaks every test in the file. See injected issue(s).")
      ' "$verdict_path" > "$verdict_path.tmp" && mv "$verdict_path.tmp" "$verdict_path"
      echo "[mini-ork] spec-reviewer DETERMINISTIC FAIL: forced REQUEST_CHANGES_SPEC ($(echo "$hard_issues" | jq 'length') hard-rule violations)" >&2
    fi
  fi

  local v_after
  v_after=$(jq -r '.verdict // "UNKNOWN"' "$verdict_path" 2>/dev/null)

  # T2: emit cache row at success.
  if [ "$rc" -eq 0 ] && [ "${MO_SKIP_CACHE:-0}" -ne 1 ]; then
    local spec_body=""
    if [ -f "$spec_path" ]; then spec_body=$(cat "$spec_path"); fi
    local cache_hash
    cache_hash=$(printf '%s\x1e%s\x1e%s' "$(cat "$kickoff_abs")" "$spec_body" "$(cat "$template" 2>/dev/null | mo_cache_input_hash)" | mo_cache_input_hash)
    read -r cost turns dur <<< "$(mo_cache_costline_from_log "$log_path")"
    mo_cache_emit spec-reviewer "$epic" "$iter" "$cache_hash" "success" \
      "$verdict_path" "$log_path" "$cost" "$turns" "$dur"
  fi

  echo "[mini-ork] spec-reviewer epic=$epic iter=$iter verdict=$v_after" >&2
  return 0
}

# ─── Verdict extractor (pure parse — no LLM call) ───────────────────────
mo_extract_spec_verdict() {
  local log_path="$1" verdict_path="$2"

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
starts = [m.start() for m in re.finditer(r"\{[^{]*?\"verdict\"", text)]
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
      /\{[[:space:]]*"verdict"[[:space:]]*:/ {
        buf = $0
        while ((getline next_line) > 0) buf = buf "\n" next_line
        print buf; exit
      }
    ' "$log_path" 2>/dev/null)
  fi

  if echo "$extracted" | jq -e '.verdict' >/dev/null 2>&1; then
    echo "$extracted" | jq -c '
      {
        verdict: (.verdict // "ESCALATE"),
        rationale: (.rationale // ""),
        issues: (.issues // []),
        feedback_to_author: (.feedback_to_author // "")
      }
    ' > "$verdict_path"
  else
    echo "[mini-ork] mo_extract_spec_verdict: failed to parse JSON; defaulting to ESCALATE" >&2
    jq -n --arg log "$log_path" '
      {
        verdict: "ESCALATE",
        rationale: "spec-reviewer output did not contain parseable JSON verdict",
        issues: [],
        feedback_to_author: ("see log: " + $log)
      }
    ' > "$verdict_path"
  fi
}

# ─── Re-extract path (no LLM call) ──────────────────────────────────────
mo_replay_extract_spec_verdict() {
  local epic="$1" iter="$2"
  local _db="${MINI_ORK_DB:-$AGENTFLOW_DIR/state.db}"
  local cached_log
  cached_log=$(sqlite3 "$_db" "
    SELECT log_path FROM mini_orch_sessions
    WHERE epic_id='$epic' AND iter=$iter AND stage='spec-reviewer'
      AND status='success'
    ORDER BY updated_at DESC LIMIT 1;
  " 2>/dev/null)
  if [ -z "$cached_log" ] || [ ! -f "$cached_log" ]; then
    echo "[mini-ork] mo_replay_extract_spec_verdict: no cached log for $epic iter=$iter" >&2
    return 1
  fi
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  mkdir -p "$iter_dir"
  mo_extract_spec_verdict "$cached_log" "$iter_dir/spec-verdict.json"
  local v
  v=$(jq -r '.verdict // "UNKNOWN"' "$iter_dir/spec-verdict.json" 2>/dev/null)
  echo "[mini-ork] spec-reviewer RE-EXTRACT: epic=$epic iter=$iter verdict=$v (no LLM call)" >&2
}

# Returns: APPROVE_SPEC | REQUEST_CHANGES_SPEC | ESCALATE | UNKNOWN
mo_spec_verdict() {
  local epic="$1" iter="$2"
  local v="$(mo_run_dir "$epic")/iter-$iter/spec-verdict.json"
  if [ ! -f "$v" ]; then echo "UNKNOWN"; return; fi
  jq -r '.verdict // "UNKNOWN"' "$v" 2>/dev/null || echo "UNKNOWN"
}

# Render the spec-reviewer feedback as a markdown file the author can re-read.
mo_spec_feedback_file() {
  local epic="$1" iter="$2"
  local v="$(mo_run_dir "$epic")/iter-$iter/spec-verdict.json"
  local out="$(mo_run_dir "$epic")/iter-$((iter + 1))/spec-feedback.md"
  mkdir -p "$(dirname "$out")"
  jq -r --argjson iter "$iter" '
    "# Spec reviewer feedback (iter " + ($iter | tostring) + ")\n" +
    "\nVerdict: " + .verdict + "\n" +
    "\n## Rationale\n\n" + (.rationale // "") + "\n" +
    "\n## Issues\n\n" + ((.issues // []) | map("- [" + .severity + " · " + .criterion + "] " + .description) | join("\n")) +
    "\n\n## Direct feedback to author\n\n" + (.feedback_to_author // "(none)") + "\n"
  ' "$v" > "$out"
  echo "$out"
}
