# Trace-Governed Budget Allocation Experiment Protocol

**Purpose:** collect real mini-ork data for
`docs/research/trace-governed-budget-allocation-arxiv-draft.md`.

The paper hypothesis is not proven by a single successful run. We need a
controlled comparison where the same benchmark tasks are executed under
different model-routing policies and then analyzed with the same metrics.

## Research Questions

1. Does trace-governed routing reduce cost per successful task relative to an
   expensive/frontier-only policy?
2. Does trace-governed routing preserve verifier pass rate relative to static
   hybrid routing?
3. Does trace-governed routing avoid repeated cheap-model failure loops by
   escalating after verifier failures or retries?

## Policies

Run each benchmark task under these policy labels:

| Policy | Meaning | Expected use in paper |
|---|---|---|
| `frontier_only` | All LLM lanes route to expensive/high-capability models. | Cost upper baseline. |
| `cheap_only` | All LLM lanes route to cheap/fast models. | Reliability lower baseline. |
| `static_hybrid` | Planner/reviewer use expensive lanes; workers use cheap lanes. | Practical baseline. |
| `trace_governed` | Start cheap where safe; escalate on verifier failure, retry count, uncertainty, or risk. | Proposed method. |

Do **not** mix policy changes with prompt, verifier, or recipe changes in the
same experimental batch. If a prompt or verifier changes, start a new
experiment id.

## Benchmark Tasks

Use at least three task classes:

1. `obs_smoke` for low-cost telemetry sanity checks.
2. `docs` for deterministic documentation edits with link/grep verifiers.
3. `code_fix` for software tasks with typecheck/test verifiers.

Minimum useful sample:

| Task class | Tasks | Replicates per policy | Rationale |
|---|---:|---:|---|
| `obs_smoke` | 3 | 3 | Confirms telemetry and cheap repeated runs. |
| `docs` | 5 | 2 | Measures verifier-gated non-code work. |
| `code_fix` | 5 | 2 | Measures the software-engineering claim. |

For a stronger arXiv version, target 20-40 total tasks and 2-3 replicates per
policy.

## Metrics

The analysis script computes:

- total runs;
- successful runs;
- success rate;
- verifier pass rate;
- total cost;
- cost per successful task;
- median duration;
- LLM call count;
- failed LLM call count;
- input and output tokens;
- expensive-call count;
- distinct provider count.

Primary table for the paper:

```text
policy | runs | success_rate | verifier_pass_rate | total_cost_usd | cost_per_success_usd | median_duration_s | expensive_call_count
```

## Running Controlled Batches

The executor now supports an experiment routing policy through environment
variables:

| Variable | Values | Meaning |
|---|---|---|
| `MO_ROUTING_POLICY` | `workflow_default`, `frontier_only`, `cheap_only`, `static_hybrid`, `trace_governed` | Selects the routing policy under test. |
| `MO_FRONTIER_LANE` | lane name, default `opus_lens` | Expensive/high-capability lane. |
| `MO_CHEAP_LANE` | lane name, default `kimi_lens` | Cheap/fast worker lane. |

Use deterministic run ids so each result can be mapped to a policy:

```bash
export EXP_ID=trace-budget-20260610
export TASK_ID=obs-smoke-001
export REP=1
export POLICY=frontier_only
export MINI_ORK_RUN_ID="${EXP_ID}-${POLICY}-${TASK_ID}-r${REP}"
export MO_ROUTING_POLICY="$POLICY"
export MO_FRONTIER_LANE=opus_lens
export MO_CHEAP_LANE=kimi_lens
bin/mini-ork run obs-smoke recipes/obs-smoke/example-kickoff.md
```

After every run, append an entry to the manifest:

```json
{
  "policy": "frontier_only",
  "task_id": "obs-smoke-001",
  "replicate": 1,
  "run_id": "trace-budget-20260610-frontier_only-obs-smoke-001-r1"
}
```

Then analyze:

```bash
python3 scripts/research/analyze_trace_budget_experiment.py \
  --db .mini-ork/state.db \
  --mini-ork-home .mini-ork \
  --manifest docs/research/experiments/trace-budget-manifest.json \
  --out-dir docs/research/experiments/results/trace-budget-20260610
```

For a pilot sanity check on existing historical runs:

```bash
python3 scripts/research/analyze_trace_budget_experiment.py \
  --db .mini-ork/state.db \
  --mini-ork-home .mini-ork \
  --auto-recipe obs-smoke \
  --policy historical_obs_smoke \
  --out-dir docs/research/experiments/results/historical-obs-smoke
```

Historical runs are **pilot evidence only** because they are not controlled by
policy.

## Acceptance Criteria Before Updating the Paper

- Each policy has enough successful runs for the claim being made. The analyzer
  defaults to 12 runs per policy for paper-level claims; smaller smoke batches
  may be reported only as preliminary evidence.
- The benchmark covers at least three task classes before the paper claims the
  general software-agent hypothesis.
- The manifest maps every run to exactly one policy and task id.
- The analyzer produces `summary.md`, `summary.csv`, `comparisons.csv`, and
  `runs.csv`.
- Any failed run has a recorded failure mode, not just a missing artifact.
- The claim gate in `summary.md` reports `Supported by this batch: true` for
  the claim being copied into the paper.
- The paper result table replaces the TODO table in Appendix A only after the
  task-class diversity gate passes.

## Statistical Gate

The analyzer reports Wilson 95% intervals for success and verifier-pass rates.
It also compares the target policy, default `trace_governed`, against
`frontier_only`, `static_hybrid`, and `cheap_only`.

Default paper-level thresholds:

| Gate | Default |
|---|---:|
| Minimum runs per policy | 12 |
| Minimum task classes | 3 |
| Minimum cost reduction vs `frontier_only` | 20% |
| Maximum verifier-pass loss | 5 percentage points |

The thresholds are intentionally conservative. A smoke-only batch may show a
useful cost trend, but it should not be described as proving the paper
hypothesis.

## Notes for Scientific Honesty

- If `cheap_only` wins on both cost and success, report that. It means the
  benchmark was too easy or the cheap model is sufficient for that class.
- If `trace_governed` costs more than `static_hybrid`, inspect escalation
  thresholds before claiming benefit.
- If verifier pass rate is high but human review fails, the verifier is
  incomplete; report this as a threat to validity.
