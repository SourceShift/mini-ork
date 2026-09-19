#!/usr/bin/env bash
# CLOSED-LOOP autonomous RSI driver over live book d0df3cdb, WITH the deploy +
# re-dispatch stage armed. This is the base drive.py wave PLUS the goal_apply node
# (recipes/goal-loop/lib/transforms.py::goal_apply_deploy): after a fix child edits
# the researcher worktree, the wave DEPLOYS it (push worktree HEAD -> origin/main),
# waits for the worker to pick up the new code, RE-DISPATCHES the failing chapter
# through researcher's sanctioned forceResumeJob path, then awaits the regen so
# goal_check sees the DB flip in the SAME wave (sidestepping the divergence-kill).
#
# SAFE BY DEFAULT: MO_GOAL_APPLY_DRY defaults to 1 — the goal_apply node records the
# deploy/redispatch PLAN without executing the irreversible prod push or spend.
# The single go/no-go to run it for real is:
#
#     MO_GOAL_APPLY_DRY=0 bash kickoffs/book-goal-loop/launch-armed.sh
#
# That, and only that, pushes to libwit PRODUCTION main (~6-8min deploy, ALL books)
# and burns a real 30-90min regen lane. A running researcher book-generation worker
# is REQUIRED (forceResumeJob's readiness preflight refuses otherwise).
set -uo pipefail

# ── engine paths ──
export MINI_ORK_ROOT=/Volumes/docker-ssd/ps/mini-ork
export MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork
export MINI_ORK_SECRETS=/Volumes/docker-ssd/ps/mini-ork/config/secrets.local.sh
export MINI_ORK_DB="${MINI_ORK_HOME}/state.db"

# ── pinned run dir: driver READ == wave-child WRITE ──
TS=$(date +%Y%m%d-%H%M%S)
export MINI_ORK_RUN_ID="goalloop-armed-${TS}"
export MINI_ORK_RUN_DIR="${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}"
mkdir -p "${MINI_ORK_RUN_DIR}"

# ── researcher targets ──
#   WT  = the worktree the fix children EDIT and the deploy pushes FROM.
#   PRIMARY = a built checkout (node_modules + server/.env) the redispatch's
#             sanctioned forceResumeJob CLI runs from (read/execute only).
WT=/Volumes/docker-ssd/Migration/Development/worktrees/goalloop-fixes
PRIMARY=/Volumes/docker-ssd/Migration/Development/researcher
export MO_GOAL_TARGET_CWD="$WT"
export MO_TARGET_CWD="$WT"
export MINI_ORK_TARGET_REPO="$WT"
export MO_RESEARCHER_DIR="$PRIMARY"

# ── book/goal inputs (live Postgres) ──
# NO secret in this file (mini-ork origin is a PUBLIC repo). Export PGPASSWORD in
# your shell or source it from MINI_ORK_SECRETS before running. Host/port/user are
# operator-overridable and default to the researcher instance via the env below.
export BOOK_UUID="${BOOK_UUID:-d0df3cdb-8164-450e-b841-2c9354ea0423}"
: "${PGPASSWORD:?export PGPASSWORD (researcher DB) before running — never hardcode it here}"
export PGHOST="${PGHOST:-100.74.239.22}" PGPORT="${PGPORT:-5932}" PGUSER="${PGUSER:-researcher_user}" PGDATABASE="${PGDATABASE:-researcher_db}"

# ── wave + child kickoffs / goal inputs (wave nodes read these from ENV) ──
BIND="${MINI_ORK_ROOT}/kickoffs/book-goal-loop/binding"
export MO_GOAL_WAVE_KICKOFF="$BIND/wave-kickoff.md"
export MO_GOAL_CHILD_KICKOFF="$BIND/child-kickoff.md"
export MO_GOAL_UNITS_CMD="bash $BIND/list_chapters.sh"
export MO_GOAL_PREDICATE_CMD="python3 $BIND/chapter_predicate.py"
# DEEP evidence (optional but on by default here): run ONCE per wave for only the
# selected fix units, capturing the real failure picture (untruncated last_error,
# the artifact the lane produced, produced-vs-required heading delta) so the fix
# child diagnoses root cause instead of guessing from the 80-char predicate slice.
# The goal-loop persists it to <run_dir>/evidence/<slug>.md and templates it into
# the child kickoff as {{evidence}} / {{evidence_path}}. Unset to fall back to reason.
export MO_GOAL_EVIDENCE_CMD="${MO_GOAL_EVIDENCE_CMD:-python3 $BIND/harvest_evidence.py}"
export MO_GOAL_CHILD_RECIPE=code-fix
export MO_GOAL_MAX_CHILDREN_PER_WAVE="${MO_GOAL_MAX_CHILDREN_PER_WAVE:-1}"

# ── recipe / loop knobs ──
export MO_STATIC_RECIPE_PLAN=1
export MINI_ORK_PROFILE_GATE=0
export MINI_ORK_RECURSIVE_MAX_PARALLEL=1
export MINI_ORK_ALLOW_CHILD_SPAWN=1
# 0 = spawn a REAL fix child (LLM spend); 1 = scaffold-only (~$0 wiring proof).
export MO_GOAL_NO_EXECUTE="${MO_GOAL_NO_EXECUTE:-0}"

# ── grandchild verifiers scoped OFF the 17k-file researcher build ──
export MINI_ORK_TYPECHECK_CMD='echo scoped-typecheck-skipped-bounded-goal-loop-demo'
export MINI_ORK_TEST_CMD='echo scoped-test-skipped-bounded-goal-loop-demo'

# ── APPLY / DEPLOY / RE-DISPATCH stage (goal_apply node) ──
export MO_GOAL_APPLY=1
# SAFE default: dry. Flip to 0 at the go/no-go to run the real prod push + spend.
export MO_GOAL_APPLY_DRY="${MO_GOAL_APPLY_DRY:-1}"
# Deploy MODE — how the verified fix becomes LIVE to the running worker. The
# loop OWNS the worker lifecycle: "restart the worker if needed" is this stage.
#   local-worker (DEFAULT — reversible, dev-scoped, NO prod push): restart the
#     book-generation worker FROM THE WORKTREE (binding/restart_worker.py) so it
#     runs the W15-fixed code. book-gen is a Redis-SETNX singleton, so this
#     SIGTERMs the incumbent (graceful shutdown releases the lock first), then
#     starts a replacement from MO_GOAL_TARGET_CWD sharing the primary's
#     .env + node_modules. Proves the fix closes the loop before it ever touches
#     all-books prod; costs one book's regen spend only.
#   prod-push (irreversible, ALL books): ff-reconcile the worktree to origin/main
#     (worktree HEAD trails origin — recon behind=4 ahead=0 — so a raw push is a
#     non-ff REJECT; the ff-only merge preserves the uncommitted W15 change since
#     origin never touched verifiedArtifactDagRuntime.ts), stage ONLY the W15
#     file (never `git add -A` — it sweeps generated baml_client/), commit
#     idempotently, push HEAD -> origin/main; the k8s deploy rebuilds the worker.
export MO_GOAL_DEPLOY_MODE="${MO_GOAL_DEPLOY_MODE:-local-worker}"
if [ "$MO_GOAL_DEPLOY_MODE" = "prod-push" ]; then
  export MO_GOAL_APPLY_CMD='git fetch origin main && git merge --ff-only origin/main && git add server/compose/verifiedArtifact/verifiedArtifactDagRuntime.ts && { git diff --cached --quiet || git commit -m "fix(compose): bind W15 static-lens title-pin (goal-loop autonomous wave)"; } && git push origin HEAD:main'
else
  export MO_GOAL_APPLY_CMD="python3 $BIND/restart_worker.py"
fi
# Re-dispatch: sanctioned forceResumeJob per failing chapter (idempotent per wave;
# resolves job_id from BOOK_UUID, no-ops when the run FSM already left 'failed').
export MO_GOAL_REDISPATCH_CMD="python3 $BIND/redispatch_chapter.py"
# Deploy settle: ff-sync (<=120s) + drain-aware worker restart (~6-8min). No
# AWAIT_DEPLOY poll wired for the first run — a fixed settle is simpler + safe.
export MO_GOAL_DEPLOY_SETTLE_SECONDS="${MO_GOAL_DEPLOY_SETTLE_SECONDS:-480}"
export MO_GOAL_DEPLOY_TIMEOUT_SECONDS="${MO_GOAL_DEPLOY_TIMEOUT_SECONDS:-900}"
# Await regen: wait up to 90min for the chapter to reach the goal predicate
# (TERMINAL_CMD defaults to PREDICATE_CMD = wait-for-pass), polling each minute.
export MO_GOAL_APPLY_AWAIT_SECONDS="${MO_GOAL_APPLY_AWAIT_SECONDS:-5400}"
export MO_GOAL_APPLY_POLL_SECONDS="${MO_GOAL_APPLY_POLL_SECONDS:-60}"

PY="${MINI_ORK_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY=python3.11

echo "=== goal-loop ARMED driver launch $(date -u +%FT%TZ) ==="
echo "RUN_ID=${MINI_ORK_RUN_ID}"
echo "RUN_DIR=${MINI_ORK_RUN_DIR}"
echo "WORKTREE=${WT}"
echo "MO_GOAL_APPLY_DRY=${MO_GOAL_APPLY_DRY}  (1=dry/no prod push, 0=LIVE prod push + spend)"
# Loop bounds — env-overridable so the same core drives a single bounded proof
# (defaults: 1 wave / \$45) OR a continuous many-wave campaign (the autonomous
# wrapper raises these). The flags always beat the recipe's declared recursion
# block, so whatever lands here is the enforced cap.
MAX_WAVES="${MO_GOAL_MAX_WAVES:-1}"
BUDGET_USD="${MO_GOAL_BUDGET_USD:-45}"
echo "MO_GOAL_MAX_WAVES=${MAX_WAVES}  MO_GOAL_BUDGET_USD=${BUDGET_USD}  MAX_CHILDREN_PER_WAVE=${MO_GOAL_MAX_CHILDREN_PER_WAVE}"
echo "MO_GOAL_EVIDENCE_CMD=${MO_GOAL_EVIDENCE_CMD}"
echo "=== driver output ==="

SD="${MINI_ORK_HOME}/goal-loop/book-d0df3cdb"
rm -f "$SD/goal-loop-state.json" "$SD/final-verdict.json"

cd "$MINI_ORK_ROOT"
"$PY" recipes/goal-loop/lib/drive.py \
  --goal-id book-d0df3cdb \
  --target-cwd "$WT" \
  --units-cmd "bash $BIND/list_chapters.sh" \
  --predicate-cmd "python3 $BIND/chapter_predicate.py" \
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
