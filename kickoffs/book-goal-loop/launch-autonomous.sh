#!/usr/bin/env bash
# FULLY-AUTONOMOUS, human-off continuous RSI driver for book d0df3cdb.
#
# Loops mini-ork's goal-loop until EVERY failing chapter reaches
# committed_complete=true + rubric_status=pass, with NO human in the loop. Each
# wave: find failing chapters -> harvest DEEP evidence -> dispatch a fix child
# that patches the researcher `server/` root cause -> DEPLOY it (restart the
# book-generation worker from the worktree) -> RE-DISPATCH the chapter -> await
# the DB flip -> re-check every chapter. drive.py's cross-wave loop handles
# convergence, divergence-kill, GRAO quarantine, and the budget rail.
#
# This is a thin wrapper over launch-armed.sh that supplies the human-off env
# (live apply, many waves, local-worker deploy) and daemonizes it, so a single
# invocation runs the whole campaign unattended:
#
#     bash kickoffs/book-goal-loop/launch-autonomous.sh
#
# SANCTIONED ENVELOPE: deploy defaults to `local-worker` — reversible and
# dev-scoped (it bounces the dev book-gen worker to run the fixed code; it never
# pushes to production). The one irreversible, all-books lever (`prod-push`)
# stays human-gated even here: it is refused unless I_UNDERSTAND_PROD_PUSH=1.
set -uo pipefail

ROOT=/Volumes/docker-ssd/ps/mini-ork
ARMED="$ROOT/kickoffs/book-goal-loop/launch-armed.sh"
RESEARCHER_ENV=/Volumes/docker-ssd/Migration/Development/researcher/server/.env

# ── resolve PGPASSWORD unattended: existing env -> secrets file -> researcher .env
export MINI_ORK_SECRETS="${MINI_ORK_SECRETS:-$ROOT/config/secrets.local.sh}"
if [ -z "${PGPASSWORD:-}" ] && [ -f "$MINI_ORK_SECRETS" ]; then
  # shellcheck disable=SC1090
  set -a; . "$MINI_ORK_SECRETS"; set +a
fi
if [ -z "${PGPASSWORD:-}" ] && [ -f "$RESEARCHER_ENV" ]; then
  PGPASSWORD="$(grep -E '^POSTGRES_PASSWORD=' "$RESEARCHER_ENV" | head -1 | cut -d= -f2- | tr -d '"'\''')"
  export PGPASSWORD
fi
: "${PGPASSWORD:?PGPASSWORD unresolved — put it in $MINI_ORK_SECRETS, export it, or ensure $RESEARCHER_ENV has POSTGRES_PASSWORD}"

# ── HUMAN-OFF loop policy (all overridable; these are the autonomous defaults) ──
export MO_GOAL_APPLY=1
export MO_GOAL_APPLY_DRY=0                                   # live deploy + redispatch + await
export MO_GOAL_DEPLOY_MODE="${MO_GOAL_DEPLOY_MODE:-local-worker}"   # reversible, dev-scoped
export MO_GOAL_NO_EXECUTE=0                                  # real fix children (LLM spend)
export MO_GOAL_MAX_WAVES="${MO_GOAL_MAX_WAVES:-24}"          # enough to walk 10 chapters + retries
export MO_GOAL_BUDGET_USD="${MO_GOAL_BUDGET_USD:-300}"       # total campaign rail
export MO_GOAL_MAX_CHILDREN_PER_WAVE="${MO_GOAL_MAX_CHILDREN_PER_WAVE:-1}"  # one worktree -> serial

# ── prod-push stays human-gated even in autonomous mode ──
if [ "$MO_GOAL_DEPLOY_MODE" = "prod-push" ] && [ "${I_UNDERSTAND_PROD_PUSH:-0}" != "1" ]; then
  {
    echo "REFUSING: prod-push pushes to PRODUCTION main for ALL books and is irreversible."
    echo "Autonomous mode is sanctioned for local-worker (reversible, dev-scoped) only."
    echo "To run prod-push deliberately: I_UNDERSTAND_PROD_PUSH=1 MO_GOAL_DEPLOY_MODE=prod-push bash $0"
  } >&2
  exit 3
fi

TS=$(date +%Y%m%d-%H%M%S)
LOG="$ROOT/.mini-ork/runs/autonomous-${TS}.log"
mkdir -p "$(dirname "$LOG")"

echo "=== launching FULLY-AUTONOMOUS human-off goal-loop $(date -u +%FT%TZ) ==="
echo "book        = ${BOOK_UUID:-d0df3cdb-8164-450e-b841-2c9354ea0423}"
echo "deploy_mode = $MO_GOAL_DEPLOY_MODE (reversible, dev-scoped — no prod push)"
echo "max_waves   = $MO_GOAL_MAX_WAVES   budget = \$$MO_GOAL_BUDGET_USD   children/wave = $MO_GOAL_MAX_CHILDREN_PER_WAVE"
echo "log         = $LOG"

# Daemonize: detach from this shell so the campaign survives terminal exit.
nohup bash "$ARMED" > "$LOG" 2>&1 < /dev/null &
PID=$!
disown "$PID" 2>/dev/null || true

echo "PID         = $PID"
echo "watch it    : tail -f $LOG"
echo "stop it     : kill $PID"
echo "final       : cat \$(dirname $LOG)/../goal-loop/book-d0df3cdb/final-verdict.json  # when it stops"
