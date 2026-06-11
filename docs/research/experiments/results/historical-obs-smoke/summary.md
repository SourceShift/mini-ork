# Trace-Budget Experiment Summary: historical-obs-smoke

## Policy Summary

| policy | runs | success_rate | verifier_pass_rate | total_cost_usd | cost_per_success_usd | median_duration_s | expensive_call_count |
| --- | --- | --- | --- | --- | --- | --- | --- |
| historical_obs_smoke | 24 | 0.1667 | 0.4583 | 7.658439 | 1.91461 | 106.0 | 156 |


## Run-Level Data

| policy | task_id | replicate | run_id | status | success | verifier_pass | task_cost_usd | duration_s | llm_call_count | expensive_call_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| historical_obs_smoke | obs-smoke-001 | 1 | run-1781015034-9925 | failed | 0 | 0 | 0.05 | 350.0 | 0 | 0 |
| historical_obs_smoke | obs-smoke-002 | 1 | run-1781015300-29102 | failed | 0 | 0 | 0.0 | 87.0 | 1 | 1 |
| historical_obs_smoke | obs-smoke-003 | 1 | run-1781015438-38459 | failed | 0 | 0 | 0.534316 | 105.0 | 5 | 5 |
| historical_obs_smoke | obs-smoke-004 | 1 | run-1781015698-55547 | failed | 0 | 0 | 0.536798 | 104.0 | 5 | 5 |
| historical_obs_smoke | obs-smoke-005 | 1 | run-1781016198-84027 | failed | 0 | 0 | 0.05 | 96.0 | 1 | 1 |
| historical_obs_smoke | obs-smoke-006 | 1 | run-1781016416-96341 | failed | 0 | 0 | 0.05 | 100.0 | 3 | 3 |
| historical_obs_smoke | obs-smoke-007 | 1 | run-1781031690-74197 | failed | 0 | 0 | 0.0 | 11.0 | 0 | 0 |
| historical_obs_smoke | obs-smoke-008 | 1 | run-1781031742-78513 | failed | 0 | 0 | 0.0 | 10.0 | 0 | 0 |
| historical_obs_smoke | obs-smoke-009 | 1 | run-1781031767-81437 | classified | 0 | 0 | 0.05 | 0.0 | 0 | 0 |
| historical_obs_smoke | obs-smoke-010 | 1 | run-1781031854-85467 | failed | 0 | 0 | 0.400261 | 89.0 | 7 | 7 |
| historical_obs_smoke | obs-smoke-011 | 1 | run-1781032390-13149 | failed | 0 | 0 | 0.407633 | 103.0 | 8 | 8 |
| historical_obs_smoke | obs-smoke-012 | 1 | run-1781032903-48633 | failed | 0 | 0 | 0.420374 | 108.0 | 7 | 7 |
| historical_obs_smoke | obs-smoke-013 | 1 | run-1781038813-58423 | failed | 0 | 1 | 0.421135 | 113.0 | 8 | 8 |
| historical_obs_smoke | obs-smoke-014 | 1 | run-1781039046-84883 | reviewing | 0 | 1 | 0.421618 | 106.0 | 7 | 7 |
| historical_obs_smoke | obs-smoke-015 | 1 | run-1781039200-2684 | reviewing | 0 | 1 | 0.435406 | 120.0 | 8 | 8 |
| historical_obs_smoke | obs-smoke-016 | 1 | run-1781039455-30154 | reviewing | 0 | 1 | 0.41606 | 105.0 | 8 | 8 |
| historical_obs_smoke | obs-smoke-017 | 1 | run-1781070408-23684 | reviewing | 0 | 1 | 0.415721 | 108.0 | 7 | 7 |
| historical_obs_smoke | obs-smoke-018 | 1 | run-1781070892-81354 | reviewing | 0 | 1 | 0.425597 | 117.0 | 8 | 8 |
| historical_obs_smoke | obs-smoke-019 | 1 | run-1781071699-60187 | reviewing | 0 | 1 | 0.422854 | 122.0 | 8 | 8 |
| historical_obs_smoke | obs-smoke-020 | 1 | run-1781082049-43701 | published | 1 | 1 | 0.435835 | 130.0 | 13 | 13 |
| historical_obs_smoke | obs-smoke-021 | 1 | run-1781104706-3045 | published | 1 | 1 | 0.423092 | 145.0 | 12 | 12 |
| historical_obs_smoke | obs-smoke-022 | 1 | run-1781105320-64712 | failed | 0 | 0 | 0.508578 | 156.0 | 15 | 15 |
| historical_obs_smoke | obs-smoke-023 | 1 | run-1781106591-90667 | published | 1 | 1 | 0.431676 | 125.0 | 12 | 12 |
| historical_obs_smoke | obs-smoke-024 | 1 | run-1781107148-33710 | published | 1 | 1 | 0.401485 | 102.0 | 13 | 13 |


## Caveats

- A policy with zero runs is not represented in the summary.
- Historical auto-recipe mode is pilot evidence only; use a manifest for controlled experiments.
- `verifier_pass` requires at least one `verifier-result-*.json` sidecar in the run directory.
