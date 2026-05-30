#!/usr/bin/env bash
# mini-ork Spec Author — Phase A.2
# Invokes a model to write a Playwright BDD spec for one epic, based on the
# epic's kickoff handoff. Output: e2e/<EPIC>_<short>.spec.ts in the
# worker's worktree (committed by the agent itself).
#
# Foreground invocation — sequential is fine, this is once-per-epic at
# spec-synth time. Caller is dispatch.sh's pre-iter spec sub-loop.
#
# Source from dispatch.sh; not meant to run alone.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Caller exports: REPO_ROOT, MINI_ORK_DIR, AGENTFLOW_DIR, JOB_ID

# ─── Discovery ──────────────────────────────────────────────────────────
mo_spec_exists() {
  local epic="$1" worktree="$2"
  ls -1 "${worktree}/e2e/${epic}_"*.spec.ts 2>/dev/null | head -1
}

# ─── Authoring (foreground claude invocation) ───────────────────────────
# Args: epic worktree iter [reviewer_feedback_path]
# Writes:
#   <run-dir>/iter-<N>/spec-author.log
#   <run-dir>/iter-<N>/spec-author-prompt.md
# Returns 0 on author completion (regardless of spec quality — reviewer
# decides). Returns non-zero only on infra failure (claude crash, env missing).
mo_run_spec_author() {
  local epic="$1" worktree="$2" iter="$3" feedback="${4:-}"
  local run_dir
  run_dir="$(mo_run_dir "$epic")"
  local iter_dir="$run_dir/iter-$iter"
  mkdir -p "$iter_dir"

  local prompt_path="$iter_dir/spec-author-prompt.md"
  local log_path="$iter_dir/spec-author.log"
  local _db="${MINI_ORK_DB:-$AGENTFLOW_DIR/state.db}"

  # Retry the kickoff_path lookup on transient SQLITE_BUSY.
  local kickoff_rel="" sqlite_err=""
  for _attempt in 1 2 3 4 5; do
    kickoff_rel=$(sqlite3 "$_db" \
      "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/tmp/spec-author-sqlite-err-$$.log)
    sqlite_err=$(cat /tmp/spec-author-sqlite-err-$$.log 2>/dev/null)
    rm -f /tmp/spec-author-sqlite-err-$$.log
    if [ -n "$kickoff_rel" ]; then break; fi
    if [ -z "$sqlite_err" ]; then break; fi
    echo "[mini-ork] spec-author: sqlite3 err on epic=$epic attempt=$_attempt: $sqlite_err" >&2
    sleep 0.1
  done
  if [ -z "$kickoff_rel" ]; then
    echo "[mini-ork] spec-author: no kickoff in state.db for $epic (last sqlite_err: ${sqlite_err:-none})" >&2
    return 2
  fi
  local kickoff_abs="$REPO_ROOT/$kickoff_rel"
  if [ ! -f "$kickoff_abs" ]; then
    echo "[mini-ork] spec-author: kickoff missing: $kickoff_abs" >&2
    return 3
  fi

  # ─── T1: stage-cache lookup ──────────────────────────────────────────
  local feedback_body=""
  if [ -n "$feedback" ] && [ -f "$feedback" ]; then
    feedback_body=$(cat "$feedback")
  fi
  # T8: salt the cache key with the prompt template's content hash so a
  # template edit auto-invalidates cached entries.
  local _spec_author_template="${MINI_ORK_DIR:-$MINI_ORK_ROOT}/prompts/spec-author.md"
  local prompt_version=""
  [ -f "$_spec_author_template" ] && \
    prompt_version=$(cat "$_spec_author_template" | mo_cache_input_hash)
  local cache_hash
  cache_hash=$(printf '%s\x1e%s\x1e%s' "$(cat "$kickoff_abs")" "$feedback_body" "$prompt_version" | mo_cache_input_hash)

  if [ "${MO_SKIP_CACHE:-0}" -ne 1 ]; then
    local cached_spec
    cached_spec=$(mo_cache_lookup spec-author "$epic" "$iter" "$cache_hash")
    if [ -n "$cached_spec" ] && [ -f "$cached_spec" ]; then
      local cached_name
      cached_name=$(basename "$cached_spec")
      local dest="$worktree/e2e/$cached_name"
      mkdir -p "$worktree/e2e"
      cp "$cached_spec" "$dest"
      mo_cache_record_hit spec-author "$epic" "$iter" "$cache_hash"

      local cached_log
      cached_log=$(sqlite3 "$_db" "
        SELECT log_path FROM mini_orch_sessions
        WHERE epic_id='$epic' AND iter=$iter AND stage='spec-author'
          AND input_hash='$cache_hash' AND status='success'
        ORDER BY updated_at DESC LIMIT 1;
      " 2>/dev/null)
      [ -n "$cached_log" ] && [ -f "$cached_log" ] && cp "$cached_log" "$log_path"

      local saved
      saved=$(sqlite3 "$_db" "
        SELECT printf('%.2f', cost_usd) FROM mini_orch_sessions
        WHERE epic_id='$epic' AND iter=$iter AND stage='spec-author'
          AND input_hash='$cache_hash' AND status='success'
        ORDER BY updated_at DESC LIMIT 1;
      " 2>/dev/null)
      echo "[mini-ork] CACHE HIT: spec-author epic=$epic iter=$iter saved=\$$saved" >&2
      return 0
    fi
  fi

  local _prompts_dir="${MINI_ORK_DIR:-$MINI_ORK_ROOT}/prompts"
  local template="$_prompts_dir/spec-author.md"
  if [ ! -f "$template" ]; then
    echo "[mini-ork] spec-author: prompt template missing: $template" >&2
    return 4
  fi

  # ─── Pre-resolved kickoff hints ──────────────────────────────────────
  # Eliminate the agent's filesystem reconnaissance budget by handing it
  # the facts up front: file paths named in the kickoff, plus route →
  # lazy-import resolution from App.tsx.
  local hints_block=""
  local scope_files
  scope_files=$(grep -oE 'src/[a-zA-Z0-9/_-]+\.(tsx?|jsx?)' "$kickoff_abs" 2>/dev/null | sort -u)
  local scope_routes
  scope_routes=$(awk '
    {
      line = $0
      while (match(line, /\/(lw\/[a-z0-9_-]+(\/[a-z0-9_-]+)*|login(\/[a-z0-9_-]+)*|register(\/[a-z0-9_-]+)*|forgot[a-z-]*|reset[a-z-]*|onboarding(\/[a-z0-9_-]+)*)/)) {
        m = substr(line, RSTART, RLENGTH)
        prev_char = (RSTART == 1) ? "" : substr(line, RSTART - 1, 1)
        if (prev_char !~ /[a-zA-Z]/) {
          sub(/[\.\,\:\;\?\!\)]+$/, "", m)
          if (length(m) > 1) print m
        }
        line = substr(line, RSTART + RLENGTH)
      }
    }
  ' "$kickoff_abs" 2>/dev/null | sort -u)

  if [ -n "$scope_files" ] || [ -n "$scope_routes" ]; then
    hints_block=$'\n\n---\n\n## Pre-resolved kickoff hints (auto-extracted by orch)\n\nUse these instead of grepping the codebase yourself. They are the contract.\n\n'
    if [ -n "$scope_files" ]; then
      hints_block+=$'**Files referenced in kickoff Scope:**\n'
      while IFS= read -r f; do
        if [ -f "$worktree/$f" ]; then
          hints_block+="- \`$f\` (exists — worker will modify)"$'\n'
        else
          hints_block+="- \`$f\` (NEW — worker will create)"$'\n'
        fi
      done <<< "$scope_files"
      hints_block+=$'\n'
    fi
    if [ -n "$scope_routes" ]; then
      hints_block+=$'**Routes referenced in kickoff:**\n'
      while IFS= read -r r; do
        local app_path="$worktree/src/App.tsx"
        if [ -f "$app_path" ]; then
          local match line_no lazy_file
          match=$(grep -nE "path=[\"']${r}[\"']" "$app_path" 2>/dev/null | head -1)
          line_no=$(echo "$match" | cut -d: -f1)
          if [ -n "$line_no" ]; then
            lazy_file=$(awk -v ln="$line_no" 'NR<=ln{buf[NR%15]=$0} END {for (i=NR-14;i<=NR;i++) print buf[i%15]}' "$app_path" 2>/dev/null \
              | grep -oE "import\(['\"][^'\"]+['\"]\)" | tail -1 | sed -E "s/.*['\"]([^'\"]+)['\"].*/\\1/")
            if [ -n "$lazy_file" ]; then
              hints_block+="- \`$r\` → \`$lazy_file\` (lazy import in App.tsx:$line_no)"$'\n'
            else
              hints_block+="- \`$r\` → registered in App.tsx:$line_no"$'\n'
            fi
          else
            hints_block+="- \`$r\` → not found in App.tsx (component-level route OR new route this epic adds)"$'\n'
          fi
        fi
      done <<< "$scope_routes"
      hints_block+=$'\n'
    fi
  fi
  echo "[mini-ork] spec-author hints: $(echo "$scope_files" | wc -l | tr -d ' ') file(s), $(echo "$scope_routes" | wc -l | tr -d ' ') route(s)" >&2

  # ─── BE-only short-circuit (skip LLM entirely) ──────────────────────
  # If kickoff scope contains no UI files (.tsx/.jsx) AND no routes, the
  # spec-author would correctly emit SPEC_SKIPPED after a 30-60s LLM call.
  # Detect mechanically and skip. Disable via MO_SPEC_AUTHOR_SKIP_BE=0.
  if [ "${MO_SPEC_AUTHOR_SKIP_BE:-1}" -eq 1 ]; then
    local has_ui_file=0
    if [ -n "$scope_files" ] && echo "$scope_files" | grep -qE '\.(tsx|jsx)$'; then
      has_ui_file=1
    fi
    if [ "$has_ui_file" -eq 0 ] && [ -z "$scope_routes" ]; then
      echo "[mini-ork] spec-author: BE-only kickoff (no .tsx/.jsx, no routes) — skipping LLM call" >&2
      printf 'SPEC_SKIPPED: BE-only kickoff (no UI files, no routes — heuristic short-circuit)\n' > "$log_path"
      return 0
    fi
  fi

  # No-context A/B probe (When Context Hurts, arXiv 2605.04361): for every
  # Nth (epic, iter) pair, deliberately SKIP memory grounding.
  local probe_skip_memory=0
  local probe_rate="${MO_NO_CONTEXT_PROBE_RATE:-5}"
  if [ "$probe_rate" -gt 0 ] 2>/dev/null; then
    local _h _bucket
    _h=$(printf '%s\x1e%s' "$epic" "$iter" | shasum | cut -c1-8)
    _bucket=$(( 0x$_h % probe_rate ))
    if [ "$_bucket" -eq 0 ]; then
      probe_skip_memory=1
      mkdir -p "$iter_dir"
      printf '{"probe":"no-context","epic":"%s","iter":%s,"rate":%s,"reason":"When Context Hurts, arXiv 2605.04361 — measure memory-hint impact"}\n' \
        "$epic" "$iter" "$probe_rate" > "$iter_dir/no-context-probe.flag"
      echo "[mini-ork] spec-author: NO-CONTEXT PROBE active for $epic/iter-$iter (1/${probe_rate} rate) — skipping memory hints" >&2
    fi
  fi

  # Compose the prompt by file concatenation. The template body has small
  # variable placeholders that are safe for sed substitution. The two LARGE
  # placeholders ({{KICKOFF_BODY}}, {{REVIEWER_FEEDBACK}}) are replaced by
  # file content via a marker-split + cat.
  local tmp_head="$iter_dir/.spec-author-head.tmp"
  local tmp_tail="$iter_dir/.spec-author-tail.tmp"

  awk -v marker='{{KICKOFF_BODY}}' '
    !found && index($0, marker) {
      found = 1
      gsub(marker, "")
      if (length($0) > 0) print
      next
    }
    !found { print > "/dev/stdout" }
    found  { print > "/dev/stderr" }
  ' "$template" >"$tmp_head" 2>"$tmp_tail" || true

  for f in "$tmp_head" "$tmp_tail"; do
    sed -i.bak \
      -e "s|{{EPIC_ID}}|$epic|g" \
      -e "s|{{WORKTREE}}|${worktree//|/\\|}|g" \
      -e "s|{{KICKOFF_PATH}}|${kickoff_rel//|/\\|}|g" \
      "$f"
    rm -f "$f.bak"
  done

  if grep -q '{{REVIEWER_FEEDBACK}}' "$tmp_tail" 2>/dev/null; then
    local tmp_tail_2a="$iter_dir/.spec-author-tail-a.tmp"
    local tmp_tail_2b="$iter_dir/.spec-author-tail-b.tmp"
    awk -v marker='{{REVIEWER_FEEDBACK}}' '
      !found && index($0, marker) {
        found = 1
        gsub(marker, "")
        if (length($0) > 0) print
        next
      }
      !found { print > "/dev/stdout" }
      found  { print > "/dev/stderr" }
    ' "$tmp_tail" >"$tmp_tail_2a" 2>"$tmp_tail_2b" || true

    {
      cat "$tmp_head"
      cat "$kickoff_abs"
      [ -n "$hints_block" ] && printf '%s' "$hints_block"
      cat "$tmp_tail_2a"
      if [ -n "$feedback" ] && [ -f "$feedback" ]; then
        printf '\n---\n\n## Reviewer feedback from previous iteration\n\n'
        cat "$feedback"
      fi
      cat "$tmp_tail_2b"
    } > "$prompt_path"
    rm -f "$tmp_tail_2a" "$tmp_tail_2b"
  else
    {
      cat "$tmp_head"
      cat "$kickoff_abs"
      [ -n "$hints_block" ] && printf '%s' "$hints_block"
      cat "$tmp_tail"
    } > "$prompt_path"
  fi
  rm -f "$tmp_head" "$tmp_tail"

  local lane="${MO_SPEC_AUTHOR_LANE:-glm}"
  echo "[mini-ork] spec-author epic=$epic iter=$iter (model=$lane)" >&2

  local scripts_dir="${AGENT_SCRIPTS_DIR:-$MINI_ORK_ROOT/lib/providers}"
  local env_script=""
  case "$lane" in
    sonnet|opus) env_script="" ;;
    *)
      env_script="$scripts_dir/cl_${lane}.sh"
      if [ ! -f "$env_script" ]; then
        echo "[mini-ork] spec-author: env script missing for lane=$lane → $env_script" >&2
        return 5
      fi
      ;;
  esac

  _spec_author_invoke_claude() {
    local _lane="$1" _env_script="$2"
    local _budget_flag=()
    mo_emit_budget_flag _budget_flag "$_lane" "${MO_SPEC_AUTHOR_BUDGET_USD:-0.80}"
    (
      set -uo pipefail
      if [ -n "$_env_script" ]; then
        # shellcheck disable=SC1090
        source "$_env_script"
      fi
      export CLAUDE_CODE_EFFORT_LEVEL="${MO_SPEC_AUTHOR_EFFORT:-medium}"
      export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${MO_SPEC_AUTHOR_MAX_OUTPUT_TOKENS:-16000}"
      cd "$worktree" || exit 1
      local _TO="${MO_SPEC_AUTHOR_TIMEOUT_SEC:-1200}"
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
  }
  _spec_author_invoke_claude "$lane" "$env_script"
  local rc=$?

  # Fallback lane on infra failure.
  if [ "$rc" -ne 0 ] && [ -n "${MO_SPEC_AUTHOR_FALLBACK_LANE:-}" ] \
     && [ "${MO_SPEC_AUTHOR_FALLBACK_LANE}" != "$lane" ]; then
    local fb_lane="${MO_SPEC_AUTHOR_FALLBACK_LANE}"
    local fb_env_script="$scripts_dir/cl_${fb_lane}.sh"
    if [ -f "$fb_env_script" ]; then
      echo "[mini-ork] spec-author: primary lane=$lane crashed (rc=$rc), retrying with fallback lane=$fb_lane" >&2
      mv "$log_path" "${log_path}.${lane}-failed" 2>/dev/null || true
      _spec_author_invoke_claude "$fb_lane" "$fb_env_script"
      rc=$?
      if [ "$rc" -eq 0 ]; then
        echo "[mini-ork] spec-author: fallback lane=$fb_lane succeeded" >&2
      fi
    fi
  fi

  if [ "$rc" -ne 0 ]; then
    echo "[mini-ork] spec-author: claude exit=$rc — see $log_path" >&2
  fi

  # T1: emit cache row on success.
  if [ "$rc" -eq 0 ] && [ "${MO_SKIP_CACHE:-0}" -ne 1 ]; then
    local cached_artifact=""
    local outcome_status="success"
    local skipped
    skipped=$(grep -oE 'SPEC_SKIPPED: [^"]+' "$log_path" 2>/dev/null | head -1)
    if [ -n "$skipped" ]; then
      cached_artifact="$iter_dir/spec-skipped.marker"
      printf '%s\n' "$skipped" > "$cached_artifact"
    else
      local found
      found=$(mo_spec_exists "$epic" "$worktree")
      if [ -n "$found" ]; then
        cached_artifact="$iter_dir/$(basename "$found")"
        cp "$found" "$cached_artifact"
      fi
    fi
    if [ -n "$cached_artifact" ]; then
      read -r cost turns dur <<< "$(mo_cache_costline_from_log "$log_path")"
      mo_cache_emit spec-author "$epic" "$iter" "$cache_hash" "$outcome_status" \
        "$cached_artifact" "$log_path" "$cost" "$turns" "$dur"
    fi
  fi
  return "$rc"
}

# Returns:
#   "WRITTEN <path>"  — author wrote a spec
#   "SKIPPED <reason>" — author declared BE-only
#   "MISSING"          — neither marker found AND no spec file found
mo_spec_author_outcome() {
  local epic="$1" worktree="$2" iter="$3"
  local log="$(mo_run_dir "$epic")/iter-$iter/spec-author.log"

  if [ -f "$log" ]; then
    local skipped
    skipped=$(grep -oE 'SPEC_SKIPPED: [^"]+' "$log" | head -1)
    if [ -n "$skipped" ]; then
      echo "SKIPPED ${skipped#SPEC_SKIPPED: }"
      return
    fi
  fi

  local found
  found=$(mo_spec_exists "$epic" "$worktree")
  if [ -n "$found" ]; then
    echo "WRITTEN $found"
    return
  fi

  echo "MISSING"
}
