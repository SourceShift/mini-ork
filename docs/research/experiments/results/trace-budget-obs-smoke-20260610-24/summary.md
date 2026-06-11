# Trace-Budget Experiment Summary: trace-budget-obs-smoke-20260610-24

## Policy Summary

| policy | runs | success_rate | verifier_pass_rate | reviewer_accept_rate | total_cost_usd | cost_per_success_usd | median_duration_s | expensive_call_count | plan_fallback_count | zero_stdout_artifact_count | success_rate_ci95 | verifier_pass_rate_ci95 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cheap_only | 6 | 1.0 | 1.0 | 1.0 | 2.04615 | 0.341025 | 132.0 | 6 | 3 | 0 | [0.6097, 1.0] | [0.6097, 1.0] |
| frontier_only | 6 | 1.0 | 1.0 | 1.0 | 3.022141 | 0.50369 | 87.0 | 24 | 4 | 0 | [0.6097, 1.0] | [0.6097, 1.0] |
| static_hybrid | 6 | 1.0 | 1.0 | 1.0 | 2.200773 | 0.366795 | 93.0 | 12 | 3 | 0 | [0.6097, 1.0] | [0.6097, 1.0] |
| trace_governed | 6 | 1.0 | 1.0 | 1.0 | 2.257001 | 0.376167 | 99.0 | 12 | 2 | 0 | [0.6097, 1.0] | [0.6097, 1.0] |


## Target Policy Comparisons

| target_policy | baseline_policy | target_runs | baseline_runs | success_rate_delta | verifier_pass_rate_delta | cost_per_success_delta_usd | cost_reduction_vs_baseline |
| --- | --- | --- | --- | --- | --- | --- | --- |
| trace_governed | frontier_only | 6 | 6 | 0.0 | 0.0 | -0.127523 | 0.2532 |
| trace_governed | static_hybrid | 6 | 6 | 0.0 | 0.0 | 0.009372 | -0.0256 |
| trace_governed | cheap_only | 6 | 6 | 0.0 | 0.0 | 0.035142 | -0.103 |


## Claim Gate

- Target policy: `trace_governed`
- Primary baseline: `frontier_only`
- Minimum runs per policy: 6
- Minimum task classes: 3
- Observed task classes: obs_smoke
- Minimum cost reduction: 0.2
- Maximum verifier-pass loss: 0.05
- Supported by this batch: **false**

Reasons:

- insufficient task-class diversity: observed=1 (obs_smoke), required>=3

## Run-Level Data

| policy | task_id | replicate | run_id | status | success | verifier_pass | reviewer_verdicts | reviewer_accept | task_cost_usd | duration_s | llm_call_count | expensive_call_count | plan_fallback | zero_stdout_artifact_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| frontier_only | obs-smoke-001 | 1 | trace-budget-obs-smoke-20260610-24-frontier_only-obs-smoke-001-r1 | published | 1 | 1 | pass | 1 | 0.493341 | 93.0 | 4 | 4 | 1 | 0 |
| cheap_only | obs-smoke-001 | 1 | trace-budget-obs-smoke-20260610-24-cheap_only-obs-smoke-001-r1 | published | 1 | 1 | pass | 1 | 0.258498 | 124.0 | 3 | 1 | 1 | 0 |
| static_hybrid | obs-smoke-001 | 1 | trace-budget-obs-smoke-20260610-24-static_hybrid-obs-smoke-001-r1 | published | 1 | 1 | pass | 1 | 0.35464 | 88.0 | 3 | 2 | 1 | 0 |
| trace_governed | obs-smoke-001 | 1 | trace-budget-obs-smoke-20260610-24-trace_governed-obs-smoke-001-r1 | published | 1 | 1 | pass | 1 | 0.388173 | 102.0 | 3 | 2 | 0 | 0 |
| frontier_only | obs-smoke-001 | 2 | trace-budget-obs-smoke-20260610-24-frontier_only-obs-smoke-001-r2 | published | 1 | 1 | pass | 1 | 0.492734 | 89.0 | 4 | 4 | 1 | 0 |
| cheap_only | obs-smoke-001 | 2 | trace-budget-obs-smoke-20260610-24-cheap_only-obs-smoke-001-r2 | published | 1 | 1 | pass | 1 | 0.23993 | 96.0 | 3 | 1 | 0 | 0 |
| static_hybrid | obs-smoke-001 | 2 | trace-budget-obs-smoke-20260610-24-static_hybrid-obs-smoke-001-r2 | published | 1 | 1 | pass | 1 | 0.378773 | 99.0 | 3 | 2 | 0 | 0 |
| trace_governed | obs-smoke-001 | 2 | trace-budget-obs-smoke-20260610-24-trace_governed-obs-smoke-001-r2 | published | 1 | 1 | pass | 1 | 0.374987 | 97.0 | 3 | 2 | 0 | 0 |
| frontier_only | obs-smoke-002 | 1 | trace-budget-obs-smoke-20260610-24-frontier_only-obs-smoke-002-r1 | published | 1 | 1 | pass | 1 | 0.528714 | 88.0 | 4 | 4 | 0 | 0 |
| cheap_only | obs-smoke-002 | 1 | trace-budget-obs-smoke-20260610-24-cheap_only-obs-smoke-002-r1 | published | 1 | 1 | pass | 1 | 0.435771 | 153.0 | 3 | 1 | 1 | 0 |
| static_hybrid | obs-smoke-002 | 1 | trace-budget-obs-smoke-20260610-24-static_hybrid-obs-smoke-002-r1 | published | 1 | 1 | pass | 1 | 0.357985 | 93.0 | 3 | 2 | 1 | 0 |
| trace_governed | obs-smoke-002 | 1 | trace-budget-obs-smoke-20260610-24-trace_governed-obs-smoke-002-r1 | published | 1 | 1 | pass | 1 | 0.35154 | 98.0 | 3 | 2 | 1 | 0 |
| frontier_only | obs-smoke-002 | 2 | trace-budget-obs-smoke-20260610-24-frontier_only-obs-smoke-002-r2 | published | 1 | 1 | pass | 1 | 0.491941 | 86.0 | 4 | 4 | 1 | 0 |
| cheap_only | obs-smoke-002 | 2 | trace-budget-obs-smoke-20260610-24-cheap_only-obs-smoke-002-r2 | published | 1 | 1 | pass | 1 | 0.574482 | 199.0 | 3 | 1 | 0 | 0 |
| static_hybrid | obs-smoke-002 | 2 | trace-budget-obs-smoke-20260610-24-static_hybrid-obs-smoke-002-r2 | published | 1 | 1 | pass | 1 | 0.379236 | 98.0 | 3 | 2 | 0 | 0 |
| trace_governed | obs-smoke-002 | 2 | trace-budget-obs-smoke-20260610-24-trace_governed-obs-smoke-002-r2 | published | 1 | 1 | pass | 1 | 0.35218 | 98.0 | 3 | 2 | 1 | 0 |
| frontier_only | obs-smoke-003 | 1 | trace-budget-obs-smoke-20260610-24-frontier_only-obs-smoke-003-r1 | published | 1 | 1 | pass | 1 | 0.492291 | 83.0 | 4 | 4 | 1 | 0 |
| cheap_only | obs-smoke-003 | 1 | trace-budget-obs-smoke-20260610-24-cheap_only-obs-smoke-003-r1 | published | 1 | 1 | pass | 1 | 0.250975 | 108.0 | 3 | 1 | 1 | 0 |
| static_hybrid | obs-smoke-003 | 1 | trace-budget-obs-smoke-20260610-24-static_hybrid-obs-smoke-003-r1 | published | 1 | 1 | pass | 1 | 0.380509 | 93.0 | 3 | 2 | 0 | 0 |
| trace_governed | obs-smoke-003 | 1 | trace-budget-obs-smoke-20260610-24-trace_governed-obs-smoke-003-r1 | published | 1 | 1 | pass | 1 | 0.412014 | 111.0 | 3 | 2 | 0 | 0 |
| frontier_only | obs-smoke-003 | 2 | trace-budget-obs-smoke-20260610-24-frontier_only-obs-smoke-003-r2 | published | 1 | 1 | pass | 1 | 0.52312 | 86.0 | 4 | 4 | 0 | 0 |
| cheap_only | obs-smoke-003 | 2 | trace-budget-obs-smoke-20260610-24-cheap_only-obs-smoke-003-r2 | published | 1 | 1 | pass | 1 | 0.286494 | 140.0 | 3 | 1 | 0 | 0 |
| static_hybrid | obs-smoke-003 | 2 | trace-budget-obs-smoke-20260610-24-static_hybrid-obs-smoke-003-r2 | published | 1 | 1 | pass | 1 | 0.34963 | 81.0 | 3 | 2 | 1 | 0 |
| trace_governed | obs-smoke-003 | 2 | trace-budget-obs-smoke-20260610-24-trace_governed-obs-smoke-003-r2 | published | 1 | 1 | pass | 1 | 0.378107 | 100.0 | 3 | 2 | 0 | 0 |


## Caveats

- A policy with zero runs is not represented in the summary.
- Historical auto-recipe mode is pilot evidence only; use a manifest for controlled experiments.
- `verifier_pass` requires at least one `verifier-result-*.json` sidecar in the run directory.
