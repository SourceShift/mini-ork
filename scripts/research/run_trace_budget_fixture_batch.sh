#!/usr/bin/env bash
# Run controlled docs/code_fix fixture batches for the trace-budget paper.
#
# The runner resets each fixture before every policy run. This preserves a fair
# starting state across policies and prevents one run's edit from leaking into
# the next run.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

EXP_ID="${EXP_ID:-trace-budget-fixtures-$(date +%Y%m%d-%H%M%S)}"
REPS="${REPS:-1}"
MANIFEST="${MANIFEST:-docs/research/experiments/${EXP_ID}-manifest.json}"
RESULTS_DIR="${RESULTS_DIR:-docs/research/experiments/results/${EXP_ID}}"
MO_FRONTIER_LANE="${MO_FRONTIER_LANE:-opus_lens}"
MO_CHEAP_LANE="${MO_CHEAP_LANE:-kimi_lens}"
POLICIES=(frontier_only cheap_only static_hybrid trace_governed)

DOC_BASE="docs/research/experiments/fixtures/docs/baselines"
DOC_WORK="docs/research/experiments/fixtures/docs/work"
DOC_KICKOFF="docs/research/experiments/fixtures/docs/kickoffs"
CODE_BASE="docs/research/experiments/fixtures/code_fix/baselines"
CODE_WORK="docs/research/experiments/fixtures/code_fix/work"
CODE_KICKOFF="docs/research/experiments/fixtures/code_fix/kickoffs"

mkdir -p "$(dirname "$MANIFEST")" "$RESULTS_DIR" "$DOC_WORK" "$CODE_WORK"

reset_docs_fixture() {
  local task_num="$1"
  mkdir -p "$DOC_WORK"
  cp "$DOC_BASE/doc-task-${task_num}.md" "$DOC_WORK/doc-task-${task_num}.md"
}

reset_code_fixture() {
  local task_num="$1"
  rm -rf "$CODE_WORK/task_${task_num}"
  mkdir -p "$CODE_WORK/task_${task_num}"
  cp "$CODE_BASE/task_${task_num}/"*.py "$CODE_WORK/task_${task_num}/"
}

python3 - "$EXP_ID" "$REPS" "$MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

exp_id = sys.argv[1]
reps = int(sys.argv[2])
manifest_path = Path(sys.argv[3])
policies = ["frontier_only", "cheap_only", "static_hybrid", "trace_governed"]

runs = []
for task_class, prefix in [("docs", "docs"), ("code_fix", "code-fix")]:
    for task_index in range(1, 4):
        task_id = f"{prefix}-{task_index:03d}"
        for rep in range(1, reps + 1):
            for policy in policies:
                runs.append(
                    {
                        "policy": policy,
                        "task_id": task_id,
                        "replicate": rep,
                        "run_id": f"{exp_id}-{policy}-{task_id}-r{rep}",
                        "task_class": task_class,
                    }
                )

manifest = {
    "experiment_id": exp_id,
    "description": (
        "Controlled docs/code_fix fixture batch. Fixtures are reset before "
        "each policy run."
    ),
    "expensive_model_patterns": ["opus", "sonnet", "fable", "mythos", "claude"],
    "runs": runs,
}
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(manifest_path)
PY

for rep in $(seq 1 "$REPS"); do
  for task_num in 001 002 003; do
    for policy in "${POLICIES[@]}"; do
      reset_docs_fixture "$task_num"
      run_id="${EXP_ID}-${policy}-docs-${task_num}-r${rep}"
      echo "=== ${run_id} ==="
      MINI_ORK_RUN_ID="$run_id" \
      MO_ROUTING_POLICY="$policy" \
      MO_FRONTIER_LANE="$MO_FRONTIER_LANE" \
      MO_CHEAP_LANE="$MO_CHEAP_LANE" \
      MO_RUBRIC=0 \
      MO_AUTO_REFLECT=0 \
      bin/mini-ork run docs "$DOC_KICKOFF/docs-${task_num}.md"
    done
  done

  for task_num in 001 002 003; do
    for policy in "${POLICIES[@]}"; do
      reset_code_fixture "$task_num"
      run_id="${EXP_ID}-${policy}-code-fix-${task_num}-r${rep}"
      echo "=== ${run_id} ==="
      MINI_ORK_RUN_ID="$run_id" \
      MO_ROUTING_POLICY="$policy" \
      MO_FRONTIER_LANE="$MO_FRONTIER_LANE" \
      MO_CHEAP_LANE="$MO_CHEAP_LANE" \
      MO_RUBRIC=0 \
      MO_AUTO_REFLECT=0 \
      MINI_ORK_TYPECHECK_CMD="python3 -m py_compile $CODE_WORK/task_${task_num}/"*.py \
      MINI_ORK_TEST_CMD="python3 $CODE_WORK/task_${task_num}/test_$(case "$task_num" in 001) echo tally ;; 002) echo clamp ;; 003) echo slugify ;; esac).py" \
      bin/mini-ork run code-fix "$CODE_KICKOFF/code-fix-${task_num}.md"
    done
  done
done

for task_num in 001 002 003; do
  reset_docs_fixture "$task_num"
  reset_code_fixture "$task_num"
done

python3 scripts/research/analyze_trace_budget_experiment.py \
  --db .mini-ork/state.db \
  --mini-ork-home .mini-ork \
  --manifest "$MANIFEST" \
  --min-runs-per-policy "$((6 * REPS))" \
  --min-task-classes 2 \
  --out-dir "$RESULTS_DIR"
