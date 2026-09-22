#!/usr/bin/env bash
# CLOSED-LOOP autonomous RSI driver that LANDS a compose book: two phases, armed.
#
# The draft-region sibling of launch-planning-armed.sh, pointed at
# job_1790005402603_0bd1f22b (the "Better Harness" book). That job is parked
# BEFORE the planning region: FSM `draft` (state_revision 8) while the UI sits
# on ?step=plan and draft_form_data autosaves climbed to write_revision 25 —
# measured 2026-09-22. No planning session, no planning events, no lifecycle
# rows, no burst ledger: BOTH existing loops are blind to it (the planning
# loop's detector needs compose_planning_events; the chapter loop's lister
# needs book_chapter_lifecycle). So the unit here is PINNED by the operator,
# not detected statistically — there is nothing to detect with.
#
# TWO PHASES, one goal ("the book lands with every chapter written, high
# quality, at the plan-declared minimum length"):
#   Phase A (THIS driver): fix children repair the researcher server/ code
#     that drops the draft→plan transition silently; after each live deploy
#     the loop itself fires the next golden-path FSM action through the
#     product's sanctioned pipeline (advance_fsm.py → applyComposeFsmActionCore)
#     until the FSM reads `generating`/`completed`.
#   Phase B (chained on Phase A pass): the ALREADY-VERIFIED chapter loop
#     (launch-book-armed.sh) drives every chapter to committed_complete +
#     rubric pass + the plan-declared length floor (chapter_quality.py takes
#     max(declared, env) — the min-chapter-length requirement lives THERE).
#
# THE BOOK IDENTITY TRAP applies here too — this job carries:
#   * job_1790005402603_0bd1f22b            the TEXT job id (planning/artifact tables)
#   * 4a9194b1-c15a-4fbc-bc99-b2520d9d1bc2  the RUN uuid (book_generation_runs.id,
#     compose_job_fsm_state.job_id) — Phase A's unit space.
#   * the GENERATED BOOK uuid — does NOT EXIST YET. It is minted by the
#     canonical plan (artifact_payload->>'bookUUID') when planning completes,
#     which is why Phase B's BOOK_UUID is resolved at chain time, never
#     hardcoded. (run.book_id is a THIRD, different uuid: 43875c2f… — using it
#     for lifecycle rows returns zero rows and lies.)
#
# SAFE BY DEFAULT: MO_GOAL_APPLY_DRY defaults to 1 — deploy/re-dispatch PLAN
# only, nothing committed, no worker restart, no FSM action fired. Go/no-go:
#
#     MO_GOAL_APPLY_DRY=0 bash kickoffs/book-goal-loop/launch-landing-armed.sh
#
# A running researcher book-generation worker is REQUIRED (deploy restarts it,
# and the planning burst Phase A drives into needs it).
set -uo pipefail

# ── engine paths ──
export MINI_ORK_ROOT=/Volumes/docker-ssd/ps/mini-ork
export MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork
export MINI_ORK_SECRETS=/Volumes/docker-ssd/ps/mini-ork/config/secrets.local.sh
export MINI_ORK_DB="${MINI_ORK_HOME}/state.db"

# ── pinned run dir: driver READ == wave-child WRITE ──
TS=$(date +%Y%m%d-%H%M%S)
export MINI_ORK_RUN_ID="goalloop-landing-${TS}"
export MINI_ORK_RUN_DIR="${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}"
mkdir -p "${MINI_ORK_RUN_DIR}"

# ── researcher targets ──
WORKTREES="${WORKTREES:-/Volumes/docker-ssd/Migration/Development/worktrees}"
WT="${MO_GOAL_TARGET_CWD:-$WORKTREES/goalloop-landing}"
PRIMARY="${MO_RESEARCHER_DIR:-/Volumes/docker-ssd/Migration/Development/researcher}"
export MO_GOAL_TARGET_CWD="$WT"
export MO_TARGET_CWD="$WT"
export MINI_ORK_TARGET_REPO="$WT"
export MO_RESEARCHER_DIR="$PRIMARY"
# Pin the researcher toolchain: tsx's shebang resolves `node` from PATH, and
# the homebrew node (25.x) is not the engine the repo pins (22.23.1).
export PATH="$HOME/.nvm/versions/node/v22.23.1/bin:$PATH"

# ── live Postgres ──
# NO secret in this file (mini-ork origin is a PUBLIC repo).
: "${PGPASSWORD:?export PGPASSWORD (researcher DB) before running — never hardcode it here}"
export PGHOST="${PGHOST:-REDACTED-INTERNAL-IP}" PGPORT="${PGPORT:-5932}" PGUSER="${PGUSER:-researcher_user}" PGDATABASE="${PGDATABASE:-researcher_db}"
# PG* → POSTGRES_* bridge: the checkout's ROOT .env poisons POSTGRES_* with a
# dead localhost endpoint and dotenvx never overrides a pre-set key (measured
# 2026-09-21, same trap launch-planning-armed.sh documents). advance_fsm.py
# pins these again as defence in depth.
export POSTGRES_HOST="${POSTGRES_HOST:-$PGHOST}" POSTGRES_PORT="${POSTGRES_PORT:-$PGPORT}"
export POSTGRES_USER="${POSTGRES_USER:-$PGUSER}" POSTGRES_DB="${POSTGRES_DB:-$PGDATABASE}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$PGPASSWORD}"

# ── the PINNED unit ──
# The lister emits exactly this run uuid while its FSM is pre-writing, and an
# empty list once it reaches generating/completed (or cancelled — dead, not
# fixed; job_terminal_fail sees that separately).
RUN_UUID="${MO_GOAL_LANDING_RUN_UUID:-4a9194b1-c15a-4fbc-bc99-b2520d9d1bc2}"
export MO_GOAL_LANDING_RUN_UUID="$RUN_UUID"
# A fresh plan_sketching burst is IN FLIGHT, not wedged: withhold the unit while
# the FSM moved within this window so a redundant child is not dispatched.
export MO_GOAL_LANDING_STALL_SECONDS="${MO_GOAL_LANDING_STALL_SECONDS:-900}"

# ── wave + child kickoffs / goal inputs ──
BIND="${MINI_ORK_ROOT}/kickoffs/book-goal-loop/binding"
export MO_GOAL_WAVE_KICKOFF="$BIND/landing-wave-kickoff.md"
export MO_GOAL_CHILD_KICKOFF="$BIND/landing-child-kickoff.md"
export MO_GOAL_UNITS_CMD="python3 $BIND/landing_units.py"
export MO_GOAL_PREDICATE_CMD="python3 $BIND/landing_predicate.py"
# EMPTY BOARD IS THE GOAL STATE (the pinned job entered the writing region).
# Safe only because landing_units.py is non-vacuous by construction: it exits 2
# when it cannot read the DB, so an empty list is always a real observation.
export MO_GOAL_EMPTY_UNITS_PASS=1
# Deep evidence: mostly ABSENCE (no session, no events, no dispatch) stated
# explicitly, plus the autosave revision trail proving the API path is alive.
export MO_GOAL_EVIDENCE_CMD="${MO_GOAL_EVIDENCE_CMD:-python3 $BIND/landing_evidence.py}"
# Fail-fast await: latches the newest failure, calls TERMINAL only on a NEWER
# failure of the SAME class.
export MO_GOAL_TERMINAL_FAIL_CMD="${MO_GOAL_TERMINAL_FAIL_CMD:-python3 $BIND/job_terminal_fail.py}"
export MO_GOAL_CHILD_RECIPE=code-fix
export MO_GOAL_MAX_CHILDREN_PER_WAVE="${MO_GOAL_MAX_CHILDREN_PER_WAVE:-1}"

# ── recipe / loop knobs ──
export MO_STATIC_RECIPE_PLAN=1
export MINI_ORK_PROFILE_GATE=0
export MINI_ORK_RECURSIVE_MAX_PARALLEL=1
export MINI_ORK_ALLOW_CHILD_SPAWN=1
export MO_GOAL_NO_EXECUTE="${MO_GOAL_NO_EXECUTE:-0}"
export MINI_ORK_ROLLBACK_KEEP_WORKTREE="${MINI_ORK_ROLLBACK_KEEP_WORKTREE:-1}"

# ── grandchild verifiers: REAL, scoped to the child's own diff ──
export MINI_ORK_TYPECHECK_CMD="python3 $BIND/scoped_gate.py typecheck"
export MINI_ORK_TEST_CMD="python3 $BIND/scoped_gate.py test"
export MO_GOAL_SCOPED_BASE="${MO_GOAL_SCOPED_BASE:-origin/main}"

# ── APPLY / DEPLOY / RE-DISPATCH (goal_apply node) ──
export MO_GOAL_APPLY=1
export MO_GOAL_APPLY_DRY="${MO_GOAL_APPLY_DRY:-1}"
export MO_GOAL_FIX_BRANCH="${MO_GOAL_FIX_BRANCH:-rsi/landing-1790005402603}"
export MO_GOAL_APPLY_CMD="python3 $BIND/deploy_branch_local.py"
# Re-dispatch = fire the NEXT golden-path FSM action through the sanctioned
# pipeline (applyComposeFsmActionCore, actor system:goal-loop-landing). One
# action per wave, straight toward writing: draft→next, plan→start_planning,
# plan_ready→confirm_plan (AUTO-CONFIRM is deliberate — this loop's charter is
# no-human-in-the-loop; the quality backstop is Phase B + the frozen bar).
export MO_GOAL_REDISPATCH_CMD="python3 $BIND/advance_fsm.py"
export MO_GOAL_REDISPATCH_WINDOW_SECONDS="${MO_GOAL_REDISPATCH_WINDOW_SECONDS:-900}"
export MO_GOAL_REDISPATCH_POLL_SECONDS="${MO_GOAL_REDISPATCH_POLL_SECONDS:-30}"
export MO_GOAL_REDISPATCH_TIMEOUT_SECONDS="${MO_GOAL_REDISPATCH_TIMEOUT_SECONDS:-300}"

# INSTRUMENT GUARD. The predicate reads the FSM reaching `generating` — state
# written by the tree the child edits. Three files ARE the instrument:
#   * graphDefinition.json — the legal transitions. A child that cannot make
#     `next` fire honestly could add a draft→generating edge; that is the pass
#     being laundered, not earned.
#   * evidenceRetrievalPolicy.ts — the per-topic evidence floor the planning
#     burst must clear on the way to plan_ready.
#   * operatorExecutors.ts — where that floor is enforced.
# transition.ts / guards.ts / inputs.ts / routes are deliberately NOT listed:
# the BUG lives somewhere in there, freezing them would freeze the fix target.
export MO_GOAL_PROTECTED_PATHS="${MO_GOAL_PROTECTED_PATHS:-
server/compose/fsm/graphDefinition.json
server/services/bookGeneration/evidenceBank/evidenceRetrievalPolicy.ts
server/compose/operatorExecutors.ts
}"
export MO_GOAL_PROTECTED_MODE="${MO_GOAL_PROTECTED_MODE:-refuse}"

# Deploy settle + await. The await must cover a FULL planning burst (the
# longest leg between two golden-path actions is plan_sketching→plan_ready).
export MO_GOAL_DEPLOY_SETTLE_SECONDS="${MO_GOAL_DEPLOY_SETTLE_SECONDS:-480}"
export MO_GOAL_DEPLOY_TIMEOUT_SECONDS="${MO_GOAL_DEPLOY_TIMEOUT_SECONDS:-900}"
export MO_GOAL_APPLY_AWAIT_SECONDS="${MO_GOAL_APPLY_AWAIT_SECONDS:-5400}"
export MO_GOAL_APPLY_POLL_SECONDS="${MO_GOAL_APPLY_POLL_SECONDS:-60}"

# CROSS-WAVE PATIENCE: several waves against one silent wedge before giving up.
export MO_GOAL_DIVERGENCE_PATIENCE="${MO_GOAL_DIVERGENCE_PATIENCE:-6}"
export MO_GOAL_QUARANTINE_PATIENCE="${MO_GOAL_QUARANTINE_PATIENCE:-6}"

PY="${MINI_ORK_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY=python3.11

echo "=== goal-loop LANDING driver launch $(date -u +%FT%TZ) ==="
echo "RUN_ID=${MINI_ORK_RUN_ID}"
echo "RUN_DIR=${MINI_ORK_RUN_DIR}"
echo "PINNED_RUN_UUID=${RUN_UUID}"
echo "WORKTREE=${WT}"
echo "BRANCH=${MO_GOAL_FIX_BRANCH}  (local commit + worker restart; nothing pushed)"
echo "MO_GOAL_APPLY_DRY=${MO_GOAL_APPLY_DRY}  (1=dry plan only, 0=LIVE commit + restart + FSM action)"
MAX_WAVES="${MO_GOAL_MAX_WAVES:-6}"
# Budget: derived, not constant (drive.py measures against the GLOBAL 24h
# rolling meter — see the BUDGET TRAP note in launch-planning-armed.sh).
HEADROOM="${MO_GOAL_BUDGET_HEADROOM_USD:-200}"
AMBIENT="$("$PY" -c "from mini_ork import scheduler; print(f'{scheduler.today_cost_usd(\"${MINI_ORK_HOME}/state.db\"):.2f}')" 2>/dev/null || true)"
case "$AMBIENT" in ''|*[!0-9.]*) AMBIENT="" ;; esac
if [ -n "$AMBIENT" ]; then
  BUDGET_USD="${MO_GOAL_BUDGET_USD:-$("$PY" -c "print(f'{$AMBIENT + $HEADROOM:.2f}')")}"
else
  BUDGET_USD="${MO_GOAL_BUDGET_USD:-45}"
fi
DAILY_CAP="${MO_DAILY_BUDGET_USD:-50.0}"
if [ -n "$AMBIENT" ] && "$PY" -c "import sys; sys.exit(0 if $DAILY_CAP <= $AMBIENT else 1)" 2>/dev/null; then
  echo "WARNING: MO_DAILY_BUDGET_USD=${DAILY_CAP} <= ambient \$${AMBIENT} — the cost circuit is ALREADY OPEN;" >&2
  echo "         child dispatch will halt. Raise it above the ambient total." >&2
fi
echo "MO_GOAL_MAX_WAVES=${MAX_WAVES}  MO_GOAL_BUDGET_USD=${BUDGET_USD} (ambient ${AMBIENT:-unknown} + headroom ${HEADROOM})  MO_DAILY_BUDGET_USD=${DAILY_CAP}"
echo "=== Phase A: land the job in the writing region ==="

SD="${MINI_ORK_HOME}/goal-loop/landing-wedge"
rm -f "$SD/goal-loop-state.json" "$SD/final-verdict.json"

cd "$MINI_ORK_ROOT"
"$PY" recipes/goal-loop/lib/drive.py \
  --goal-id landing-wedge \
  --target-cwd "$WT" \
  --units-cmd "python3 $BIND/landing_units.py" \
  --predicate-cmd "python3 $BIND/landing_predicate.py" \
  --child-recipe code-fix \
  --max-waves "$MAX_WAVES" \
  --budget-usd "$BUDGET_USD"
rc=$?

echo "=== Phase A driver exit rc=${rc} ==="
echo "=== apply-result.json ==="
cat "${MINI_ORK_RUN_DIR}/apply-result.json" 2>/dev/null || echo "(no apply-result.json)"
echo "=== panel-verdict.json ==="
cat "${MINI_ORK_RUN_DIR}/panel-verdict.json" 2>/dev/null || echo "(no panel-verdict.json)"

# ── Phase B chain: chapter loop over the freshly-minted book ──
# Gate: the LIVE predicate, not the driver's rc — the only fact that matters is
# whether the job is actually writing now. BOOK_UUID is resolved from the
# canonical plan the planning burst just wrote (it did not exist at launch).
# Phase B's state namespace (book-rsi-*) is hardcoded in launch-book-armed.sh;
# acceptable because that launcher clears its own state files pre-run.
CHAIN="${MO_GOAL_CHAIN_PHASE_B:-1}"
if [ "$CHAIN" = "1" ] && python3 "$BIND/landing_predicate.py" "$RUN_UUID"; then
  GEN_BOOK_UUID="$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -tA -c \
    "SELECT a.artifact_payload->>'bookUUID' FROM book_run_artifacts a \
     JOIN book_generation_runs r ON r.job_id=a.job_id \
     WHERE r.id='${RUN_UUID}' AND a.artifact_kind='canonical_plan' \
     ORDER BY a.write_revision DESC LIMIT 1;" 2>/dev/null | head -1)"
  case "$GEN_BOOK_UUID" in
    [0-9a-f]*-*-*-*-*)
      echo "=== Phase B: chapter loop over generated book ${GEN_BOOK_UUID} ==="
      BOOK_UUID="$GEN_BOOK_UUID" \
      MO_GOAL_TARGET_CWD="$WT" \
      MO_GOAL_FIX_BRANCH="$MO_GOAL_FIX_BRANCH" \
      MO_GOAL_APPLY_DRY="$MO_GOAL_APPLY_DRY" \
        bash "${MINI_ORK_ROOT}/kickoffs/book-goal-loop/launch-book-armed.sh"
      rc=$?
      ;;
    *)
      echo "Phase B SKIPPED: job is writing but no canonical_plan bookUUID resolved (got: '${GEN_BOOK_UUID}')" >&2
      ;;
  esac
else
  echo "Phase B not chained (predicate not passing yet, or MO_GOAL_CHAIN_PHASE_B=0) — re-run this launcher; waves resume."
fi

echo "=== DONE rc=${rc} ==="
