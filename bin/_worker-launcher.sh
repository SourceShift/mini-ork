#!/usr/bin/env bash
# Per-track nohup'd worker launcher.
#
# Direct claude invocation — bypasses any orchestrator scaffolding that has
# a `set -e` short-circuit when run outside the main dispatcher.
# Re-implements just what is needed: source the agent's env script,
# install scope-sentinel, run claude with the kickoff as prompt.
#
# Note: this is a real script, NOT an inline heredoc. nohup'ing inline
# heredocs silently dies.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

: "${MO_EPIC:?}"
: "${MO_AGENT:?}"
: "${MO_WORKTREE:?}"
: "${MO_RUN_DIR:?}"
: "${MO_ITER:?}"
: "${MINI_ORK_HOME:?}"
MO_FEEDBACK="${MO_FEEDBACK:-}"
MO_JOB="${MO_JOB:-unknown-job}"
# Resume support — if set, claude will be invoked with --resume <id> to
# continue a prior worker session that hit gtimeout (exit 124/137).
MO_RESUME_SESSION_ID="${MO_RESUME_SESSION_ID:-}"

REPO_ROOT="${REPO_ROOT:-$(cd "$MINI_ORK_HOME/.." && pwd)}"
ITER_DIR="$MO_RUN_DIR/iter-$MO_ITER"
mkdir -p "$ITER_DIR"

echo "=== worker epic=$MO_EPIC iter=$MO_ITER agent=$MO_AGENT ==="
echo "    started: $(date -u +%FT%TZ)"
echo "    worktree: $MO_WORKTREE"
echo "    feedback: ${MO_FEEDBACK:-(fresh kickoff)}"
if [ -n "$MO_RESUME_SESSION_ID" ]; then
  echo "    RESUME mode: --resume $MO_RESUME_SESSION_ID (continuing prior timed-out session)"
fi
echo "==========================================================="

# ─── Resolve env script per agent ───────────────────────────────────────
# Provider scripts in MINI_ORK_ROOT/lib/providers; override via AGENT_SCRIPTS_DIR
SCRIPTS_DIR="${AGENT_SCRIPTS_DIR:-$MINI_ORK_ROOT/lib/providers}"
_DS_FALLBACK="${MO_DEEPSEEK_FALLBACK_LANE:-glm}"
case "$MO_AGENT" in
  deepseek) MO_AGENT="$_DS_FALLBACK"; ENV_SCRIPT="$SCRIPTS_DIR/cl_${_DS_FALLBACK}.sh" ;;
  glm)      ENV_SCRIPT="$SCRIPTS_DIR/cl_glm.sh" ;;
  kimi)     ENV_SCRIPT="$SCRIPTS_DIR/cl_kimi.sh" ;;
  minimax)  ENV_SCRIPT="$SCRIPTS_DIR/cl_minimax.sh" ;;
  sonnet|opus) ENV_SCRIPT="" ;;
  *) echo "FATAL: unknown agent: $MO_AGENT" >&2; exit 2 ;;
esac
if [ -n "$ENV_SCRIPT" ] && [ ! -f "$ENV_SCRIPT" ]; then
  echo "FATAL: env script missing: $ENV_SCRIPT" >&2
  exit 3
fi

# ─── Resolve kickoff path from state.db ─────────────────────────────────
STATE_DB="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"
KICKOFF_REL=$(sqlite3 "$STATE_DB" \
  "SELECT kickoff_path FROM epics WHERE id='$MO_EPIC';" 2>/dev/null)
if [ -z "$KICKOFF_REL" ]; then
  echo "FATAL: no kickoff_path in state.db for epic $MO_EPIC" >&2
  exit 4
fi
KICKOFF_ABS="$REPO_ROOT/$KICKOFF_REL"
[ -f "$KICKOFF_ABS" ] || { echo "FATAL: kickoff missing: $KICKOFF_ABS" >&2; exit 5; }
echo "    kickoff: $KICKOFF_REL"

# ─── Resolve config dirs ─────────────────────────────────────────────────
MINI_ORK_HOME="${MINI_ORK_HOME:-.mini-ork}"
SCOPE_FILE="${MINI_ORK_SCOPE_FILE:-$MINI_ORK_HOME/config/scope-patterns.yaml}"
AGENTS_FILE="${MINI_ORK_AGENTS_FILE:-$MINI_ORK_HOME/config/agents.yaml}"
INBOX_DIR="$MINI_ORK_HOME/INBOX"
mkdir -p "$INBOX_DIR"

# ─── Install scope-sentinel pre-commit hook (best-effort) ────────────────
SCOPE_PATTERNS=$(awk -v id="$MO_EPIC" '
  /^epics:/ { in_epics = 1; next }
  in_epics && $0 ~ "^  " id ":" { in_epic = 1; next }
  in_epic && /^    patterns:/ { in_pat = 1; next }
  in_pat && /^      - / {
    sub(/^      - /, "")
    gsub(/^"|"$/, "")
    print
    next
  }
  in_pat && /^    [a-z]/ { in_pat = 0 }
  in_epic && /^  [A-Za-z]/ { in_epic = 0; in_pat = 0 }
' "$SCOPE_FILE" 2>/dev/null)

if [ -n "$SCOPE_PATTERNS" ] && [ -x "$MINI_ORK_HOME/hooks/install-scope-sentinel.sh" ]; then
  TMP_SCOPE=$(mktemp)
  printf '%s\n' "$SCOPE_PATTERNS" > "$TMP_SCOPE"
  bash "$MINI_ORK_HOME/hooks/install-scope-sentinel.sh" \
    "$MO_WORKTREE" "$MO_EPIC" "$TMP_SCOPE" 2>&1 | sed 's/^/   /' || true
  rm -f "$TMP_SCOPE"
fi

# ─── Build worker prompt ────────────────────────────────────────────────
KICKOFF_BODY=$(cat "$KICKOFF_ABS")

PROMPT_FILE="$ITER_DIR/prompt.md"
{
  echo "# Worker — Epic $MO_EPIC, iter $MO_ITER"
  echo
  echo "**Worktree:** \`$MO_WORKTREE\` (you are already cd'd here)"
  echo "**Agent:** $MO_AGENT"
  echo
  echo "## Required reading (in order)"
  echo
  echo "1. \`$KICKOFF_REL\` — your kickoff handoff (full content reproduced below)"
  echo "2. \`${MINI_ORK_HOME}/AGENT_SCOPE_CARD.md\` — mini-ork worker rules"
  echo "3. \`CLAUDE.md\` — project rules"
  if [ -n "${MO_PLAN_FILE:-}" ] && [ -f "$MO_PLAN_FILE" ]; then
    echo "4. **PRE-COMPUTED PLAN below** (\`$MO_PLAN_FILE\`) — read FIRST and follow it. The pre-planner has already analyzed the codebase for you. Skip rediscovery."
  fi
  echo

  if [ -n "${MO_PLAN_FILE:-}" ] && [ -f "$MO_PLAN_FILE" ]; then
    echo "<plan>"
    cat "$MO_PLAN_FILE"
    echo "</plan>"
    echo
  fi

  echo "## SCOPE (HARD — pre-commit hook will REJECT out-of-scope edits)"
  echo
  echo "You may ONLY create / edit / delete files matching these patterns:"
  echo
  echo '```'
  printf '%s\n' "$SCOPE_PATTERNS"
  echo '```'
  echo
  echo "If you need to touch another path, create a scope-question note via:"
  echo "  mktemp \"\$REPO_ROOT/${MINI_ORK_HOME}/INBOX/$MO_EPIC-scope-question-\$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.md\""
  echo "then write your reason into that file. Do NOT bypass scope_globs."
  echo

  if declare -F mo_format_denylist_for_kickoff >/dev/null 2>&1 && [ -n "${MO_RUN_DIR:-}" ]; then
    mo_format_denylist_for_kickoff "$MO_RUN_DIR" 2>/dev/null || true
  elif [ -n "${MO_RUN_DIR:-}" ] && [ -f "$MO_RUN_DIR/scope-manifest.json" ]; then
    _deny_count=$(jq -r '.denylist | length' "$MO_RUN_DIR/scope-manifest.json" 2>/dev/null || echo 0)
    if [ "${_deny_count:-0}" -gt 0 ]; then
      echo "## You do NOT have access to (HARD denylist)"
      echo
      echo "These paths are explicitly OUT OF YOUR CAPABILITY for this run."
      echo "Treat them as if they do not exist for you:"
      echo
      echo '```'
      jq -r '.denylist | .[]' "$MO_RUN_DIR/scope-manifest.json"
      echo '```'
      echo
    fi
  fi
  if [ -n "$MO_FEEDBACK" ] && [ -f "$MO_FEEDBACK" ]; then
    echo "## Reviewer feedback from previous iteration"
    echo
    cat "$MO_FEEDBACK"
    echo
  fi
  echo "## Hard rules (non-negotiable)"
  echo
  echo "- Touch ONLY files in scope patterns above."
  echo "- Idempotent migrations (\`IF NOT EXISTS\`)."
  echo "- Before writing code that uses any library/SDK, fetch its current docs via the \`ctx7\` CLI: \`npx ctx7@latest docs /org/project '<your question>'\`."
  echo "- No fallback logic for SDK / sandbox failures — fail loudly."
  echo "- Commit per logical unit. Conventional Commits format."
  echo "- Do NOT push. Do NOT create PRs."
  echo
  echo "## Commit cadence (CRITICAL — your session may be killed at any time)"
  echo
  echo "- After EACH completed file group, run \`git add <files> && git commit -m 'feat(...)'\`."
  echo "- Do NOT batch all commits to the end — if you hit the timeout, ALL uncommitted work is LOST."
  echo "- Aim for 1 commit every 5-10 minutes of work. Smaller commits > no commits."
  echo
  echo "## Discover before write"
  echo
  echo "- Before creating ANY new file, \`ls\` the parent directory to learn the existing naming convention."
  echo "- Before editing ANY existing file, \`Read\` it first to understand its structure."
  echo "- Match existing patterns in the codebase — do NOT invent new directory structures."
  echo
  echo "## DoD self-check (run before final commit)"
  echo
  echo "Re-read the 'Definition of Done' from the kickoff. For each item:"
  echo "- Run \`ls\`, \`grep\`, or \`git log --oneline\` to objectively verify it."
  echo "- If an item is NOT satisfied, either finish it or note it as 'open' in the report."
  echo "- The reviewer will run these same checks — do not claim done without evidence."
  echo
  echo "## When done"
  echo
  echo "1. \`git diff --staged --name-only\` — verify all paths in scope."
  echo "2. \`git commit\` (conventional commit format)."
  echo "3. Write iter-$MO_ITER report at \`${MINI_ORK_HOME}/dispatch-$MO_JOB/track-${MO_EPIC,,}-iter-$MO_ITER-report.md\` summarizing what landed + open questions."
  echo "4. STOP. Reviewer takes over from here."
  echo
  echo "## When pre-commit hooks fail (baseline-rot protocol)"
  echo
  echo "If \`git commit\` fails because pre-commit errors are in files you did NOT modify:"
  echo "1. Confirm errors are in untouched files via \`git diff --name-only main..HEAD\` vs the error file paths."
  echo "2. File a baseline-rot note:"
  echo "   mktemp \"\$REPO_ROOT/${MINI_ORK_HOME}/INBOX/$MO_EPIC-baseline-rot-\$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.md\""
  echo "3. STOP. Do NOT use --no-verify. The orchestrator picks up INBOX notes."
  echo
  echo "Errors in files you DID modify are real — fix them, then commit normally."
  echo
  echo "---"
  echo
  echo "## Kickoff content"
  echo
  echo "$KICKOFF_BODY"
} > "$PROMPT_FILE"

# ─── ContextNest worker context (PR-3 + prefetch wiring; best-effort) ────
# Two complementary, bounded, never-blocking augmentations appended to the
# worker prompt so EVERY recipe's workers get CN context — not just the 3
# code-fix prompts that hard-code MO_CN_PREFETCH_DIR.
#
#   (1) Implementer role pack — synchronous, inlined now. This is the missing
#       implementer call site (previously context_role_pack_md was only ever
#       called for the planner).
#   (2) Generic "ContextNest prefetch" read instruction — points the worker at
#       MO_CN_PREFETCH_DIR (written async by hooks/subagent-prefetch.sh) so the
#       per-session prefetch file is consumed by all recipes, not only code-fix.
if [ "${MO_USE_ROLE_PACKS:-1}" = "1" ] && [ "${MO_DISABLE_CN:-0}" != "1" ] \
   && [ -f "$MINI_ORK_ROOT/lib/context_role_packs.sh" ]; then
  # shellcheck source=../lib/context_role_packs.sh
  source "$MINI_ORK_ROOT/lib/context_role_packs.sh" 2>/dev/null || true
  if declare -f context_role_pack_md >/dev/null 2>&1; then
    _cn_impl_pack="$(context_role_pack_md implementer "$KICKOFF_ABS" "" 2>/dev/null || true)"
    if [ -n "$_cn_impl_pack" ]; then
      {
        echo
        echo "## ContextNest implementer pack (cross-session substrate — advisory)"
        echo
        echo "Prior editors of these files, adjacent deliveries, and graph"
        echo "neighbours. Verify against live code before acting on any atom."
        echo
        printf '%s\n' "$_cn_impl_pack"
      } >> "$PROMPT_FILE"
    fi
  fi
fi
if [ -n "${MO_CN_PREFETCH_DIR:-}" ]; then
  {
    echo
    echo "## Step 0 — ContextNest prefetch (read if present)"
    echo
    echo "Before planning your edits, check for a prefetched cross-session"
    echo "context file. If \`$MO_CN_PREFETCH_DIR\` exists and contains any"
    echo "\`*.md\` file, read it first — it holds semantic atoms, inbox items,"
    echo "and recent feature deliveries relevant to this task. Treat it as"
    echo "advisory memory: verify against live code before acting."
  } >> "$PROMPT_FILE"
fi

PROMPT_BYTES=$(wc -c < "$PROMPT_FILE" | xargs)
echo "    prompt: $PROMPT_FILE ($PROMPT_BYTES bytes)"

# ─── Source provider env + run claude in subshell ───────────────────────
echo "----------------------------------------------------"
(
  set +eu
  if [ -n "$ENV_SCRIPT" ]; then
    # shellcheck disable=SC1090
    source "$ENV_SCRIPT"
    echo "    env-loaded: $ENV_SCRIPT"
  fi
  cd "$MO_WORKTREE" || { echo "FATAL: cd failed" >&2; exit 1; }

  # Pick a timeout binary
  TIMEOUT_BIN=""
  command -v gtimeout >/dev/null 2>&1 && TIMEOUT_BIN=gtimeout
  [ -z "$TIMEOUT_BIN" ] && command -v timeout >/dev/null 2>&1 && TIMEOUT_BIN=timeout

  # Worker timeout cap (default 30 min; override MO_WORKER_TIMEOUT_MIN)
  TM="${MO_WORKER_TIMEOUT_MIN:-30}"

  export CLAUDE_CODE_EFFORT_LEVEL="${MO_WORKER_EFFORT_LEVEL:-xhigh}"

  PROMPT_TEXT="$(cat "$PROMPT_FILE")"

  # Resume mode
  RESUME_FLAGS=()
  if [ -n "$MO_RESUME_SESSION_ID" ]; then
    RESUME_FLAGS=(--resume "$MO_RESUME_SESSION_ID")
    PROMPT_TEXT="Continue from where you left off. Your prior session timed out at the ${TM}-minute wall before all in-scope files were complete. Re-read your own commits with \`git log --oneline main..HEAD\` to identify what's already done; do NOT redo completed work. Finish only the remaining files in your kickoff scope, then commit and STOP."
  fi

  echo "    claude: nice -n 19 ${TIMEOUT_BIN:-(no-timeout)} ${TM}m claude -p ${RESUME_FLAGS[*]:-} --allow-dangerously-skip-permissions"
  echo "----------------------------------------------------"

  # ─── Transport branch: SDK streaming vs CLI ─────────────────────────
  USE_SDK_LAUNCHER=0
  if [ "${MO_USE_SDK_LAUNCHER:-}" = "1" ]; then
    USE_SDK_LAUNCHER=1
    echo "    sdk-launcher resolution: env MO_USE_SDK_LAUNCHER=1 (forced)"
  elif [ "${MO_USE_SDK_LAUNCHER:-}" = "0" ]; then
    USE_SDK_LAUNCHER=0
    echo "    sdk-launcher resolution: env MO_USE_SDK_LAUNCHER=0 (forced CLI)"
  else
    ALLOWLIST="$REPO_ROOT/${MINI_ORK_HOME}/config/sdk-launcher.allowlist"
    if [ -f "$ALLOWLIST" ]; then
      while IFS= read -r _line; do
        _line="${_line%%#*}"
        _line="$(echo "$_line" | xargs)"
        [ -z "$_line" ] && continue
        case "$MO_EPIC" in
          $_line)
            USE_SDK_LAUNCHER=1
            echo "    sdk-launcher resolution: allowlist matched pattern '$_line'"
            break
            ;;
        esac
      done < "$ALLOWLIST"
      if [ "$USE_SDK_LAUNCHER" = "0" ]; then
        echo "    sdk-launcher resolution: not on allowlist → CLI"
      fi
    else
      echo "    sdk-launcher resolution: no allowlist file → CLI default"
    fi
  fi

  if [ "$USE_SDK_LAUNCHER" = "1" ]; then
    if ! command -v tsx >/dev/null 2>&1; then
      echo "FATAL: MO_USE_SDK_LAUNCHER=1 but tsx is not installed" >&2
      echo 6 > "$ITER_DIR/worker.exit"
      exit 6
    fi
    SDK_LAUNCHER="$MINI_ORK_ROOT/lib/_worker-sdk-launcher.ts"
    [ ! -f "$SDK_LAUNCHER" ] && SDK_LAUNCHER="$REPO_ROOT/${MINI_ORK_HOME}/lib/_worker-sdk-launcher.ts"
    STEER_FILE="$ITER_DIR/STEER.jsonl"
    HEARTBEAT_FILE="$ITER_DIR/HEARTBEAT"
    : > "$STEER_FILE"
    echo "    transport: SDK streaming (tsx $SDK_LAUNCHER)"
    echo "    steer: $STEER_FILE  heartbeat: $HEARTBEAT_FILE"
    RESUME_ARGS=()
    if [ -n "$MO_RESUME_SESSION_ID" ]; then
      RESUME_ARGS=(--resume "$MO_RESUME_SESSION_ID")
    fi
    BUDGET_ARGS=()
    if [ -n "${MO_WORKER_BUDGET_USD:-}" ]; then
      BUDGET_ARGS=(--max-budget-usd "$MO_WORKER_BUDGET_USD")
    fi
    if [ -n "$TIMEOUT_BIN" ]; then
      nice -n 19 "$TIMEOUT_BIN" --kill-after=30s --signal=TERM "${TM}m" \
        tsx "$SDK_LAUNCHER" \
          --prompt-file "$PROMPT_FILE" \
          --worktree "$MO_WORKTREE" \
          --add-dir "$REPO_ROOT" \
          --log "$ITER_DIR/worker.log" \
          --steer "$STEER_FILE" \
          --heartbeat "$HEARTBEAT_FILE" \
          --max-turns "${MO_MAX_TURNS:-60}" \
          "${RESUME_ARGS[@]}" \
          "${BUDGET_ARGS[@]}" \
          2> "$ITER_DIR/worker.err"
    else
      nice -n 19 \
        tsx "$SDK_LAUNCHER" \
          --prompt-file "$PROMPT_FILE" \
          --worktree "$MO_WORKTREE" \
          --add-dir "$REPO_ROOT" \
          --log "$ITER_DIR/worker.log" \
          --steer "$STEER_FILE" \
          --heartbeat "$HEARTBEAT_FILE" \
          --max-turns "${MO_MAX_TURNS:-60}" \
          "${RESUME_ARGS[@]}" \
          "${BUDGET_ARGS[@]}" \
          2> "$ITER_DIR/worker.err"
    fi
    echo $? > "$ITER_DIR/worker.exit"
  else
    # ─── CLI path (default) ────────────────────────────────────────────
    CACHE_FLAGS=()
    local_lane_helpers="$MINI_ORK_ROOT/lib/lane-helpers.sh"
    if [ -f "$local_lane_helpers" ]; then
      # shellcheck disable=SC1090
      source "$local_lane_helpers" 2>/dev/null && mo_emit_cache_flags CACHE_FLAGS 2>/dev/null || true
    fi
    if [ -n "$TIMEOUT_BIN" ]; then
      nice -n 19 "$TIMEOUT_BIN" --kill-after=30s "${TM}m" \
        claude -p \
          "${CACHE_FLAGS[@]}" \
          "${RESUME_FLAGS[@]}" \
          --allow-dangerously-skip-permissions \
          --dangerously-skip-permissions \
          --add-dir "$REPO_ROOT" \
          --output-format stream-json \
          --include-partial-messages \
          --verbose \
          "$PROMPT_TEXT" \
          > "$ITER_DIR/worker.log" 2> "$ITER_DIR/worker.err"
    else
      nice -n 19 \
        claude -p \
          "${CACHE_FLAGS[@]}" \
          "${RESUME_FLAGS[@]}" \
          --allow-dangerously-skip-permissions \
          --dangerously-skip-permissions \
          --add-dir "$REPO_ROOT" \
          --output-format stream-json \
          --include-partial-messages \
          --verbose \
          "$PROMPT_TEXT" \
          > "$ITER_DIR/worker.log" 2> "$ITER_DIR/worker.err"
    fi
    echo $? > "$ITER_DIR/worker.exit"
  fi
)

WORKER_RC=$(cat "$ITER_DIR/worker.exit" 2>/dev/null || echo 99)
LOG_BYTES=$(wc -c < "$ITER_DIR/worker.log" 2>/dev/null || echo 0)
ERR_BYTES=$(wc -c < "$ITER_DIR/worker.err" 2>/dev/null || echo 0)

# ─── Auto-rescue uncommitted files ─────────────────────────────────────
# Worker may have exited (or been killed) without committing. Auto-stage
# and commit any in-scope changes so the reviewer sees actual work.
(
  cd "$MO_WORKTREE" || exit 0
  _RESCUE_OK=0
  if [ "${MO_SKIP_AUTO_COMMIT_RESCUE:-0}" != "1" ]; then
    case "$WORKER_RC" in
      0|124|137) _RESCUE_OK=1 ;;
      *)
        if [ "${LOG_BYTES:-0}" -gt 102400 ] && ! git diff --quiet 2>/dev/null; then
          _RESCUE_OK=1
        fi
        ;;
    esac
  fi
  if [ "$_RESCUE_OK" = "1" ]; then
    git add -A \
      ":!node_modules" \
      ":!${MINI_ORK_HOME}/runs" \
      ":!${MINI_ORK_HOME}/state.db*" \
      2>/dev/null || true
    if ! git diff --cached --quiet 2>/dev/null; then
      _epic_lower=$(echo "$MO_EPIC" | tr '[:upper:]' '[:lower:]')
      case "$WORKER_RC" in
        0)   _RESCUE_SUBJ="feat(${_epic_lower}): worker iter ${MO_ITER} — auto-rescue of uncommitted files" ;;
        124) _RESCUE_SUBJ="feat(${_epic_lower}): worker iter ${MO_ITER} — auto-rescue after timeout (rc=124)" ;;
        137) _RESCUE_SUBJ="feat(${_epic_lower}): worker iter ${MO_ITER} — auto-rescue after SIGKILL (rc=137)" ;;
        *)   _RESCUE_SUBJ="feat(${_epic_lower}): worker iter ${MO_ITER} — auto-rescue after crash (rc=${WORKER_RC})" ;;
      esac
      git commit -m "$_RESCUE_SUBJ" \
        --no-verify > "$ITER_DIR/auto-rescue-commit.log" 2>&1 || true
      echo "[worker-launcher] auto-rescued $(git rev-parse --short HEAD) — worker exit=$WORKER_RC, ${LOG_BYTES}B log"
    fi
  fi
  git diff main..HEAD --stat > "$ITER_DIR/diff.stat" 2>/dev/null || true
  git log main..HEAD --oneline > "$ITER_DIR/commits.log" 2>/dev/null || true
)

# ─── Capture session metadata for resume support ────────────────────────
SESSION_ID=$(jq -r 'select(.session_id) | .session_id' "$ITER_DIR/worker.log" 2>/dev/null | head -1 || true)
if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "null" ]; then
  echo "$SESSION_ID" > "$ITER_DIR/worker.session_id"
fi
shasum -a 256 "$KICKOFF_ABS" 2>/dev/null | awk '{print $1}' > "$ITER_DIR/worker.kickoff_hash" || true

PRIOR_ITER=$((MO_ITER - 1))
PRIOR_COUNT=0
if [ -n "$MO_RESUME_SESSION_ID" ] && [ -f "$MO_RUN_DIR/iter-$PRIOR_ITER/worker.resume_count" ]; then
  PRIOR_COUNT=$(cat "$MO_RUN_DIR/iter-$PRIOR_ITER/worker.resume_count" 2>/dev/null || echo 0)
fi
if [ -n "$MO_RESUME_SESSION_ID" ]; then
  echo $((PRIOR_COUNT + 1)) > "$ITER_DIR/worker.resume_count"
else
  echo 0 > "$ITER_DIR/worker.resume_count"
fi

echo "----------------------------------------------------"
echo "=== worker epic=$MO_EPIC iter=$MO_ITER ended at $(date -u +%FT%TZ) ==="
echo "    rc=$WORKER_RC log=$LOG_BYTES err=$ERR_BYTES"
echo "    commits: $(wc -l < "$ITER_DIR/commits.log" 2>/dev/null || echo 0)"
echo "    session_id: ${SESSION_ID:-(not captured)}"
if [ -n "$MO_RESUME_SESSION_ID" ]; then
  echo "    resume_count: $((PRIOR_COUNT + 1)) (was --resume $MO_RESUME_SESSION_ID)"
fi
exit "$WORKER_RC"
