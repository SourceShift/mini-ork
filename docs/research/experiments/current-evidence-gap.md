# Current Evidence Status for the Trace-Governed Budget Paper

The repository now has an experiment protocol, analyzer, controlled fixtures,
and a 48-run cross-task benchmark. The narrow benchmark claim is now supported:
trace-governed routing reduced cost per successful task relative to
frontier-only routing while preserving verifier pass rate on controlled
mini-ork fixtures.

## What We Have

- A scientific draft with formal policy properties:
  `docs/research/trace-governed-budget-allocation-arxiv-draft.md`
- A controlled experiment protocol:
  `docs/research/experiments/trace-budget-experiment-protocol.md`
- A manifest template:
  `docs/research/experiments/trace-budget-manifest.example.json`
- A read-only telemetry analyzer:
  `scripts/research/analyze_trace_budget_experiment.py`
- A pilot analysis of historical `obs-smoke` runs:
  `docs/research/experiments/results/historical-obs-smoke/summary.md`
- A controlled four-policy `obs-smoke` smoke batch:
  `docs/research/experiments/results/trace-budget-smoke-20260610/summary.md`
- A replicated controlled four-policy `obs_smoke` batch:
  `docs/research/experiments/results/trace-budget-obs-smoke-20260610-24/summary.md`
- A controlled `docs` + `code_fix` fixture batch:
  `docs/research/experiments/results/trace-budget-fixtures-20260611-24c/summary.md`
- A combined 48-run cross-task benchmark:
  `docs/research/experiments/results/trace-budget-cross-task-20260611-48/summary.md`

## Historical Pilot Result

Historical `obs-smoke` telemetry currently shows:

| Policy | Runs | Success rate | Verifier pass rate | Total cost | Cost per success |
|---|---:|---:|---:|---:|---:|
| `historical_obs_smoke` | 24 | 0.1667 | 0.4583 | 7.658439 | 1.91461 |

This is useful as a telemetry sanity check, not as hypothesis proof.

## Controlled Smoke Result

The first controlled batch used one `obs_smoke` task, four policies, and one
replicate per policy:

| Policy | Runs | Success rate | Verifier pass rate | Total cost | Cost per success | Median duration | Expensive calls |
|---|---:|---:|---:|---:|---:|---:|---:|
| `frontier_only` | 1 | 1.0 | 1.0 | 0.51963 | 0.51963 | 87.0s | 4 |
| `cheap_only` | 1 | 1.0 | 1.0 | 0.473604 | 0.473604 | 129.0s | 1 |
| `static_hybrid` | 1 | 1.0 | 1.0 | 0.606211 | 0.606211 | 120.0s | 2 |
| `trace_governed` | 1 | 1.0 | 1.0 | 0.389639 | 0.389639 | 110.0s | 2 |

This is a controlled pipeline check, not a statistically meaningful result.
It does show that the four policies can be run against the same task and
analyzed from persisted telemetry.

Important caveat: current policy routing is enforced at execution-node dispatch.
Classifier/planner calls can still use the workflow default provider/lane, so
the paper must distinguish execution-node routing from whole-run routing until
classifier/planner routing is included in the policy surface.

## Replicated Controlled Smoke Result

The replicated smoke batch used three `obs_smoke` task labels, two replicates,
and four policies for 24 live LLM runs:

| Policy | Runs | Success rate | Verifier pass rate | Total cost | Cost per success | Median duration | Expensive calls | Planner fallbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `frontier_only` | 6 | 1.0 | 1.0 | 3.022141 | 0.50369 | 87.0s | 24 | 4 |
| `cheap_only` | 6 | 1.0 | 1.0 | 2.04615 | 0.341025 | 132.0s | 6 | 3 |
| `static_hybrid` | 6 | 1.0 | 1.0 | 2.200773 | 0.366795 | 93.0s | 12 | 3 |
| `trace_governed` | 6 | 1.0 | 1.0 | 2.257001 | 0.376167 | 99.0s | 12 | 2 |

Within this smoke benchmark, `trace_governed` reduced cost per successful task
by 25.32% relative to `frontier_only` with no observed verifier-pass loss.
However, it was 2.56% more expensive than `static_hybrid` and 10.3% more
expensive than `cheap_only` on this easy task class.

The analyzer's claim gate marks the full paper hypothesis as **not supported**
by this batch because it covers only one task class (`obs_smoke`). The Wilson
95% interval for each observed 6/6 pass rate is `[0.6097, 1.0]`, which is still
wide.

## Cross-Task Benchmark Result

The combined benchmark used 48 live runs across `obs_smoke`, `docs`, and
`code_fix`:

| Policy | Runs | Success rate | Verifier pass rate | Reviewer accept rate | Total cost | Cost per success | Median duration | Expensive calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `frontier_only` | 12 | 1.0 | 1.0 | 0.8889 | 6.437018 | 0.536418 | 87.0s | 67 |
| `cheap_only` | 12 | 1.0 | 1.0 | 1.0 | 5.229859 | 0.435822 | 132.0s | 12 |
| `static_hybrid` | 12 | 1.0 | 1.0 | 0.8889 | 4.999689 | 0.416641 | 93.0s | 31 |
| `trace_governed` | 12 | 1.0 | 1.0 | 0.8889 | 4.723043 | 0.393587 | 99.0s | 27 |

Trace-governed routing reduced cost per successful task by 26.63% relative to
`frontier_only`, with no observed verifier-pass loss. The configured claim gate
passed with 12 runs per policy and three observed task classes.

## Remaining Validity Limits

- The benchmark uses small deterministic fixtures, not large production tasks.
- No verifier-failure escalation occurred because all deterministic verifiers
  passed on the first execution attempt.
- The `query-level workflow` baseline is not implemented yet, so the empirical
  comparison is against `frontier_only`, `cheap_only`, and `static_hybrid`.
- Policy routing currently applies at execution-node dispatch; classifier and
  planner calls can still use workflow defaults.
- Reviewer acceptance was not perfect on code-fix runs, so reviewer verdicts
  should remain a separate metric from deterministic verifier pass rate.

## Paper-Ready Claim Shape

Acceptable:

> In a controlled 48-run mini-ork fixture benchmark spanning `obs_smoke`,
> `docs`, and `code_fix`, trace-governed routing reduced cost per successful
> task by 26.63% relative to frontier-only routing while preserving verifier
> pass rate.

Not acceptable yet:

> Trace-governed routing proves that cheap models can replace frontier models.

That stronger claim needs controlled data across task classes and a careful
failure-mode analysis.
