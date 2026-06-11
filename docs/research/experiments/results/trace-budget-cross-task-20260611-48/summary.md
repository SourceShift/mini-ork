# Trace-Budget Experiment Summary: trace-budget-cross-task-20260611-48

## Policy Summary

| policy | runs | success_rate | verifier_pass_rate | reviewer_accept_rate | total_cost_usd | cost_per_success_usd | median_duration_s | expensive_call_count | plan_fallback_count | zero_stdout_artifact_count | success_rate_ci95 | verifier_pass_rate_ci95 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cheap_only | 12 | 1.0 | 1.0 | 1.0 | 5.229859 | 0.435822 | 132.0 | 12 | 3 | 0 | [0.7575, 1.0] | [0.7575, 1.0] |
| frontier_only | 12 | 1.0 | 1.0 | 0.8889 | 6.437018 | 0.536418 | 87.0 | 67 | 4 | 0 | [0.7575, 1.0] | [0.7575, 1.0] |
| static_hybrid | 12 | 1.0 | 1.0 | 0.8889 | 4.999689 | 0.416641 | 93.0 | 31 | 3 | 0 | [0.7575, 1.0] | [0.7575, 1.0] |
| trace_governed | 12 | 1.0 | 1.0 | 0.8889 | 4.723043 | 0.393587 | 99.0 | 27 | 2 | 0 | [0.7575, 1.0] | [0.7575, 1.0] |


## Target Policy Comparisons

| target_policy | baseline_policy | target_runs | baseline_runs | success_rate_delta | verifier_pass_rate_delta | cost_per_success_delta_usd | cost_reduction_vs_baseline |
| --- | --- | --- | --- | --- | --- | --- | --- |
| trace_governed | frontier_only | 12 | 12 | 0.0 | 0.0 | -0.142831 | 0.2663 |
| trace_governed | static_hybrid | 12 | 12 | 0.0 | 0.0 | -0.023054 | 0.0553 |
| trace_governed | cheap_only | 12 | 12 | 0.0 | 0.0 | -0.042235 | 0.0969 |


## Claim Gate

- Target policy: `trace_governed`
- Primary baseline: `frontier_only`
- Minimum runs per policy: 12
- Minimum task classes: 3
- Observed task classes: code_fix, docs, obs_smoke
- Minimum cost reduction: 0.2
- Maximum verifier-pass loss: 0.05
- Supported by this batch: **true**

Reasons:

- none

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
| frontier_only | docs-001 | 1 | trace-budget-fixtures-20260611-24c-frontier_only-docs-001-r1 | published | 1 | 1 |  | 0 | 0.386873 | 69.0 | 5 | 5 | 0 | 0 |
| cheap_only | docs-001 | 1 | trace-budget-fixtures-20260611-24c-cheap_only-docs-001-r1 | published | 1 | 1 |  | 0 | 0.451264 | 77.0 | 2 | 1 | 0 | 0 |
| static_hybrid | docs-001 | 1 | trace-budget-fixtures-20260611-24c-static_hybrid-docs-001-r1 | published | 1 | 1 |  | 0 | 0.2228 | 68.0 | 2 | 1 | 0 | 0 |
| trace_governed | docs-001 | 1 | trace-budget-fixtures-20260611-24c-trace_governed-docs-001-r1 | published | 1 | 1 |  | 0 | 0.227148 | 62.0 | 2 | 1 | 0 | 0 |
| frontier_only | docs-002 | 1 | trace-budget-fixtures-20260611-24c-frontier_only-docs-002-r1 | published | 1 | 1 |  | 0 | 0.387762 | 67.0 | 5 | 5 | 0 | 0 |
| cheap_only | docs-002 | 1 | trace-budget-fixtures-20260611-24c-cheap_only-docs-002-r1 | published | 1 | 1 |  | 0 | 0.251841 | 69.0 | 2 | 1 | 0 | 0 |
| static_hybrid | docs-002 | 1 | trace-budget-fixtures-20260611-24c-static_hybrid-docs-002-r1 | published | 1 | 1 |  | 0 | 0.236464 | 70.0 | 2 | 1 | 0 | 0 |
| trace_governed | docs-002 | 1 | trace-budget-fixtures-20260611-24c-trace_governed-docs-002-r1 | published | 1 | 1 |  | 0 | 0.190509 | 59.0 | 2 | 1 | 0 | 0 |
| frontier_only | docs-003 | 1 | trace-budget-fixtures-20260611-24c-frontier_only-docs-003-r1 | published | 1 | 1 |  | 0 | 0.383854 | 65.0 | 5 | 5 | 0 | 0 |
| cheap_only | docs-003 | 1 | trace-budget-fixtures-20260611-24c-cheap_only-docs-003-r1 | published | 1 | 1 |  | 0 | 0.228118 | 67.0 | 2 | 1 | 0 | 0 |
| static_hybrid | docs-003 | 1 | trace-budget-fixtures-20260611-24c-static_hybrid-docs-003-r1 | published | 1 | 1 |  | 0 | 0.222095 | 63.0 | 2 | 1 | 0 | 0 |
| trace_governed | docs-003 | 1 | trace-budget-fixtures-20260611-24c-trace_governed-docs-003-r1 | published | 1 | 1 |  | 0 | 0.255659 | 69.0 | 2 | 1 | 0 | 0 |
| frontier_only | code-fix-001 | 1 | trace-budget-fixtures-20260611-24c-frontier_only-code-fix-001-r1 | published | 1 | 1 | pass | 1 | 0.734695 | 132.0 | 9 | 9 | 0 | 0 |
| cheap_only | code-fix-001 | 1 | trace-budget-fixtures-20260611-24c-cheap_only-code-fix-001-r1 | published | 1 | 1 | APPROVE | 1 | 0.608381 | 187.0 | 3 | 1 | 0 | 0 |
| static_hybrid | code-fix-001 | 1 | trace-budget-fixtures-20260611-24c-static_hybrid-code-fix-001-r1 | published | 1 | 1 | needs_revision | 0 | 0.65326 | 119.0 | 3 | 2 | 0 | 0 |
| trace_governed | code-fix-001 | 1 | trace-budget-fixtures-20260611-24c-trace_governed-code-fix-001-r1 | published | 1 | 1 | needs_revision | 0 | 0.556248 | 149.0 | 3 | 2 | 0 | 0 |
| frontier_only | code-fix-002 | 1 | trace-budget-fixtures-20260611-24c-frontier_only-code-fix-002-r1 | published | 1 | 1 | needs_revision | 0 | 0.596333 | 115.0 | 5 | 5 | 0 | 0 |
| cheap_only | code-fix-002 | 1 | trace-budget-fixtures-20260611-24c-cheap_only-code-fix-002-r1 | published | 1 | 1 | APPROVE | 1 | 0.92539 | 354.0 | 3 | 1 | 0 | 0 |
| static_hybrid | code-fix-002 | 1 | trace-budget-fixtures-20260611-24c-static_hybrid-code-fix-002-r1 | published | 1 | 1 | pass | 1 | 0.792078 | 163.0 | 10 | 9 | 0 | 0 |
| trace_governed | code-fix-002 | 1 | trace-budget-fixtures-20260611-24c-trace_governed-code-fix-002-r1 | published | 1 | 1 | pass | 1 | 0.565653 | 134.0 | 5 | 4 | 0 | 0 |
| frontier_only | code-fix-003 | 1 | trace-budget-fixtures-20260611-24c-frontier_only-code-fix-003-r1 | published | 1 | 1 | pass | 1 | 0.92536 | 153.0 | 14 | 14 | 0 | 0 |
| cheap_only | code-fix-003 | 1 | trace-budget-fixtures-20260611-24c-cheap_only-code-fix-003-r1 | published | 1 | 1 | APPROVE | 1 | 0.718715 | 221.0 | 3 | 1 | 0 | 0 |
| static_hybrid | code-fix-003 | 1 | trace-budget-fixtures-20260611-24c-static_hybrid-code-fix-003-r1 | published | 1 | 1 | pass | 1 | 0.672219 | 171.0 | 6 | 5 | 0 | 0 |
| trace_governed | code-fix-003 | 1 | trace-budget-fixtures-20260611-24c-trace_governed-code-fix-003-r1 | published | 1 | 1 | pass | 1 | 0.670825 | 154.0 | 7 | 6 | 0 | 0 |


## Caveats

- A policy with zero runs is not represented in the summary.
- Historical auto-recipe mode is pilot evidence only; use a manifest for controlled experiments.
- `verifier_pass` requires at least one `verifier-result-*.json` sidecar in the run directory.
