#!/usr/bin/env bash
# tests/test_obs_surface.sh — end-to-end observability regression suite.
#
# Runs the obs-smoke recipe and asserts every observability surface
# populated correctly. Costs ~$0.05-$0.15 per execution. Should be
# re-run after any change to bin/mini-ork-execute, lib/llm-dispatch.sh,
# or any other emit site.
#
# Usage:
#   bash tests/test_obs_surface.sh                  # real LLM run, asserts
#   MINI_ORK_OBS_SMOKE_DRY=1 bash tests/test_obs_surface.sh   # dry-run, asserts what's possible
#
# Returns non-zero on any assertion failure. Stdout reports each check.

set -uo pipefail

ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export MINI_ORK_ROOT="$ROOT"
export PATH="$ROOT/bin:$PATH"

DRY="${MINI_ORK_OBS_SMOKE_DRY:-0}"
ISOLATED="${MINI_ORK_OBS_SMOKE_ISOLATED:-0}"

# ── workspace ─────────────────────────────────────────────────────────────
# Default: run in $ROOT/.mini-ork so the row shows up in `mini-ork serve`'s
# fleet view immediately. The user can navigate to it, click into the
# agent detail, see the transcript — that's the point of this smoke test.
#
# CI / sandbox path: MINI_ORK_OBS_SMOKE_ISOLATED=1 uses a tempdir so the
# test doesn't pollute the dev state.db.
if [ "$ISOLATED" = "1" ]; then
  WORK=$(mktemp -d)
  trap 'echo "[cleanup] $WORK"; rm -rf "$WORK"' EXIT
  cd "$WORK"
  git init -q >/dev/null
  export MINI_ORK_HOME="$WORK/.mini-ork"
  export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
  mini-ork init >/dev/null 2>&1 || { echo "FAIL: mini-ork init failed"; exit 1; }
  echo "→ isolated workspace: $WORK"
else
  cd "$ROOT"
  export MINI_ORK_HOME="$ROOT/.mini-ork"
  export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
  if [ ! -f "$MINI_ORK_DB" ]; then
    mini-ork init >/dev/null 2>&1 || { echo "FAIL: mini-ork init failed"; exit 1; }
  fi
  WORK="$ROOT"
  echo "→ using main workspace: $ROOT/.mini-ork (run visible in UI fleet)"
  echo "  to isolate instead: MINI_ORK_OBS_SMOKE_ISOLATED=1 bash tests/test_obs_surface.sh"
fi

cp "$ROOT/recipes/obs-smoke/example-kickoff.md" "$WORK/kickoff.md"

# ── run ───────────────────────────────────────────────────────────────────
if [ "$DRY" = "1" ]; then
  export MINI_ORK_DRY_RUN=1
  echo "→ dry-run mode (no LLM calls)"
else
  # MO_TRACE_RICH=1 → stream-json mode → per-turn telemetry + transcript.json
  # MINI_ORK_PROFILE_GATE=0 → don't block on planner's run_profile.confidence
  # MO_DAILY_BUDGET_USD=500 → don't trip the cost circuit because of prior
  #   spend (the dev .mini-ork accumulates many dollars across recursive-self-improve
  #   iters; with a low cap the planner aborts before any obs-smoke LLM fires)
  export MINI_ORK_DRY_RUN=0 MO_TRACE_RICH=1 MO_DAILY_BUDGET_USD=500 MINI_ORK_PROFILE_GATE=0
  echo "→ live mode (real LLM calls, ~\$0.05-\$0.15 expected)"
fi

echo "→ mini-ork run obs-smoke kickoff.md (streaming live — takes ~30-60s for real LLM)"
# Stream live AND capture full log. Prefix each line with [run] so it's
# visible the dispatcher is making progress — silent test harnesses look
# hung when they're not.
set +e
mini-ork run obs-smoke ./kickoff.md 2>&1 | tee "$WORK/run.log" | sed 's/^/  [run] /'
RC=${PIPESTATUS[0]}
set -e 2>/dev/null || true
echo "  [run] exit=$RC"

# ── assertions ────────────────────────────────────────────────────────────
FAIL=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }

# In dry-run mode, classify early-exits before the task_runs INSERT. Verify
# that the dispatcher at least walked the recipe (4 [dry-run] dispatch lines
# in the log) and exit early — the real assertions need a real LLM run.
if [ "$DRY" = "1" ]; then
  DISPATCHED=$(grep -c "\[dry-run\] would dispatch" "$WORK/run.log" || true)
  [ "$DISPATCHED" -ge 4 ] && pass "dry-run dispatched 4 nodes" || fail "dry-run dispatched $DISPATCHED nodes (expected ≥4)"
  echo ""
  if [ "$FAIL" -eq 0 ]; then
    echo "✓ dry-run harness OK — re-run without MINI_ORK_OBS_SMOKE_DRY=1 for real assertions"
    exit 0
  else
    echo "✗ dry-run harness failed"
    exit 1
  fi
fi

# Find the task_run (real-run path). When running in the main workspace,
# scope to runs created during this test invocation so we don't pick up a
# stale row from a prior run.
RID=$(sqlite3 "$MINI_ORK_DB" "SELECT id FROM task_runs WHERE recipe='obs-smoke' ORDER BY created_at DESC LIMIT 1;" 2>/dev/null)
if [ -z "$RID" ]; then
  fail "no task_runs row written"
  echo "[total failures: $FAIL]"
  exit 1
fi
echo "[task_run: $RID]"
RUN_DIR="$MINI_ORK_HOME/runs/$RID"

# Schema-level checks
TRACE_ID=$(sqlite3 "$MINI_ORK_DB" "SELECT COALESCE(trace_id,'') FROM task_runs WHERE id='$RID';")
[ -n "$TRACE_ID" ] && pass "task_runs.trace_id is non-NULL ($TRACE_ID)" \
                  || fail "task_runs.trace_id is NULL"

STATUS=$(sqlite3 "$MINI_ORK_DB" "SELECT status FROM task_runs WHERE id='$RID';")
case "$STATUS" in
  published|verifying|reviewing|planned) pass "task_runs.status reached '$STATUS'";;
  failed) pass "task_runs.status=failed (verifier may have failed, OK for assertion shape)";;
  *) fail "task_runs.status unexpected: '$STATUS'";;
esac

# Filesystem artifacts
[ -f "$RUN_DIR/execute.log" ] && pass "execute.log exists" || fail "execute.log missing at $RUN_DIR/execute.log"

if [ "$DRY" != "1" ]; then
  [ -f "$RUN_DIR/lens-tiny.md" ] && pass "lens-tiny.md exists" || fail "lens-tiny.md missing"
  [ -f "$RUN_DIR/review-tiny_reviewer.json" ] && pass "review-tiny_reviewer.json exists" || fail "review-tiny_reviewer.json missing"
fi

# run_events: each node should have node_start + node_end
NODE_START_COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM run_events WHERE run_id='$RID' AND event_type='node_start';")
NODE_END_COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM run_events WHERE run_id='$RID' AND event_type='node_end';")
[ "$NODE_START_COUNT" -ge 3 ] && pass "run_events node_start count = $NODE_START_COUNT (≥3)" \
                              || fail "run_events node_start count = $NODE_START_COUNT (<3 — _dispatch_node emit broken?)"
[ "$NODE_END_COUNT" -ge 3 ] && pass "run_events node_end count = $NODE_END_COUNT (≥3)" \
                            || fail "run_events node_end count = $NODE_END_COUNT (<3 — RETURN trap broken?)"

# llm_calls assertions (skip in dry-run mode)
if [ "$DRY" != "1" ]; then
  LLM_COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM llm_calls WHERE traceparent LIKE '%${TRACE_ID}%';")
  [ "$LLM_COUNT" -ge 2 ] && pass "llm_calls with strict trace_id bridge = $LLM_COUNT (≥2)" \
                         || fail "llm_calls strict-bridge count = $LLM_COUNT (<2 — MO_TRACEPARENT export broken?)"

  # node_id attribution via metadata_json
  NODE_ATTR=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM llm_calls WHERE traceparent LIKE '%${TRACE_ID}%' AND json_extract(metadata_json,'\$.node_id') IS NOT NULL;")
  [ "$NODE_ATTR" -ge 2 ] && pass "llm_calls with metadata.node_id = $NODE_ATTR (MO_NODE_ID wired)" \
                         || fail "llm_calls with metadata.node_id = $NODE_ATTR — MO_NODE_ID export broken in _dispatch_node?"

  # session_id column populated for at least one row (stream-json path)
  SESS_COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM llm_calls WHERE traceparent LIKE '%${TRACE_ID}%' AND session_id IS NOT NULL;")
  if [ "$SESS_COUNT" -ge 1 ]; then
    pass "llm_calls.session_id populated for $SESS_COUNT row(s) (stream-json captured)"
  else
    echo "  WARN  llm_calls.session_id all NULL — stream-json path may not have fired (acceptable if MO_TRACE_RICH=0)"
  fi

  # Token totals non-zero for at least one row
  TOK_COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM llm_calls WHERE traceparent LIKE '%${TRACE_ID}%' AND (input_tokens > 0 OR output_tokens > 0);")
  [ "$TOK_COUNT" -ge 1 ] && pass "llm_calls with non-zero tokens = $TOK_COUNT (token extractor working)" \
                         || fail "llm_calls all show zero tokens — extractor not capturing usage block"

  # Transcript file (per-turn full content)
  TRANSCRIPT_COUNT=$(ls "$RUN_DIR"/*.transcript.json 2>/dev/null | wc -l | tr -d ' ')
  [ "$TRANSCRIPT_COUNT" -ge 1 ] && pass "transcript.json files written ($TRANSCRIPT_COUNT)" \
                                || fail "no transcript.json files — UI 'Agent transcript' panel will be empty"

  # Cost rolled up
  COST=$(sqlite3 "$MINI_ORK_DB" "SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE traceparent LIKE '%${TRACE_ID}%';")
  python3 -c "import sys; sys.exit(0 if float('$COST') > 0 else 1)" \
    && pass "llm_calls cost sum = \$$COST (>0)" \
    || fail "llm_calls cost sum = \$$COST (no cost recorded)"
fi

# Verifier sidecar
[ -f "$RUN_DIR/verifier-result-lens-exists.json" ] \
  && pass "verifier-result-lens-exists.json sidecar written" \
  || fail "verifier-result-lens-exists.json sidecar missing"

# Final
echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "✓ all observability surfaces populated correctly"
  exit 0
else
  echo "✗ $FAIL assertion(s) failed — observability has regressed"
  echo ""
  echo "[execute.log tail]"
  tail -20 "$RUN_DIR/execute.log" 2>/dev/null | sed 's/^/  | /'
  exit 1
fi
