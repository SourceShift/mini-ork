#!/usr/bin/env bash
# recursive-migrate.sh — drive mini-ork's own framework-edit recursively over the
# remaining bash libs, one bounded subsystem at a time, with hard safety gates.
#
# Per lib:  framework-edit run -> harvest the implementer worktree diff ->
#           verify pytest-green on a scratch apply -> land (commit) -> engine
#           health check (doctor + collect).  HALT on: no diff, tests red, or a
#           broken engine (never compound a break across a self-migration).
#
# Excludes the highest-risk runtime-core (llm-dispatch 2000L, decision_service
# routing-brain, context_assembler no-port) — those need reviewed handling, not
# blind auto-land.  Detached; logs to $LOG.  Cost is bounded by framework-edit's
# own $50/day circuit.
set -uo pipefail
cd /Volumes/docker-ssd/ps/mini-ork
export MINI_ORK_ROOT="$PWD" MINI_ORK_HOME="$PWD/.mini-ork" MO_TARGET_CWD="$PWD"
export MO_ALLOW_FRAMEWORK_CWD=1 MINI_ORK_PROFILE_GATE=0 MINI_ORK_DRY_RUN=0
LOG="${1:?usage: recursive-migrate.sh <logfile>}"
BRANCH="$(git branch --show-current)"

log(){ printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >> "$LOG"; }

# Bounded, self-contained libs whose ports are verifiable by unit tests alone.
# Ordered smallest-first. The 3 runtime-core giants are deliberately NOT here.
LIBS="cost_pause gate_bootstrap lane-helpers gate_registry gates_common circuit_breaker \
db_open config_resolve cw_por deadline_budget benchmark_suite promotion_gate \
trace_store utility_function version_registry \
process_reward policy_store lane_router coord_gate coord_registry"

log "=== recursive-migrate START on $BRANCH — $(echo $LIBS | wc -w | tr -d ' ') candidate libs ==="

for lib in $LIBS; do
  mod="${lib//-/_}"
  sh="lib/${lib}.sh"
  [ -f "$sh" ] || { log "SKIP $lib — already gone"; continue; }
  [ -f "mini_ork/ported/${mod}.py" ] || { log "SKIP $lib — no Python port module"; continue; }

  # SAFETY PRE-CHECK: is this lib shelled-out to by the Python engine at runtime?
  # If so, retiring the bash requires rewiring those callers (out of this driver's
  # scope) — skipping avoids breaking the live engine.
  RTS=$(grep -rlnE "(source|bash -c|subprocess|Popen|check_output)[^#]*(${lib}\.sh|/lib/${lib}\.sh)" mini_ork/ --include='*.py' 2>/dev/null | grep -v '/test' | grep -v "ported/${mod}.py" | head -3 | tr '\n' ' ')
  if [ -n "$RTS" ]; then log "SKIP $lib — RUNTIME-SHELL-OUT (needs caller-rewiring): $RTS"; continue; fi
  # Also skip if a bash test still sources it (needs its bash test ported first)
  if grep -rlnE "^\s*(source|\.)\s+[^#]*${lib}\.sh" tests/ --include='*.sh' 2>/dev/null | head -1 | grep -q .; then
     log "SKIP $lib — sourced by a bash test (needs test port first)"; continue; fi
  # Also skip if the benchmark corpus pins it
  if grep -rln "${lib}\.sh" benchmark/ 2>/dev/null | head -1 | grep -q .; then
     log "SKIP $lib — benchmark-pinned"; continue; fi

  log "--- $lib: writing kickoff ---"
  KICK="kickoffs/migration/port-${lib}.md"
  cat > "$KICK" <<EOF
# Port lib/${lib}.sh logic into the Python core (stop shelling out)

## Goal
\`mini_ork/ported/${mod}.py\` currently delegates to bash \`lib/${lib}.sh\` at runtime
(subprocess/source). Reimplement the bash logic natively in Python so the module no
longer shells out, then retire the bash file.

## Deliverable
- Port every public function of \`/Volumes/docker-ssd/ps/mini-ork/lib/${lib}.sh\` into
  \`/Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/${mod}.py\` as native Python (no
  \`subprocess\`/\`bash -c\`/\`source\` of the bash file). Preserve exact behavior + return
  contracts. Keep the public API identical so callers are unchanged.
- Rewrite \`/Volumes/docker-ssd/ps/mini-ork/tests/unit/test_${mod}_py.py\` (if present) to
  a STANDALONE pytest that imports the port directly (no bash subprocess), matching or
  exceeding the prior coverage. pytest-green AND pyright-clean (0 errors).

## Acceptance criteria
- \`grep -nE 'subprocess|bash -c|source .*${lib}' mini_ork/ported/${mod}.py\` finds no
  runtime shell-out of the bash file.
- \`python3 -m pytest tests/unit/test_${mod}_py.py -q\` passes.
- Existing tests stay green.

## Files in scope
- /Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/${mod}.py   (reimplement natively)
- /Volumes/docker-ssd/ps/mini-ork/lib/${lib}.sh                (logic source; read only)
- /Volumes/docker-ssd/ps/mini-ork/tests/unit/test_${mod}_py.py (standalone test)
EOF

  RUNID="run-mig-${lib}-$(date +%s)"
  log "$lib: framework-edit run $RUNID (may take ~30m)"
  MINI_ORK_RUN_ID="$RUNID" timeout 3000 "$MINI_ORK_ROOT/bin/mini-ork" run framework-edit "$KICK" >> "$LOG" 2>&1 || log "$lib: run exited non-zero (framework-edit verdict gap is expected)"

  # Harvest: find the implementer worktree that changed our port module
  WT=$(git worktree list --porcelain 2>/dev/null | awk '/^worktree /{w=$2} /^branch/{if(w) print w}' | while read w; do
        git -C "$w" diff --quiet HEAD -- "mini_ork/ported/${mod}.py" 2>/dev/null || echo "$w"; done | head -1)
  if [ -z "$WT" ]; then log "$lib: NO worktree changed the port module — SKIP (framework-edit produced nothing)"; git checkout -- "$KICK" 2>/dev/null; rm -f "$KICK"; continue; fi
  log "$lib: harvesting from $WT"

  # Verify on a scratch: apply the worktree's port module + test, run pytest
  cp "$WT/mini_ork/ported/${mod}.py" "mini_ork/ported/${mod}.py"
  [ -f "$WT/tests/unit/test_${mod}_py.py" ] && cp "$WT/tests/unit/test_${mod}_py.py" "tests/unit/test_${mod}_py.py"
  if ! python3 -m pytest "tests/unit/test_${mod}_py.py" -q >> "$LOG" 2>&1; then
     log "$lib: port tests RED — reverting, SKIP"; git checkout -- "mini_ork/ported/${mod}.py" "tests/unit/test_${mod}_py.py" 2>/dev/null; rm -f "$KICK"; continue
  fi
  if ! python3 -m pyright "mini_ork/ported/${mod}.py" 2>&1 | grep -q '0 errors'; then
     log "$lib: pyright NOT clean — reverting, SKIP"; git checkout -- "mini_ork/ported/${mod}.py" "tests/unit/test_${mod}_py.py" 2>/dev/null; rm -f "$KICK"; continue
  fi
  # confirm it actually stopped shelling out
  if grep -qE "subprocess|bash -c|source .*${lib}\.sh|/lib/${lib}\.sh" "mini_ork/ported/${mod}.py"; then
     log "$lib: port STILL shells out to bash — reverting, SKIP"; git checkout -- "mini_ork/ported/${mod}.py" "tests/unit/test_${mod}_py.py" 2>/dev/null; rm -f "$KICK"; continue
  fi

  # Land: retire the bash + commit (explicit pathspec)
  git rm -q "$sh" 2>/dev/null || rm -f "$sh"
  git add "mini_ork/ported/${mod}.py" "tests/unit/test_${mod}_py.py" "$sh" 2>/dev/null
  git commit -q -m "refactor(migrate): port lib/${lib}.sh into Python — stop shelling out

Native Python reimplementation in mini_ork/ported/${mod}.py (was a bash shell-out
wrapper); standalone test; bash retired. Authored by mini-ork framework-edit ($RUNID),
harvested + verified by scripts/recursive-migrate.sh." \
    -- "mini_ork/ported/${mod}.py" "tests/unit/test_${mod}_py.py" "$sh" >> "$LOG" 2>&1
  rm -f "$KICK"
  log "$lib: LANDED ✓ ($(git rev-parse --short HEAD))"

  # ENGINE HEALTH GATE — real dry-run exercises the execute path (catches a
  # retired lib that's still sourced at runtime). Halt on any break.
  if ! python3 -m pytest tests/unit --co -q >> "$LOG" 2>&1; then
     log "!!! $lib: test collection BROKE after landing — HALTING"; break; fi
  if ! MINI_ORK_DRY_RUN=1 timeout 180 "$MINI_ORK_ROOT/bin/mini-ork" run examples/01-hello-world/kickoff.md >> "$LOG" 2>&1; then
     log "!!! $lib: DRY-RUN of the loop BROKE after landing (retired lib still sourced?) — HALTING"; break; fi
  log "$lib: engine health OK (dry-run passed) — continuing"
done

log "=== recursive-migrate DONE — lib/*.sh remaining: $(ls lib/*.sh 2>/dev/null | wc -l | tr -d ' ') ==="
