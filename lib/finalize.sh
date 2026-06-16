#!/usr/bin/env bash
# finalize.sh — runs after all epics APPROVE'd.
# Reads run dirs + commit history, emits a job completion report,
# and (default ON) auto-merges APPROVE branches into main.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Auto-merge wiring. Source-time only; the actual call happens inside mo_finalize.
# shellcheck disable=SC1091
. "$MINI_ORK_ROOT/lib/auto-merge.sh"
# PR-create wiring (E3). Disabled by default; opt-in via MO_OPEN_PR=1.
# shellcheck disable=SC1091
. "$MINI_ORK_ROOT/lib/pr-create.sh"

mo_finalize() {
  : "${REPO_ROOT:?}"
  : "${MINI_ORCH_DIR:?}"
  : "${JOB_ID:?}"

  local agentflow_dir="${MINI_ORK_HOME:-.mini-ork}"
  local state_db="${MINI_ORK_DB:-${agentflow_dir}/state.db}"

  local job_run_dir="$MINI_ORCH_DIR/runs/$JOB_ID"
  local report="$job_run_dir/COMPLETION_REPORT.md"
  mkdir -p "$job_run_dir"

  {
    echo "# Mini-ork completion report — $JOB_ID"
    echo
    echo "Generated: $(date -u +%FT%TZ)"
    echo
    echo "## Epics"
    echo
    for epic_dir in "$job_run_dir"/*/; do
      [ -d "$epic_dir" ] || continue
      local epic
      epic=$(basename "$epic_dir")
      [[ "$epic" == "$(basename "$job_run_dir")" ]] && continue
      echo "### $epic"
      echo
      # Find last iter WITH a verdict.json — skip phantom iter dirs
      local last_iter=""
      for _d in $(ls -d "$epic_dir"iter-*/ 2>/dev/null | sort -V -r); do
        if [ -f "${_d}verdict.json" ]; then
          last_iter=$(echo "$_d" | sed -E 's|.*iter-([0-9]+)/?$|\1|')
          break
        fi
      done
      local verdict="UNKNOWN"
      if [ -n "$last_iter" ] && [ -f "$epic_dir/iter-$last_iter/verdict.json" ]; then
        verdict=$(jq -r '.verdict // "UNKNOWN"' "$epic_dir/iter-$last_iter/verdict.json")
      fi
      echo "- Final verdict: **$verdict** (iter-${last_iter:-none})"
      local kickoff_path
      kickoff_path=$(sqlite3 "$state_db" "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
      local branch
      branch=$(grep -E '^>?[[:space:]]*\*\*Branch:\*\*' "$REPO_ROOT/$kickoff_path" 2>/dev/null | head -1 | sed -E 's/^[^`]*`([^`]+)`.*/\1/')
      echo "- Branch: \`$branch\`"
      if [ -n "$branch" ] && git -C "$REPO_ROOT" rev-parse "$branch" >/dev/null 2>&1; then
        echo "- Commits ahead of main:"
        git -C "$REPO_ROOT" log --oneline "main..$branch" 2>/dev/null | sed 's|^|    |' | head -30
      fi
      echo
    done
    echo
    echo "## Cache reuse this run"
    echo
    local cache_rows
    cache_rows=$(sqlite3 "$state_db" "
      SELECT COUNT(*) FROM mini_orch_sessions
      WHERE job_id = '$JOB_ID' AND reused_count > 0;
    " 2>/dev/null)
    if [ -z "$cache_rows" ] || [ "$cache_rows" = "0" ]; then
      echo "_No cache hits this run (cold cache or first dispatch)._"
    else
      local saved_total
      saved_total=$(sqlite3 "$state_db" "
        SELECT printf('%.2f', COALESCE(SUM(cost_usd * reused_count), 0))
        FROM mini_orch_sessions
        WHERE job_id = '$JOB_ID' AND reused_count > 0;
      " 2>/dev/null)
      echo "**Total dollars saved by cache hits: \$${saved_total}**"
      echo
      echo '```'
      mo_cache_run_summary "$JOB_ID"
      echo '```'
      echo
      echo "Replay cache state:"
      echo
      echo '```'
      echo "  mini-ork replay inspect $JOB_ID"
      echo "  mini-ork replay stats"
      echo '```'
    fi
    echo
    echo "## Cost trace (per-stage breakdown)"
    echo
    printf '```\n'
    printf '%-22s %-22s %-10s %5s %12s\n' "epic/iter/stage" "model" "result" "turns" "cost_usd"
    printf '%-22s %-22s %-10s %5s %12s\n' "----------------------" "----------------------" "----------" "-----" "------------"
    local _bcap_count=0 _err_count=0 _grand_cost=0
    for epic_dir in "$job_run_dir"/*/; do
      [ -d "$epic_dir" ] || continue
      local epic_name
      epic_name=$(basename "$epic_dir")
      [[ "$epic_name" == "$(basename "$job_run_dir")" ]] && continue
      for iter_dir in "$epic_dir"iter-*/; do
        [ -d "$iter_dir" ] || continue
        local iter_n
        iter_n=$(basename "$iter_dir" | sed 's/iter-//')
        for stage_log in "$iter_dir"*.log; do
          [ -f "$stage_log" ] || continue
          local stage
          stage=$(basename "$stage_log" .log)
          case "$stage" in commits|merge|rebase|preflight) continue ;; esac
          local result_line
          result_line=$(grep '"type":"result"' "$stage_log" 2>/dev/null | tail -1)
          [ -z "$result_line" ] && continue
          local cost turns subtype model
          cost=$(echo "$result_line" | jq -r '.total_cost_usd // 0' 2>/dev/null)
          turns=$(echo "$result_line" | jq -r '.num_turns // 0' 2>/dev/null)
          subtype=$(echo "$result_line" | jq -r '.subtype // "?"' 2>/dev/null)
          model=$(grep -m1 '"subtype":"init"' "$stage_log" 2>/dev/null | jq -r '.model // empty' 2>/dev/null | sed 's/\[.*\]//')
          [ -z "$model" ] && model="?"
          local _cost_fmt
          _cost_fmt=$(awk -v c="$cost" 'BEGIN { printf "%.4f", c+0 }')
          printf '%-22s %-22s %-10s %5s %12s\n' \
            "$epic_name/i$iter_n/$stage" "$model" "$subtype" "$turns" "$_cost_fmt"
          _grand_cost=$(awk -v g="$_grand_cost" -v c="$cost" 'BEGIN { printf "%.4f", g + c }')
          case "$subtype" in
            success) ;;
            error_max_budget_usd) _bcap_count=$((_bcap_count + 1)) ;;
            error_*) _err_count=$((_err_count + 1)) ;;
          esac
        done
      done
    done
    printf '%-22s %-22s %-10s %5s %12s\n' "----------------------" "----------------------" "----------" "-----" "------------"
    printf '%-22s %-22s %-10s %5s %12s\n' "TOTAL" "" "" "" "$_grand_cost"
    printf '```\n'
    if [ "$_bcap_count" -gt 0 ] || [ "$_err_count" -gt 0 ]; then
      echo
      echo "**Stage failures detected:**"
      [ "$_bcap_count" -gt 0 ] && echo "- Budget-cap (error_max_budget_usd) hits: **$_bcap_count** — raise cap or shorten prompt"
      [ "$_err_count" -gt 0 ]  && echo "- Other stage errors: **$_err_count** — inspect *.log for subtype"
    fi
    echo
    echo "## No-context A/B probe (When Context Hurts, arXiv 2605.04361)"
    echo
    local probe_count=0 control_count=0
    local probe_cost_sum=0 control_cost_sum=0
    local probe_approve=0 probe_reject=0 control_approve=0 control_reject=0
    for epic_dir in "$job_run_dir"/*/; do
      [ -d "$epic_dir" ] || continue
      local epic_name
      epic_name=$(basename "$epic_dir")
      [[ "$epic_name" == "$(basename "$job_run_dir")" ]] && continue
      for iter_dir in "$epic_dir"iter-*/; do
        [ -d "$iter_dir" ] || continue
        local sa_log="$iter_dir/spec-author.log"
        local verdict_file="$iter_dir/verdict.json"
        local sa_cost=0
        if [ -f "$sa_log" ]; then
          sa_cost=$(grep '"type":"result"' "$sa_log" 2>/dev/null | tail -1 | jq -r '.total_cost_usd // 0' 2>/dev/null)
        fi
        local v_status="UNKNOWN"
        [ -f "$verdict_file" ] && v_status=$(jq -r '.verdict // "UNKNOWN"' "$verdict_file" 2>/dev/null)
        if [ -f "$iter_dir/no-context-probe.flag" ]; then
          probe_count=$((probe_count + 1))
          probe_cost_sum=$(awk -v s="$probe_cost_sum" -v c="$sa_cost" 'BEGIN { printf "%.4f", s + c }')
          [ "$v_status" = "APPROVE" ] && probe_approve=$((probe_approve + 1))
          [ "$v_status" = "REQUEST_CHANGES" ] && probe_reject=$((probe_reject + 1))
        else
          control_count=$((control_count + 1))
          control_cost_sum=$(awk -v s="$control_cost_sum" -v c="$sa_cost" 'BEGIN { printf "%.4f", s + c }')
          [ "$v_status" = "APPROVE" ] && control_approve=$((control_approve + 1))
          [ "$v_status" = "REQUEST_CHANGES" ] && control_reject=$((control_reject + 1))
        fi
      done
    done
    if [ "$probe_count" -eq 0 ] && [ "$control_count" -eq 0 ]; then
      echo "_No spec-author iters found in this run._"
    else
      echo '```'
      printf '%-12s %5s %16s %10s %10s\n' "arm" "iters" "spec-author_sum" "approves" "rejects"
      printf '%-12s %5s %16s %10s %10s\n' "no-context" "$probe_count" "$probe_cost_sum" "$probe_approve" "$probe_reject"
      printf '%-12s %5s %16s %10s %10s\n' "control"    "$control_count" "$control_cost_sum" "$control_approve" "$control_reject"
      echo '```'
      echo
      echo "_If no-context approve-rate is comparable to control's, the memory hints aren't pulling weight on this epic-class — consider dropping. If much worse, hints are essential._"
    fi
    echo
    echo "## Next actions"
    echo
    echo "- Review the final commits per branch (\`git log\` lines above)."
    echo "- Open PRs:"
    echo "  \`\`\`"
    for epic_dir in "$job_run_dir"/*/; do
      [ -d "$epic_dir" ] || continue
      local epic
      epic=$(basename "$epic_dir")
      [[ "$epic" == "$(basename "$job_run_dir")" ]] && continue
      local kickoff_path
      kickoff_path=$(sqlite3 "$state_db" "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
      local branch
      branch=$(grep -E '^>?[[:space:]]*\*\*Branch:\*\*' "$REPO_ROOT/$kickoff_path" 2>/dev/null | head -1 | sed -E 's/^[^`]*`([^`]+)`.*/\1/')
      # E3: actually open the PR when MO_OPEN_PR=1, capturing URL into epics.pr_url.
      # Falls back to printing the manual command when disabled / no gh / no token.
      if [ "${MO_OPEN_PR:-0}" = "1" ]; then
        local _pr_url
        _pr_url=$(mo_open_pr "$epic" "$branch" "$REPO_ROOT/$kickoff_path" 2>/dev/null || true)
        if [ -n "$_pr_url" ]; then
          echo "  $epic → $_pr_url"
        else
          echo "  $epic → (PR open skipped; see mini-ork logs)"
        fi
      else
        echo "  gh pr create --base main --head $branch --title \"...\""
      fi
    done
    echo "  \`\`\`"
  } > "$report"

  # Auto-merge APPROVE branches into main. Default ON; opt out with MO_AUTO_MERGE=0.
  if [ "${MO_AUTO_MERGE:-1}" -ne 0 ]; then
    echo
    echo "─────────────────────────────────────────────────────────────────"
    echo " auto-merge phase"
    echo "─────────────────────────────────────────────────────────────────"
    mo_auto_merge || echo "[mini-ork] WARN auto-merge returned non-zero (some epics skipped)" >&2
    {
      echo
      echo "## Auto-merge results"
      echo
      if [ -f "$job_run_dir/merge.log" ]; then
        echo '```'
        cat "$job_run_dir/merge.log"
        echo '```'
      else
        echo "_No merge log emitted._"
      fi
    } >> "$report"
  else
    echo "[mini-ork] auto-merge SKIPPED (MO_AUTO_MERGE=0)" >&2
  fi

  # ─── Cache reuse summary ──────────────────────────────────────────────
  if declare -F mo_aggregate_cache_stats >/dev/null 2>&1; then
    {
      echo
      echo "## Cache reuse this run (prompt cache)"
      echo
      printf '| Epic | Iter | Cache reads | Cache writes | Uncached | Hit rate | $ saved |\n'
      printf '|---|---|---|---|---|---|---|\n'
      local total_read=0 total_creation=0 total_uncached=0
      for epic_dir in "$job_run_dir"/*/; do
        local epic=$(basename "$epic_dir")
        case "$epic" in _*) continue ;; esac
        for iter_dir in "$epic_dir"iter-*/; do
          [ -d "$iter_dir" ] || continue
          mo_aggregate_cache_stats "$iter_dir" 2>/dev/null || continue
          local s="$iter_dir/cache-stats.json"
          [ -f "$s" ] || continue
          local r c u hr saved iter
          iter=$(basename "$iter_dir" | sed 's/iter-//')
          r=$(jq -r '.cache_read_tokens' "$s")
          c=$(jq -r '.cache_creation_tokens' "$s")
          u=$(jq -r '.uncached_input_tokens' "$s")
          hr=$(jq -r '.hit_rate' "$s" | awk '{printf "%.1f%%", $1*100}')
          saved=$(jq -r '.estimated_usd_saved' "$s")
          total_read=$((total_read + r))
          total_creation=$((total_creation + c))
          total_uncached=$((total_uncached + u))
          printf '| %s | %s | %s | %s | %s | %s | $%s |\n' "$epic" "$iter" "$r" "$c" "$u" "$hr" "$saved"
        done
      done
      echo
      local total_saved
      total_saved=$(awk -v r="$total_read" 'BEGIN{printf "%.2f", r * 0.9 * 3 / 1000000}')
      printf '**Totals:** %s cache reads, %s writes, %s uncached input tokens — **~\$%s saved** vs all-uncached.\n' \
        "$total_read" "$total_creation" "$total_uncached" "$total_saved"
    } >> "$report"
  fi

  echo "$report"
}
