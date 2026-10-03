#!/bin/bash
# SDD build driver — serial K1→K4 through recursive-validate-impl.
# Run from the worktree root under tmux: tmux new -s sdd-build
set -uo pipefail

WT=/Users/admin/ps/mini-ork-sdd-wt
export MINI_ORK_ROOT="$WT"
export MINI_ORK_HOME="$WT/.mini-ork"
export MO_TARGET_CWD="$WT"
export MO_ALLOW_FRAMEWORK_CWD=1
export MO_TIER4_QUORUM=2          # minimax lens dead (token plan exhausted 2026-10-03)
export MINI_ORK_PROFILE_GATE=0    # kickoffs carry ## Verification command; skip Q&A gate
cd "$WT" || exit 1                # dispatch from repo root, never inside .mini-ork/

LOG="$WT/kickoffs/sdd/build.log"
echo "=== SDD build start $(date -u +%FT%TZ) ===" | tee -a "$LOG"

for K in k1-specdir-ingestion k2-recipe-scaffold k3-verifiers k4-e2e-fixture-eval; do
  export MINI_ORK_RUN_ID="run-sdd-$K-$(date +%Y%m%d%H%M)"
  echo "--- dispatch $K as $MINI_ORK_RUN_ID $(date -u +%FT%TZ)" | tee -a "$LOG"
  "$WT/bin/mini-ork" run --json recursive-validate-impl "$WT/kickoffs/sdd/$K.md" \
    > "$WT/kickoffs/sdd/$K.result.json" 2>> "$LOG"
  rc=$?
  echo "--- $K rc=$rc" | tee -a "$LOG"
  if [ $rc -ne 0 ]; then
    echo "!!! $K failed — stopping chain. Inspect runs/$MINI_ORK_RUN_ID/ and $K.result.json" | tee -a "$LOG"
    exit $rc
  fi
  # Hard evidence gate between kickoffs: the success command must pass on the tree.
  case $K in
    k1-specdir-ingestion) python3 -m pytest -q tests/test_specdir_ingest.py >> "$LOG" 2>&1 || { echo "!!! $K evidence gate failed" | tee -a "$LOG"; exit 65; } ;;
    k2-recipe-scaffold)   python3 -c "import yaml,pathlib; yaml.safe_load(pathlib.Path('recipes/spec-driven-delivery/workflow.yaml').read_text())" >> "$LOG" 2>&1 || { echo "!!! $K evidence gate failed" | tee -a "$LOG"; exit 65; } ;;
    k3-verifiers)         python3 -m pytest -q tests/test_sdd_verifiers.py >> "$LOG" 2>&1 || { echo "!!! $K evidence gate failed" | tee -a "$LOG"; exit 65; } ;;
    k4-e2e-fixture-eval)  python3 -m pytest -q tests/test_sdd_e2e_dryrun.py >> "$LOG" 2>&1 || { echo "!!! $K evidence gate failed" | tee -a "$LOG"; exit 65; } ;;
  esac
  echo "--- $K evidence gate PASSED" | tee -a "$LOG"
done
echo "=== SDD build chain COMPLETE $(date -u +%FT%TZ) ===" | tee -a "$LOG"
