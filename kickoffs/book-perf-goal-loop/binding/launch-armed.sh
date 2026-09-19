#!/usr/bin/env bash
# CLOSED-LOOP autonomous PERFORMANCE driver — the chapter-write cost + time
# contract. A sibling of kickoffs/book-goal-loop/launch-armed.sh; the ONLY
# differences are: the units/predicate/evidence commands point at THIS binding's
# three-axis predicate, and the perf-budget ratchet env is wired. The deploy +
# re-dispatch stage reuses the correctness binding's restart_worker.py /
# redispatch_chapter.py / chapter_terminal_fail.py BY RELATIVE PATH (not copied).
#
# SAFE BY DEFAULT: MO_GOAL_APPLY_DRY defaults to 1 — the goal_apply node records
# the deploy/redispatch PLAN without executing the irreversible prod push or
# spend. The single go/no-go to run it for real is:
#
#     MO_GOAL_APPLY_DRY=0 bash kickoffs/book-perf-goal-loop/binding/launch-armed.sh
#
# That, and only that, pushes to libwit PRODUCTION main and burns a real regen
# lane. A running researcher book-generation worker is REQUIRED. This launcher
# must NOT run as part of authoring the binding.
set -uo pipefail

# ── engine paths — resolved from THIS script's location, never hardcoded ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # this binding/
PERF_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"                     # kickoffs/book-perf-goal-loop/
SIB_BIND="$(cd "$SCRIPT_DIR/../../book-goal-loop/binding" && pwd)"  # correctness binding (reuse-by-path)
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"                   # engine repo root

export MINI_ORK_ROOT="$ROOT"
export MINI_ORK_HOME="${MINI_ORK_HOME:-$ROOT/.mini-ork}"
export MINI_ORK_DB="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"
# MINI_ORK_SECRETS is NOT defaulted here: no secret lives in this file (mini-ork
# origin is a PUBLIC repo). Export it (or the libpq env vars below) in your
# shell before running.

# ── researcher targets (operator-supplied — NO hardcoded absolute paths) ──
#   MO_GOAL_TARGET_CWD = the worktree the fix children EDIT and the deploy pushes FROM.
#   MO_RESEARCHER_DIR   = a built checkout (node_modules + server/.env) the
#                         redispatch's sanctioned forceResumeJob CLI runs from.
: "${MO_GOAL_TARGET_CWD:?export MO_GOAL_TARGET_CWD (researcher fix worktree) before running}"
: "${MO_RESEARCHER_DIR:?export MO_RESEARCHER_DIR (researcher primary checkout) before running}"
export MO_TARGET_CWD="$MO_GOAL_TARGET_CWD"
export MINI_ORK_TARGET_REPO="$MO_GOAL_TARGET_CWD"

# ── book/goal inputs (live Postgres) ──
# NO secret in this file. Export PGPASSWORD in your shell or source it from
# MINI_ORK_SECRETS before running. Host/port/user are operator-overridable.
export BOOK_UUID="${BOOK_UUID:-d0df3cdb-8164-450e-b841-2c9354ea0423}"
: "${PGPASSWORD:?export PGPASSWORD (researcher DB) before running — never hardcode it here}"
export PGHOST="${PGHOST:-REDACTED-INTERNAL-IP}" PGPORT="${PGPORT:-5932}" PGUSER="${PGUSER:-researcher_user}" PGDATABASE="${PGDATABASE:-researcher_db}"

# ── pinned run dir: driver READ == wave-child WRITE ──
TS=$(date +%Y%m%d-%H%M%S)
export MINI_ORK_RUN_ID="goalloop-perf-${TS}"
export MINI_ORK_RUN_DIR="${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}"
mkdir -p "${MINI_ORK_RUN_DIR}"

# ── wave + child kickoffs / goal inputs (wave nodes read these from ENV) ──
export MO_GOAL_WAVE_KICKOFF="$PERF_DIR/wave-kickoff.md"
export MO_GOAL_CHILD_KICKOFF="$PERF_DIR/child-kickoff.md"
export MO_GOAL_UNITS_CMD="bash $SCRIPT_DIR/list_units.sh"
export MO_GOAL_PREDICATE_CMD="python3 $SCRIPT_DIR/unit_predicate.py"
# DEEP perf evidence (optional but on by default): run ONCE per wave for only
# the selected fix units, capturing per-node verified-artifact run counts /
# cost / duration so the fix child targets the dominant node rather than
# guessing from the 80-char predicate slice.
export MO_GOAL_EVIDENCE_CMD="${MO_GOAL_EVIDENCE_CMD:-python3 $SCRIPT_DIR/harvest_perf_evidence.py}"
export MO_GOAL_CHILD_RECIPE=code-fix
export MO_GOAL_MAX_CHILDREN_PER_WAVE="${MO_GOAL_MAX_CHILDREN_PER_WAVE:-1}"

# ── perf budget ratchet ──
# The predicate + baseline.py share this ONE file as the epoch source; changing
# it independently would make cost drift across waves. The tighten floor may
# only be RAISED (baseline.py clamps it to the shipped default).
export MO_GOAL_PERF_BUDGET="${MO_GOAL_PERF_BUDGET:-$MO_GOAL_TARGET_CWD/.mini-ork/perf-budget.json}"
export MO_GOAL_PERF_TIGHTEN="${MO_GOAL_PERF_TIGHTEN:-0.05}"

# ── recipe / loop knobs ──
export MO_STATIC_RECIPE_PLAN=1
export MINI_ORK_PROFILE_GATE=0
export MINI_ORK_RECURSIVE_MAX_PARALLEL=1
export MINI_ORK_ALLOW_CHILD_SPAWN=1
# 0 = spawn a REAL fix child (LLM spend); 1 = scaffold-only (~$0 wiring proof).
export MO_GOAL_NO_EXECUTE="${MO_GOAL_NO_EXECUTE:-0}"
# CLOSED-LOOP verification ownership: the code-fix child's in-sandbox reviewer
# is redundant AND evidence-starved here; the real gate is downstream
# (deploy -> regen -> DB flip). Keep the worktree edit and let the OUTER loop
# (regen + GRAO quarantine + divergence-kill) be the gate — a needs_revision
# reviewer would rollback the verified edit before goal_apply can deploy it.
export MINI_ORK_ROLLBACK_KEEP_WORKTREE="${MINI_ORK_ROLLBACK_KEEP_WORKTREE:-1}"

# ── grandchild verifiers scoped OFF the 17k-file researcher build ──
export MINI_ORK_TYPECHECK_CMD='echo scoped-typecheck-skipped-bounded-goal-loop-demo'
export MINI_ORK_TEST_CMD='echo scoped-test-skipped-bounded-goal-loop-demo'

# ── APPLY / DEPLOY / RE-DISPATCH stage (goal_apply node) — reuse-by-path ──
export MO_GOAL_APPLY=1
# SAFE default: dry. Flip to 0 at the go/no-go to run the real prod push + spend.
export MO_GOAL_APPLY_DRY="${MO_GOAL_APPLY_DRY:-1}"
# Deploy MODE: local-worker (default, reversible, dev-scoped) restarts the
# book-generation worker FROM the worktree. prod-push is operator-supplied (the
# correctness binding's prod-push command is book-specific and must not be
# copied); it refuses loudly rather than fabricate a push command.
export MO_GOAL_DEPLOY_MODE="${MO_GOAL_DEPLOY_MODE:-local-worker}"
if [ "$MO_GOAL_DEPLOY_MODE" = "prod-push" ]; then
  export MO_GOAL_APPLY_CMD="${MO_GOAL_APPLY_CMD:?set MO_GOAL_APPLY_CMD for prod-push mode}"
else
  export MO_GOAL_APPLY_CMD="python3 $SIB_BIND/restart_worker.py"
fi
# Re-dispatch: sanctioned forceResumeJob per failing chapter (idempotent per wave).
export MO_GOAL_REDISPATCH_CMD="python3 $SIB_BIND/redispatch_chapter.py"
# Deploy settle + await regen windows (same as the correctness launcher).
export MO_GOAL_DEPLOY_SETTLE_SECONDS="${MO_GOAL_DEPLOY_SETTLE_SECONDS:-480}"
export MO_GOAL_DEPLOY_TIMEOUT_SECONDS="${MO_GOAL_DEPLOY_TIMEOUT_SECONDS:-900}"
export MO_GOAL_APPLY_AWAIT_SECONDS="${MO_GOAL_APPLY_AWAIT_SECONDS:-5400}"
export MO_GOAL_APPLY_POLL_SECONDS="${MO_GOAL_APPLY_POLL_SECONDS:-60}"
# FAIL-FAST: stop awaiting the instant a regen terminally fails / stalls.
export MO_GOAL_TERMINAL_FAIL_CMD="${MO_GOAL_TERMINAL_FAIL_CMD:-python3 $SIB_BIND/chapter_terminal_fail.py}"
export MO_GOAL_RUNS_DIR="${MO_GOAL_RUNS_DIR:-$MO_GOAL_TARGET_CWD/.mini-ork/runs}"
export MO_GOAL_STALL_SECONDS="${MO_GOAL_STALL_SECONDS:-1200}"
export MO_GOAL_DIVERGENCE_PATIENCE="${MO_GOAL_DIVERGENCE_PATIENCE:-6}"
export MO_GOAL_QUARANTINE_PATIENCE="${MO_GOAL_QUARANTINE_PATIENCE:-6}"

PY="${MINI_ORK_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY=python3.11
command -v "$PY" >/dev/null 2>&1 || PY=python3

GOAL_ID="book-${BOOK_UUID%%-*}"

echo "=== goal-loop PERFORMANCE driver launch $(date -u +%FT%TZ) ==="
echo "RUN_ID=${MINI_ORK_RUN_ID}"
echo "RUN_DIR=${MINI_ORK_RUN_DIR}"
echo "TARGET_CWD=${MO_GOAL_TARGET_CWD}"
echo "MO_GOAL_APPLY_DRY=${MO_GOAL_APPLY_DRY}  (1=dry/no prod push, 0=LIVE prod push + spend)"
MAX_WAVES="${MO_GOAL_MAX_WAVES:-1}"
BUDGET_USD="${MO_GOAL_BUDGET_USD:-45}"
echo "MO_GOAL_MAX_WAVES=${MAX_WAVES}  MO_GOAL_BUDGET_USD=${BUDGET_USD}  MAX_CHILDREN_PER_WAVE=${MO_GOAL_MAX_CHILDREN_PER_WAVE}"
echo "MO_GOAL_EVIDENCE_CMD=${MO_GOAL_EVIDENCE_CMD}"
echo "MO_GOAL_PERF_BUDGET=${MO_GOAL_PERF_BUDGET}"
echo "=== driver output ==="

SD="${MINI_ORK_HOME}/goal-loop/${GOAL_ID}"
rm -f "$SD/goal-loop-state.json" "$SD/final-verdict.json"

cd "$MINI_ORK_ROOT"
"$PY" recipes/goal-loop/lib/drive.py \
  --goal-id "$GOAL_ID" \
  --target-cwd "$MO_GOAL_TARGET_CWD" \
  --units-cmd "bash $SCRIPT_DIR/list_units.sh" \
  --predicate-cmd "python3 $SCRIPT_DIR/unit_predicate.py" \
  --child-recipe code-fix \
  --max-waves "$MAX_WAVES" \
  --budget-usd "$BUDGET_USD"
rc=$?

echo "=== driver exit rc=${rc} ==="
echo "=== apply-result.json ==="
cat "${MINI_ORK_RUN_DIR}/apply-result.json" 2>/dev/null || echo "(no apply-result.json)"
echo "=== panel-verdict.json ==="
cat "${MINI_ORK_RUN_DIR}/panel-verdict.json" 2>/dev/null || echo "(no panel-verdict.json)"
echo "=== DONE rc=${rc} ==="
