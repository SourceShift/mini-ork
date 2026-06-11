#!/usr/bin/env bash
# Run a controlled obs-smoke batch for the trace-budget paper.
#
# The script intentionally targets the obs-smoke recipe only. That recipe writes
# artifacts under .mini-ork/runs/<run_id>/ and is safe to repeat in a dirty
# checkout. Documentation/code-fix batches should use clean experimental
# worktrees because those recipes can edit repository files.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

EXP_ID="${EXP_ID:-trace-budget-obs-smoke-$(date +%Y%m%d-%H%M%S)}"
TASKS="${TASKS:-3}"
REPS="${REPS:-2}"
KICKOFF="${KICKOFF:-recipes/obs-smoke/example-kickoff.md}"
MANIFEST="${MANIFEST:-docs/research/experiments/${EXP_ID}-manifest.json}"
RESULTS_DIR="${RESULTS_DIR:-docs/research/experiments/results/${EXP_ID}}"
MO_FRONTIER_LANE="${MO_FRONTIER_LANE:-opus_lens}"
MO_CHEAP_LANE="${MO_CHEAP_LANE:-kimi_lens}"
POLICIES=(frontier_only cheap_only static_hybrid trace_governed)

mkdir -p "$(dirname "$MANIFEST")" "$RESULTS_DIR"

python3 - "$EXP_ID" "$TASKS" "$REPS" "$MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

exp_id = sys.argv[1]
tasks = int(sys.argv[2])
reps = int(sys.argv[3])
manifest_path = Path(sys.argv[4])
policies = ["frontier_only", "cheap_only", "static_hybrid", "trace_governed"]

runs = []
for task_index in range(1, tasks + 1):
    task_id = f"obs-smoke-{task_index:03d}"
    for rep in range(1, reps + 1):
        for policy in policies:
            runs.append(
                {
                    "policy": policy,
                    "task_id": task_id,
                    "replicate": rep,
                    "run_id": f"{exp_id}-{policy}-{task_id}-r{rep}",
                }
            )

manifest = {
    "experiment_id": exp_id,
    "description": (
        "Controlled replicated obs-smoke routing-policy batch. Safe smoke "
        "benchmark: writes only under .mini-ork/runs."
    ),
    "expensive_model_patterns": ["opus", "sonnet", "fable", "mythos", "claude"],
    "runs": runs,
}
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(manifest_path)
PY

for task_index in $(seq 1 "$TASKS"); do
  task_id="$(printf 'obs-smoke-%03d' "$task_index")"
  for rep in $(seq 1 "$REPS"); do
    for policy in "${POLICIES[@]}"; do
      run_id="${EXP_ID}-${policy}-${task_id}-r${rep}"
      echo "=== ${run_id} ==="
      MINI_ORK_RUN_ID="$run_id" \
      MO_ROUTING_POLICY="$policy" \
      MO_FRONTIER_LANE="$MO_FRONTIER_LANE" \
      MO_CHEAP_LANE="$MO_CHEAP_LANE" \
      MO_RUBRIC=0 \
      MO_AUTO_REFLECT=0 \
      bin/mini-ork run obs-smoke "$KICKOFF"
    done
  done
done

python3 scripts/research/analyze_trace_budget_experiment.py \
  --db .mini-ork/state.db \
  --mini-ork-home .mini-ork \
  --manifest "$MANIFEST" \
  --min-runs-per-policy "$((TASKS * REPS))" \
  --out-dir "$RESULTS_DIR"
