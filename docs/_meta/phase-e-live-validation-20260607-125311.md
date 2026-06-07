# Phase E LIVE — improve → benchmark → eval → promote (20260607-125311)

**Provider**: codex
**Wall time**: 44s
**Timeout**: 120s/task
**Candidate**: `wc-phase-e-cand-001`

## Benchmark summary

```json
{
    "candidate_id": "wc-phase-e-cand-001",
    "ran_at": 1780829593,
    "total_tasks": 2,
    "passed": 1,
    "failed": 1,
    "avg_utility_score": 0.65,
    "all_pass": false,
    "results": [
        {
            "benchmark_id": "bt-phase-e-001",
            "task_class": "code-fix",
            "passed": false,
            "utility_score": 0.3,
            "error": null
        },
        {
            "benchmark_id": "bt-phase-e-002",
            "task_class": "code-fix",
            "passed": true,
            "utility_score": 1.0,
            "error": null
        }
    ]
}
```

## Per-result rows

    benchmark_id    candidate_id         pass  utility_score  evidence_path                                                               
    --------------  -------------------  ----  -------------  ----------------------------------------------------------------------------
    bt-phase-e-001  wc-phase-e-cand-001  0     0.3            {"passed": false, "utility_score": 0.3, "output": "wrong: got 3 expected 5"}
    bt-phase-e-002  wc-phase-e-cand-001  1     1.0            {"passed": true, "utility_score": 1.0, "output": "correct: 42"}             

## Promotion decision

```json
{
    "decision": "rejected",
    "rationale": "Not all benchmark tasks passed (1/2)",
    "utility_before": 0.0,
    "utility_after": 0.65,
    "utility_delta": 0.65,
    "benchmark_run_id": "wc-phase-e-cand-001",
    "all_pass": false,
    "safety_violations": []
}
```

## promotion_records row

    promotion_id         candidate_id         decision  utility_before  utility_after  decided_by
    -------------------  -------------------  --------  --------------  -------------  ----------
    pr-d0656ce6d41142fd  wc-phase-e-cand-001  rejected  0.0             0.65           gate      

## Assertion results

    8 OK / 0 FAIL / 0 SKIP

## What this proves

- benchmark_suite.benchmark_run dispatches the MINI_ORK_WORKFLOW_RUNNER_FN
  with real LLM calls via cl_codex.sh.
- The runner correctly parses model output + assigns utility_score.
- benchmark_results table receives 1 row per task with pass/util scored.
- promotion_gate.promotion_evaluate reads the aggregate summary +
  emits a valid decision (promoted/quarantined/rejected/pending).
- promotion_records persists the decision with decided_at + decided_by.

Phase E (improve → eval → promote) is now LIVE-VALIDATED, not just
stub-test-green. The chain runs end-to-end against real LLM calls
with real DB writes.
