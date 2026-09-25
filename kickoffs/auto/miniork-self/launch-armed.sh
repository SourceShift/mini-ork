#!/usr/bin/env bash
# CLOSED-LOOP RSI driver pointed at mini-ork ITSELF.
#
# The wave reads a STATIC unit list (binding/list_units.py), plans a bounded
# code-fix child against the target worktree, and re-runs an executable
# predicate (binding/unit_predicate.py) over every unit to emit
# panel-verdict.json. There is deliberately NO deploy stage: the predicate
# scores MO_GOAL_TARGET_CWD — the worktree the child edits — so the fix is
# scored in place, and mini-ork is never asked to self-modify mid-run. Merging
# that worktree to main is a separate, human-gated step.
#
#     bash kickoffs/auto/miniork-self/launch-armed.sh
#
# Nothing here is irreversible: no prod push, no live-db write. The predicate
# is read-only against the live db (it copies it before applying anything).
set -uo pipefail

# ── engine paths (the running mini-ork; the loop driver lives here) ──
export MINI_ORK_ROOT="${MINI_ORK_ROOT:-/Volumes/docker-ssd/ps/mini-ork}"
export MINI_ORK_HOME="${MINI_ORK_HOME:-${MINI_ORK_ROOT}/.mini-ork}"
export MINI_ORK_DB="${MINI_ORK_DB:-${MINI_ORK_HOME}/state.db}"

# ── the tree under test: the worktree the fix children EDIT ──
WT="${MO_SELF_WORKTREE:-/Volumes/docker-ssd/ps/mini-ork-worktrees/miniork-self-fixes}"
if [ ! -d "$WT/db/migrations" ]; then
  echo "target worktree has no db/migrations: $WT" >&2
  echo "create it first:  make worktree SLUG=miniork-self-fixes OWNS='mini_ork/stores db/migrations'" >&2
  exit 2
fi
export MO_GOAL_TARGET_CWD="$WT"
export MO_TARGET_CWD="$WT"
# A mini-ork tree edited in place trips the framework-cwd guard without this.
export MO_ALLOW_FRAMEWORK_CWD=1

# ── pinned run dir: driver READ == wave-child WRITE ──
TS=$(date +%Y%m%d-%H%M%S)
export MINI_ORK_RUN_ID="${MINI_ORK_RUN_ID:-goalloop-self-${TS}}"
export MINI_ORK_RUN_DIR="${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}"
mkdir -p "${MINI_ORK_RUN_DIR}"

# ── wave + child kickoffs / goal inputs (wave nodes read these from ENV) ──
BIND="${MINI_ORK_ROOT}/kickoffs/auto/miniork-self/binding"
export MO_GOAL_WAVE_KICKOFF="$BIND/wave-kickoff.md"
export MO_GOAL_CHILD_KICKOFF="$BIND/child-kickoff.md"
export MO_GOAL_UNITS_CMD="python3 $BIND/list_units.py"
export MO_GOAL_PREDICATE_CMD="python3 $BIND/unit_predicate.py"
export MO_GOAL_CHILD_RECIPE="${MO_GOAL_CHILD_RECIPE:-code-fix}"
# The list is static and non-vacuous: list_units.py exits 2 rather than emitting
# an empty list when the tree or the db is unreadable, so an empty list here is
# a broken lister, never a satisfied goal.
unset MO_GOAL_EMPTY_UNITS_PASS

# The instrument scoring the work. Referenced by the predicate's docstring and
# honoured by the deploy stage (unused here — this loop has no deploy).
export MO_GOAL_PROTECTED_PATHS="$BIND"

# ── lanes ──
# Working lanes on this host: deepseek-v4-flash, minimax-m3, glm-5.3, opus-4.x.
# Code nodes never get glm (analysis-only, 429s silently). The fallback tail is
# PINNED to lanes that actually serve: the stock tail ends in dead codex/sonnet.
export MO_CHEAP_LANE="${MO_CHEAP_LANE:-deepseek-v4-flash}"
export MO_FALLBACK_CODING="${MO_FALLBACK_CODING:-minimax-m3,deepseek-v4-flash}"

# ── learning ──
# 2 rather than the default 3: the region/domain slices this campaign wants to
# populate do not qualify at 3, and a slice that never qualifies never produces
# a route_margin for the UCCI map to fit from.
export MO_LEARNING_MIN_SAMPLES="${MO_LEARNING_MIN_SAMPLES:-2}"
# The $50 default opens the spend circuit mid-campaign. This is a queue cap,
# not an approval to spend it.
export MO_DAILY_BUDGET_USD="${MO_DAILY_BUDGET_USD:-2000}"

MAX_WAVES="${MO_GOAL_MAX_WAVES:-1}"
BUDGET_USD="${MO_GOAL_BUDGET_USD:-45}"

# Deterministic start: a stale verdict from a previous attempt would be read as
# this run's result.
SD="${MINI_ORK_HOME}/goal-loop/miniork-self"
rm -f "$SD/goal-loop-state.json" "$SD/final-verdict.json"

PY="${MINI_ORK_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY=python3.11

cd "$MINI_ORK_ROOT"
"$PY" recipes/goal-loop/lib/drive.py \
  --goal-id miniork-self --target-cwd "$WT" \
  --units-cmd "python3 $BIND/list_units.py" \
  --predicate-cmd "python3 $BIND/unit_predicate.py" \
  --child-recipe "$MO_GOAL_CHILD_RECIPE" \
  --max-waves "$MAX_WAVES" --budget-usd "$BUDGET_USD"
