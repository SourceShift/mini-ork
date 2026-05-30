#!/usr/bin/env bash
# cleaner.sh — single-shot cleaner agent that runs ON MAIN to fix
# cross-cutting blockers (baseline rot, dual-types trap) detected by
# detective.sh.
#
# Unlike epic workers, the cleaner:
#   - works directly on main (NOT a worktree) via a temporary branch
#   - has a tightly-scoped brief from the detective
#   - skips the iter loop — single attempt, gauntlet-checked, auto-merged
#   - holds an exclusive lock so no auto-merge runs concurrently
#
# Usage:
#   bash cleaner.sh <detective_json> <output_dir>
#
# Returns exit 0 on successful auto-merge, non-zero otherwise.
# Output: <output_dir>/{prompt.md, worker.log, gauntlet/, verdict.json}

set -euo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO_ROOT="${REPO_ROOT:-$(git -C "$MINI_ORK_ROOT" rev-parse --show-toplevel 2>/dev/null || pwd)}"
MINI_ORK_HOME="${MINI_ORK_HOME:-.mini-ork}"
AGENTFLOW_DIR="${AGENTFLOW_DIR:-$REPO_ROOT/$MINI_ORK_HOME}"
SCOPE_CARD_FILE="$AGENTFLOW_DIR/AGENT_SCOPE_CARD.md"
LOCK_FILE="$AGENTFLOW_DIR/locks/cleaner.lock"

DETECTIVE_JSON="${1:-}"
OUTPUT_DIR="${2:-}"

if [ -z "$DETECTIVE_JSON" ] || [ -z "$OUTPUT_DIR" ]; then
  echo "Usage: $0 <detective_json> <output_dir>" >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR" "$(dirname "$LOCK_FILE")"

# Acquire exclusive lock — portable across macOS (no flock) and Linux.
# Use atomic mkdir as the lock (only one process can succeed).
LOCK_DIR="${LOCK_FILE}.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  # Stale lock detection: if PID inside is dead, take over
  if [ -f "$LOCK_DIR/pid" ]; then
    other_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null)
    if [ -n "$other_pid" ] && ! kill -0 "$other_pid" 2>/dev/null; then
      echo "[cleaner] stale lock from dead PID $other_pid — reclaiming" >&2
      rm -rf "$LOCK_DIR"
      mkdir "$LOCK_DIR" 2>/dev/null || {
        echo "[cleaner] race lost reclaiming stale lock" >&2; exit 3;
      }
    else
      echo "[cleaner] lock held by PID $other_pid — refusing to run" >&2
      exit 3
    fi
  else
    echo "[cleaner] lock held (no PID file) — refusing to run" >&2
    exit 3
  fi
fi
echo "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

EPIC_ID=$(jq -r '.epic_id // "unknown"' "$DETECTIVE_JSON")
CLASSIFICATION=$(jq -r '.classification // "unknown"' "$DETECTIVE_JSON")
BRIEF=$(jq -r '.cleaner_brief // ""' "$DETECTIVE_JSON")
RATIONALE=$(jq -r '.rationale // ""' "$DETECTIVE_JSON")

if [ -z "$BRIEF" ]; then
  echo "[cleaner] detective gave no cleaner_brief — refusing to run blind" >&2
  exit 4
fi

TS=$(date -u +'%Y%m%dT%H%M%SZ')
BRANCH="chore/cleaner-${CLASSIFICATION//_/-}-$TS"

echo "[cleaner] === START ===" >&2
echo "[cleaner] triggered by epic=$EPIC_ID classification=$CLASSIFICATION" >&2
echo "[cleaner] branch=$BRANCH" >&2

# ─── 1. Pre-flight: must be on main, working tree clean ───────────────────
cd "$REPO_ROOT"
local_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [ "$local_branch" != "main" ]; then
  echo "[cleaner] ERROR: must run on main (currently on $local_branch)" >&2
  exit 5
fi
# Stash dirty working tree, but track it so we can restore on exit.
# Previous behavior leaked: every cleaner run pushed a stash and forgot it,
# silently piling up the user's uncommitted edits in the stash list (and
# making it look like edits were being reverted by a linter). Now: pop the
# stash back via EXIT trap on every code path.
CLEANER_STASH_CREATED=0
if ! git diff --quiet || ! git diff --staged --quiet; then
  echo "[cleaner] working tree dirty — stashing first (will pop on exit)" >&2
  if git stash push -u -m "cleaner pre-stash $TS" >&2; then
    CLEANER_STASH_CREATED=1
  fi
fi

restore_user_stash() {
  [ "$CLEANER_STASH_CREATED" = "1" ] || return 0
  local ref
  ref=$(git stash list | grep -F "cleaner pre-stash $TS" | head -1 | sed 's/:.*$//')
  if [ -n "$ref" ]; then
    # Switch back to main first if cleaner left us on its branch — pop only
    # makes sense on the same branch the user was on (which was main).
    git checkout main >/dev/null 2>&1 || true
    if git stash pop "$ref" >/dev/null 2>&1; then
      echo "[cleaner] restored user's pre-stash ($ref)" >&2
    else
      echo "[cleaner] WARNING: could not auto-pop $ref (conflict?). Run \`git stash pop $ref\` manually." >&2
    fi
  fi
}
trap 'restore_user_stash; rm -rf "$LOCK_DIR"' EXIT

# ─── 1.5 Fast-path: reuse a recent unmerged cleaner branch if predicate passes
# Cleaner runs cost ~5min + LLM tokens. If a prior cleaner already produced
# a branch that fixes the rot but was blocked from merging by an unrelated
# advisory check (lint/api-smoke), we should merge it rather than re-spawn.
# Predicate = type-check + build pass on the candidate branch (same narrow
# gate used in the main merge step below).
CLEANER_REUSE_LOOKBACK="${CLEANER_REUSE_LOOKBACK:-3}"
RECENT_CANDIDATES=$(git branch --list 'chore/cleaner-*' --sort=-committerdate --format='%(refname:short)' 2>/dev/null | head -"$CLEANER_REUSE_LOOKBACK")
for candidate in $RECENT_CANDIDATES; do
  # skip if already merged into main
  if git merge-base --is-ancestor "$candidate" main 2>/dev/null; then
    continue
  fi
  ahead=$(git rev-list --count "main..$candidate" 2>/dev/null || echo 0)
  [ "$ahead" -eq 0 ] && continue

  echo "[cleaner] reuse check: $candidate ($ahead commits ahead of main)" >&2
  if ! git checkout "$candidate" 2>/dev/null; then
    echo "[cleaner]   couldn't checkout $candidate — skip" >&2
    continue
  fi

  REUSE_OK=1
  if [ -f "$REPO_ROOT/package.json" ]; then
    ( cd "$REPO_ROOT" && npm run -s type-check >/dev/null 2>&1 ) || REUSE_OK=0
    if [ "$REUSE_OK" = "1" ]; then
      ( cd "$REPO_ROOT" && npm run -s build >/dev/null 2>&1 ) || REUSE_OK=0
    fi
  fi

  if [ "$REUSE_OK" = "1" ]; then
    echo "[cleaner] reuse: $candidate PASSES tc+build — fast-path squash-merge" >&2
    git checkout main 2>&1 | tail -1 >&2
    git merge --squash "$candidate" 2>&1 | tail -3 >&2

    # If squash produced no staged changes, the candidate's content is already
    # in main (cherry-picked individually). Treat as success without commit.
    # `git commit --no-verify` would fail on empty stage and crash with set -e.
    if git diff --cached --quiet; then
      echo "[cleaner] reuse: $candidate already-merged (squash empty) — skip commit, treat as success" >&2
      git branch -D "$candidate" 2>&1 | tail -1 >&2 || true
      NEW_SHA=$(git rev-parse HEAD)
      jq -n \
        --arg verdict "PASS" \
        --arg new_sha "$NEW_SHA" \
        --arg reused "$candidate" \
        --arg classification "$CLASSIFICATION" \
        '{verdict:$verdict,new_main_sha:$new_sha,reused_branch:$reused,squashed_commits:0,classification:$classification,fast_path:"reuse-noop",reason:"branch content already in main"}' \
        > "$OUTPUT_DIR/verdict.json"
      echo "[cleaner] === REUSE-NOOP === main already has the fix at sha=$NEW_SHA" >&2
      exit 0
    fi

    git commit --no-verify -m "chore(baseline): reuse cleaner branch for $CLASSIFICATION ($EPIC_ID)

Detective rationale: $RATIONALE

Reused unmerged cleaner branch \`$candidate\` instead of spawning a fresh LLM
cleaner. Predicate (type-check + build) verified PASS before merge.
Saved ~5 min + LLM tokens." 2>&1 | tail -2 >&2

    git branch -D "$candidate" 2>&1 | tail -1 >&2 || true
    NEW_SHA=$(git rev-parse HEAD)
    jq -n \
      --arg verdict "PASS" \
      --arg new_sha "$NEW_SHA" \
      --arg reused "$candidate" \
      --argjson commits "$ahead" \
      --arg classification "$CLASSIFICATION" \
      '{verdict:$verdict,new_main_sha:$new_sha,reused_branch:$reused,squashed_commits:$commits,classification:$classification,fast_path:"reuse"}' \
      > "$OUTPUT_DIR/verdict.json"
    echo "[cleaner] === REUSE SUCCESS === new main sha=$NEW_SHA" >&2
    exit 0
  fi

  echo "[cleaner] reuse: $candidate failed predicate — skipping" >&2
  git checkout main 2>&1 | tail -1 >&2
done

git checkout -b "$BRANCH" 2>&1 | tail -2 >&2

# ─── 2. Build cleaner prompt ──────────────────────────────────────────────
PROMPT_FILE="$OUTPUT_DIR/prompt.md"
INBOX_DIR="$AGENTFLOW_DIR/INBOX"
cat >"$PROMPT_FILE" <<EOF
You are the **mini-ork cleaner** — a single-shot agent that fixes a cross-cutting
blocker on main so paused epics can resume.

## Why you exist

Epic **$EPIC_ID** failed gauntlet with classification **$CLASSIFICATION**.
Detective rationale: $RATIONALE

You are NOT working on $EPIC_ID's feature. You are unblocking the BASELINE.

## Your brief (verbatim from detective)

$BRIEF

## Live branch-state diff (read FIRST)

You are on branch \`$BRANCH\` (off main). Before doing anything:

\`\`\`
\$(git log --oneline main..HEAD 2>/dev/null | head -8)
\`\`\`

Recent fix(baseline) commits ALREADY on main — DO NOT redo these:
\`\`\`
\$(git log --since="6 hours ago" --oneline main 2>/dev/null | grep -iE "fix\\(baseline|chore\\(deps|fix\\(types" | head -5)
\`\`\`

Live type-check error count on MAIN (your canonical target):
\`\`\`
main: \$(timeout 30 npm run -s type-check 2>&1 | grep -cE "error TS" 2>/dev/null || echo "?") errors
\`\`\`

**If main has 0 errors AND your branch's failures are pre-existing on disk only, exit immediately**: write \`${INBOX_DIR}/cleaner-stuck-$TS.md\` saying "main is clean; the worktree just needs a rebase + npm install, not a code fix" and exit. The orchestrator will rebase.

## Mandatory pre-flight (do this BEFORE any edit)

1. \`npm run -s type-check 2>&1 | head -50\` — see the actual errors
2. \`git diff --name-only HEAD~5..HEAD\` — see what's been recently modified
3. Confirm: every error you plan to address is in the brief's listed files
4. **If any error you'd need to fix is in a file NOT mentioned in the brief, STOP.** Write \`${INBOX_DIR}/cleaner-stuck-$TS.md\` and exit. Do NOT extend scope.

## Hard rules — violations = abort + branch discarded

- NO architectural changes. No replacing components with libraries — that's an epic, not a baseline-rot fix.
- NO refactoring. You read the type error, change the type/import/argument that produces it, run type-check, commit. That's it.
- NO touching files outside the brief's exact list. If your fix in one file requires touching an unrelated file, write to INBOX and exit.
- NO commit > 50 lines net change without explicit \`cleaner_brief\` permission. If your fix needs more, you're refactoring not fixing rot.
- You are on branch \`$BRANCH\` (off main). Stay on this branch.
- Each commit atomic + conventional (\`fix(baseline): ...\`, \`chore(deps): ...\`).
- After EACH commit run \`npm run -s type-check 2>&1 | grep -c "error TS"\` and confirm the count went DOWN (never same/up).
- If you can't complete the brief safely (ambiguity, scope blowup, count won't drop): commit nothing, \`git stash\`, write \`${INBOX_DIR}/cleaner-stuck-$TS.md\` with what's blocking + exact error list, exit.

## Common rot patterns (recognize and apply minimal fix)

- **\`@types/react 18 vs 19 cascade\`** ("Type 'bigint' is not assignable to ReactNode" everywhere) → fix is \`npm update @types/react @types/react-dom --save-dev\` from REPO ROOT, commit \`package-lock.yaml\` only. NOT individual file edits.
- **Module not found** → the imports likely point at deleted/renamed files. Fix the import path or stub the import. NOT rewriting the module.

## What success looks like

After your commits, the gauntlet that failed for $EPIC_ID should now PASS on main.
The orchestrator will:
  1. Verify your work via the gauntlet on this branch.
  2. Squash-merge your branch to main if gauntlet passes.
  3. Resume $EPIC_ID against the new clean baseline.

## Output

When done, commit and STOP. The orchestrator picks up the rest.
EOF

# Append optional memory block from insforge-pre-task hook if available
if [ -x "$AGENTFLOW_DIR/hooks/insforge-pre-task.sh" ]; then
  insforge_query="cleaner $CLASSIFICATION baseline"
  memory_block=$(AGENTFLOW_CALLER="cleaner:${EPIC_ID}:${CLASSIFICATION}" \
    bash "$AGENTFLOW_DIR/hooks/insforge-pre-task.sh" "$insforge_query" 3 2>/dev/null || echo "")
  if [ -n "$memory_block" ] && ! echo "$memory_block" | grep -qE "no insforge-context matches|insforge sdk not installed"; then
    echo "" >> "$PROMPT_FILE"
    echo "$memory_block" >> "$PROMPT_FILE"
  fi
fi

# ─── 3. Spawn cleaner agent (Anthropic sonnet — needs to read+write code) ─
# Cleaner runs locally on main, not in a sandbox, because:
#   - main is the canonical state
#   - we want full repo access for cross-cutting fixes
#   - it's a one-shot, not a long-running agent
WORKER_LOG="$OUTPUT_DIR/worker.log"
WORKER_ERR="$OUTPUT_DIR/worker.err"

CLEANER_MODEL="${CLEANER_MODEL:-claude-sonnet-4-6}"
CLEANER_BUDGET="${CLEANER_BUDGET_USD:-5.00}"

echo "[cleaner] spawning worker (model=$CLEANER_MODEL, budget=\$$CLEANER_BUDGET)" >&2

SCOPE_ARGS=()
[ -f "$SCOPE_CARD_FILE" ] && SCOPE_ARGS+=(--append-system-prompt-file "$SCOPE_CARD_FILE")

set +e
claude -p \
  --model "$CLEANER_MODEL" \
  --max-budget-usd "$CLEANER_BUDGET" \
  "${SCOPE_ARGS[@]}" \
  --add-dir "$REPO_ROOT" \
  --disallowedTools "Bash(make deploy*),Bash(npm run migrate:*),Bash(docker*),Bash(kubectl*),Bash(gh pr merge*),Bash(git push --force*),Bash(git push -f*),Bash(git reset --hard*),Bash(git checkout main*),Bash(rm -rf*),Bash(curl* -o*),Bash(wget*),Bash(ssh*),Bash(scp*),Agent,TaskCreate,TaskUpdate" \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --include-partial-messages \
  --verbose \
  "$(cat "$PROMPT_FILE")" \
  > "$WORKER_LOG" 2>"$WORKER_ERR"
WORKER_EXIT=$?
set -e

echo "$WORKER_EXIT" > "$OUTPUT_DIR/worker.exit"

# ─── 4. Verify the cleaner did something ──────────────────────────────────
COMMITS_AHEAD=$(git rev-list --count main..HEAD 2>/dev/null || echo "0")
if [ "$COMMITS_AHEAD" = "0" ]; then
  # Differentiate "intentional no-op" (main is already clean → cleaner correctly
  # exited stuck) from "actual failure". If the worker wrote a cleaner-stuck
  # INBOX file mentioning 'main is clean', return rc=10 so the orchestrator
  # knows to rebase the worktree + retry the worker, NOT count this as failure.
  STUCK_INBOX=$(ls "$INBOX_DIR/cleaner-stuck-${TS}"*.md 2>/dev/null | head -1)
  if [ -n "$STUCK_INBOX" ] && grep -qiE "main is clean|main has 0 errors|already (resolved|fixed)" "$STUCK_INBOX" 2>/dev/null; then
    echo "[cleaner] worker correctly exited stuck — main is clean, no work needed (rc=10)" >&2
    git checkout main 2>&1 | tail -1 >&2
    git branch -D "$BRANCH" 2>&1 | tail -1 >&2
    jq -n --arg reason "main-already-clean" '{verdict:"NO_OP",reason:$reason}' > "$OUTPUT_DIR/verdict.json"
    exit 10
  fi
  echo "[cleaner] worker produced no commits — aborting" >&2
  git checkout main 2>&1 | tail -1 >&2
  git branch -D "$BRANCH" 2>&1 | tail -1 >&2
  jq -n --arg reason "no commits" '{verdict:"FAIL",reason:$reason}' > "$OUTPUT_DIR/verdict.json"
  exit 6
fi

git log --oneline main..HEAD > "$OUTPUT_DIR/git.commits"
echo "[cleaner] worker produced $COMMITS_AHEAD commit(s)" >&2

# ─── 5. Run gauntlet on the cleaner's branch ──────────────────────────────
GAUNTLET_OUT="$OUTPUT_DIR/gauntlet"
GAUNTLET_SH="$MINI_ORK_ROOT/lib/gauntlet.sh"
if [ ! -f "$GAUNTLET_SH" ]; then
  GAUNTLET_SH="$AGENTFLOW_DIR/lib/gauntlet.sh"
fi

echo "[cleaner] running gauntlet on cleaner branch" >&2
set +e
bash "$GAUNTLET_SH" "$REPO_ROOT" "$GAUNTLET_OUT" 2>&1 | tee "$OUTPUT_DIR/gauntlet.tail.log" | tail -20
GAUNTLET_EXIT=${PIPESTATUS[0]}
set -e

if [ "$GAUNTLET_EXIT" != "0" ]; then
  # Narrowed merge gate: cleaner fixes baseline rot for *workers to iterate*.
  # Required: type-check + build (structural). If those pass, cleaner did its
  # job — pre-existing lint nits / api-smoke flakes on main are NOT cleaner's
  # responsibility. This avoids the "cleaner fixed 55 TS errors but blocked on
  # one unused-var lint" failure mode.
  CLEANER_REQUIRED_CHECKS="${CLEANER_REQUIRED_CHECKS:-type-check build}"
  GAUNTLET_JSON="$GAUNTLET_OUT/gauntlet.json"
  REQUIRED_OK=1
  ADVISORY_FAILS=""
  if [ -f "$GAUNTLET_JSON" ]; then
    for check in $CLEANER_REQUIRED_CHECKS; do
      status=$(jq -r --arg n "$check" '.[] | select(.name==$n) | .status' "$GAUNTLET_JSON" 2>/dev/null)
      if [ "$status" != "pass" ] && [ "$status" != "passed" ] && [ "$status" != "skipped" ]; then
        REQUIRED_OK=0
        echo "[cleaner] required check failed: $check ($status)" >&2
      fi
    done
    ADVISORY_FAILS=$(jq -r --argjson req "$(printf '%s\n' $CLEANER_REQUIRED_CHECKS | jq -R . | jq -s .)" \
      '.[] | select(.status=="fail" or .status=="failed") | select(.name as $n | $req | index($n) | not) | .name' \
      "$GAUNTLET_JSON" 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  else
    REQUIRED_OK=0
  fi

  if [ "$REQUIRED_OK" = "1" ]; then
    echo "[cleaner] required checks PASS (advisory fails: ${ADVISORY_FAILS:-none}) — proceeding to auto-merge" >&2
    GAUNTLET_EXIT=0
  else
    echo "[cleaner] required checks FAIL — NOT auto-merging" >&2
    jq -n \
      --arg reason "required cleaner checks failing" \
      --arg required "$CLEANER_REQUIRED_CHECKS" \
      --arg advisory "$ADVISORY_FAILS" \
      --argjson commits "$COMMITS_AHEAD" \
      --arg branch "$BRANCH" \
      '{verdict:"FAIL",reason:$reason,required_checks:$required,advisory_fails:$advisory,commits_made:$commits,branch:$branch}' \
      > "$OUTPUT_DIR/verdict.json"
    echo "[cleaner] preserving branch $BRANCH for human inspection" >&2
    git checkout main 2>&1 | tail -1 >&2
    exit 7
  fi
fi

# ─── 6. Auto-merge cleaner branch to main ─────────────────────────────────
echo "[cleaner] gauntlet PASS — squash-merging $BRANCH → main" >&2
git checkout main 2>&1 | tail -1 >&2
git merge --squash "$BRANCH" 2>&1 | tail -3 >&2
COMMIT_MSG="chore(baseline): cleaner fixed $CLASSIFICATION blocker for $EPIC_ID

Detective rationale: $RATIONALE

Cleaner brief: $BRIEF

Auto-spawned by mini-ork detective+cleaner. Commits squashed from
branch $BRANCH ($COMMITS_AHEAD commits)."
git commit --no-verify -m "$COMMIT_MSG" 2>&1 | tail -2 >&2

git branch -D "$BRANCH" 2>&1 | tail -1 >&2 || true

NEW_SHA=$(git rev-parse HEAD)
jq -n \
  --arg verdict "PASS" \
  --arg new_sha "$NEW_SHA" \
  --argjson commits "$COMMITS_AHEAD" \
  --arg classification "$CLASSIFICATION" \
  '{verdict:$verdict,new_main_sha:$new_sha,squashed_commits:$commits,classification:$classification}' \
  > "$OUTPUT_DIR/verdict.json"

echo "[cleaner] === SUCCESS === new main sha=$NEW_SHA" >&2
exit 0
