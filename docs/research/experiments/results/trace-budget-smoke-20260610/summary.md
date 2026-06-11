# Trace-Budget Experiment Summary: trace-budget-smoke-20260610

## Policy Summary

| policy | runs | success_rate | verifier_pass_rate | total_cost_usd | cost_per_success_usd | median_duration_s | expensive_call_count | plan_fallback_count | zero_stdout_artifact_count | success_rate_ci95 | verifier_pass_rate_ci95 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cheap_only | 1 | 1.0 | 1.0 | 0.473604 | 0.473604 | 129.0 | 1 | 1 | 0 | [0.2065, 1.0] | [0.2065, 1.0] |
| frontier_only | 1 | 1.0 | 1.0 | 0.51963 | 0.51963 | 87.0 | 4 | 0 | 0 | [0.2065, 1.0] | [0.2065, 1.0] |
| static_hybrid | 1 | 1.0 | 1.0 | 0.606211 | 0.606211 | 120.0 | 2 | 0 | 0 | [0.2065, 1.0] | [0.2065, 1.0] |
| trace_governed | 1 | 1.0 | 1.0 | 0.389639 | 0.389639 | 110.0 | 2 | 0 | 0 | [0.2065, 1.0] | [0.2065, 1.0] |


## Target Policy Comparisons

| target_policy | baseline_policy | target_runs | baseline_runs | success_rate_delta | verifier_pass_rate_delta | cost_per_success_delta_usd | cost_reduction_vs_baseline |
| --- | --- | --- | --- | --- | --- | --- | --- |
| trace_governed | frontier_only | 1 | 1 | 0.0 | 0.0 | -0.129991 | 0.2502 |
| trace_governed | static_hybrid | 1 | 1 | 0.0 | 0.0 | -0.216572 | 0.3573 |
| trace_governed | cheap_only | 1 | 1 | 0.0 | 0.0 | -0.083965 | 0.1773 |


## Claim Gate

- Target policy: `trace_governed`
- Primary baseline: `frontier_only`
- Minimum runs per policy: 12
- Minimum task classes: 3
- Observed task classes: obs_smoke
- Minimum cost reduction: 0.2
- Maximum verifier-pass loss: 0.05
- Supported by this batch: **false**

Reasons:

- insufficient per-policy sample size: cheap_only<n12, frontier_only<n12, static_hybrid<n12, trace_governed<n12
- insufficient task-class diversity: observed=1 (obs_smoke), required>=3

## Run-Level Data

| policy | task_id | replicate | run_id | status | success | verifier_pass | task_cost_usd | duration_s | llm_call_count | expensive_call_count | plan_fallback | zero_stdout_artifact_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| frontier_only | obs-smoke-001 | 1 | trace-budget-smoke-20260610-frontier_only-obs-smoke-001-r1 | published | 1 | 1 | 0.51963 | 87.0 | 4 | 4 | 0 | 0 |
| cheap_only | obs-smoke-001 | 1 | trace-budget-smoke-20260610-cheap_only-obs-smoke-001-r1 | published | 1 | 1 | 0.473604 | 129.0 | 3 | 1 | 1 | 0 |
| static_hybrid | obs-smoke-001 | 1 | trace-budget-smoke-20260610-static_hybrid-obs-smoke-001-r1 | published | 1 | 1 | 0.606211 | 120.0 | 3 | 2 | 0 | 0 |
| trace_governed | obs-smoke-001 | 1 | trace-budget-smoke-20260610-trace_governed-obs-smoke-001-r1 | published | 1 | 1 | 0.389639 | 110.0 | 3 | 2 | 0 | 0 |


## Caveats

- A policy with zero runs is not represented in the summary.
- Historical auto-recipe mode is pilot evidence only; use a manifest for controlled experiments.
- `verifier_pass` requires at least one `verifier-result-*.json` sidecar in the run directory.
