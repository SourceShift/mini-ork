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
export PGHOST="${PGHOST:-REDACTED-INTERNAL-IP}" PGPORT="${PGPORT:-5932}" PGUSER="${PGUSER:-researcher_user}" PGDATABASE="${PGDATABASE:-researcher_db}"

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
# CLOSED-LOOP verification ownership: the code-fix child's in-sandbox reviewer is
# redundant AND evidence-starved here (verifiers are scoped to echo-stubs below,
# and the real gate is downstream: deploy -> regen -> DB flip). Without this, a
# needs_revision reviewer escalates to rollback's revert_branch and DESTROYS the
# child's verified edit before goal_apply can deploy it. Keep the worktree edit;
# let the OUTER loop (regen + GRAO quarantine + divergence-kill) be the gate.
export MINI_ORK_ROLLBACK_KEEP_WORKTREE="${MINI_ORK_ROLLBACK_KEEP_WORKTREE:-1}"

# ── grandchild verifiers: REAL, scoped to the child's own diff ──
# These used to be `echo` stubs, which made both of the child's mechanical
# gates pass by construction and left the LLM reviewer as the only in-sandbox
# check on a patch. Pointing them straight at the project's tsc/jest is the
# opposite failure: the researcher tree is ~17k files across three tsconfigs
# with pre-existing diagnostics, so a whole-repo run would redden every wave
# for reasons the child did not cause. binding/scoped_gate.py takes the middle
# path — typecheck the changed files through a tsconfig that EXTENDS the
# matching project config (aliases and strictness intact, scope narrowed), and
# run only the jest tests related to the changed files. Both are no-ops when
# the diff has no in-scope TypeScript, and a jest-guard load refusal
# (rc=77) is a skip, not a red.
export MINI_ORK_TYPECHECK_CMD="python3 $BIND/scoped_gate.py typecheck"
export MINI_ORK_TEST_CMD="python3 $BIND/scoped_gate.py test"
# Committed work counts as in-scope too (the child may commit before the gate).
export MO_GOAL_SCOPED_BASE="${MO_GOAL_SCOPED_BASE:-origin/main}"

# ── APPLY / DEPLOY / RE-DISPATCH stage (goal_apply node) ──
export MO_GOAL_APPLY=1
# SAFE default: dry. Flip to 0 at the go/no-go to run the real prod push + spend.
export MO_GOAL_APPLY_DRY="${MO_GOAL_APPLY_DRY:-1}"
# INSTRUMENT GUARD (goal_apply_deploy): the fix child edits the researcher tree,
# which is ALSO where the goal predicate's inputs are written — `committed_complete`
# and `rubric_status` are columns produced by server/services/bookGeneration/**, and
# the child's scope_gate is a task_class allowlist, not a path filter. So without
# this, a child that cannot make a chapter pass can make the chapter's PASS MEANING
# cheaper instead, and the deploy stage would ship that edit to the live worker.
# Git is asked, per glob, whether the worktree differs from HEAD at any of these
# paths; if so the deploy is REFUSED (nothing shipped) and the wave report carries
# the offending paths. Set MO_GOAL_PROTECTED_MODE=warn to record without refusing.
# Note the DAG runtime the real fix touches (server/compose/verifiedArtifact/
# verifiedArtifactDagRuntime.ts) is deliberately NOT listed: generation stays
# fixable, only the scoring decision is frozen. The list is the WRITERS of the
# predicate's own inputs plus the definition of the bar, nothing wider — a
# refusal is meant as a receipt on a genuine edge case (the loop decides it must
# change the gate to make a chapter pass), not a blanket ban. When a wave really
# does need one of them, look at the named path, then widen this list or set
# MO_GOAL_PROTECTED_MODE=warn for that run.
export MO_GOAL_PROTECTED_PATHS="${MO_GOAL_PROTECTED_PATHS:-
server/services/bookGeneration/bookRubricResultsService.ts
server/services/bookGeneration/c10CompletionGate.ts
server/services/bookGeneration/chapterRecheckService.ts
server/services/bookGeneration/committedCompleteReconciler.ts
server/services/bookGeneration/rubricBackstopWorker.ts
server/services/book-generation/bookPersistence.ts
server/services/bookGeneration/postGen.ts
server/services/bookGeneration/bookRubric.ts
server/services/bookGeneration/chapterRubric.ts
server/database/migrations/*geval*
server/database/migrations/*rubric*
}"
export MO_GOAL_PROTECTED_MODE="${MO_GOAL_PROTECTED_MODE:-refuse}"
# INDEPENDENT QUALITY ANCHOR (chapter predicate): rubric_status='pass' is the
# researcher's OWN G-Eval judge flag, written by code inside the child's editable
# tree — a self-report with nothing outside the judged system contradicting it.
# This adds one: chapter_quality.py is a deterministic vacuity floor over the
# bytes the commit path actually persisted (no sections / too few / too short /
# unhashed / mixed doc_version / duplicated H2 / unresolved placeholder markers).
# It lives in mini-ork's binding dir, so the fix child cannot reach it. Pass
# requires committed AND rubric AND this. MO_GOAL_QUALITY_MODE=warn records the
# verdict in the predicate's reason without letting it flip the result.
export MO_GOAL_QUALITY_CMD="${MO_GOAL_QUALITY_CMD:-python3 $BIND/chapter_quality.py}"
export MO_GOAL_QUALITY_MODE="${MO_GOAL_QUALITY_MODE:-enforce}"
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
# FAIL-FAST: the await is otherwise blind to failure — it waits the full window
# even when the regen has terminally failed, so one bad deploy costs 90min before
# the loop can react. This detector returns 0 the instant the chapter is
# permanently_failed (always) OR — opt-in via the stall knobs below — orphaned in
# 'generating' with a fresh last_error AND no verified-artifact run-dir activity
# for MO_GOAL_STALL_SECONDS. Run-dir freshness (not lifecycle.updated_at, which
# stays frozen at the last milestone during a HEALTHY long generation) is the
# only reliable "a node is still running" signal, so the stall check keys on it.
export MO_GOAL_TERMINAL_FAIL_CMD="${MO_GOAL_TERMINAL_FAIL_CMD:-python3 $BIND/chapter_terminal_fail.py}"
# Vendored per-worktree runs dir the book-gen worker writes each node's
# verified-artifact run into — the freshness source for the stall check.
export MO_GOAL_RUNS_DIR="${MO_GOAL_RUNS_DIR:-$WT/.mini-ork/runs}"
# 0 disables stall detection (permanently_failed still fires). 1200s = 20min with
# no node activity while 'generating'+errored ⇒ orphaned. Node cadence is ~1-2min,
# so 20min is a wide safety margin against killing a merely-slow node.
export MO_GOAL_STALL_SECONDS="${MO_GOAL_STALL_SECONDS:-1200}"
# CROSS-WAVE PATIENCE: this loop attempts ONE chapter per wave (a single code-fix
# child on the shared researcher bug) against 10 units. The failing SET therefore
# cannot shrink until a whole chapter lands, so the historical 2-wave give-up
# window (divergence on an unchanged failing set; GRAO quarantine on 2 identical
# fix-hashes) killed the campaign at wave 2 while 9 chapters sat untouched. The
# driver now folds each unit's failure FINGERPRINT (status + failing node, from
# goal-state.json) into the wave signature and scopes quarantine to the units a
# wave actually attempted (sweep fan-out), so a fix that MOVES a chapter's failure
# reads as progress. These windows give the code-fix child several waves to land a
# real researcher fix before the loop declares a unit hopeless. Default 2 (the
# framework's all-units-per-wave contract) is too eager for the book loop.
export MO_GOAL_DIVERGENCE_PATIENCE="${MO_GOAL_DIVERGENCE_PATIENCE:-6}"
export MO_GOAL_QUARANTINE_PATIENCE="${MO_GOAL_QUARANTINE_PATIENCE:-6}"

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
