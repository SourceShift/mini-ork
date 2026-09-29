# I1 repair: make the matched-attempt control arm correct and green

## Context

The previous implementer pass for `kickoffs/auto/rsi-i1-control-arm.md` is
committed on this branch (HEAD). Read that kickoff: it is the spec. The pass is
NOT done: all 6 of its own new tests fail, and it regressed 4 existing tests
that were green on origin/main (verified: 50/50 pass on the base).

Current failures (`python3.11 -m pytest -q tests/unit/test_probe_scorer_control_arm.py tests/unit/test_cli_apply_py.py tests/unit/test_probe_scorer_code_arm.py`):

```
E       assert None is not None
E       assert None is not None
E       assert None is not None
E       assert 0 == 1
E        +  where 0 = len([])
E       assert None is not None
E       assert None is not None
E       AssertionError: assert 'quarantined' == 'promoted'
E         - promoted
E         + quarantined
E       assert 1.0 == 0.5
E       AssertionError: assert 8 == 4
E        +  where 8 = len(['/var/folders/kc/1h62xjm128gb_x1n09c4v90m0000gn/T/mo-probe-target-u_xbicfq', '/var/folders/kc/1h62xjm128gb_x1n09c4v90...0m0000gn/T/mo-probe-target-g_k0z480', '/var/folders/kc/1h62xjm128gb_x1n09c4v90m0000gn/T/mo-probe-target-xjldmqne', ...])
E       assert None is not None
FAILED tests/unit/test_probe_scorer_control_arm.py::test_control_n_3_baseline_solves_on_1_of_3_retries_candidate_gets_no_credit
FAILED tests/unit/test_probe_scorer_control_arm.py::test_control_n_3_candidate_solves_a_task_no_retry_solved_is_credited
FAILED tests/unit/test_probe_scorer_control_arm.py::test_control_n_1_yields_byte_identical_decisions_to_pre_change
FAILED tests/unit/test_probe_scorer_control_arm.py::test_budget_exhaustion_mid_control_returns_none
FAILED tests/unit/test_probe_scorer_control_arm.py::test_control_n_env_minimum_is_clamped_to_one
FAILED tests/unit/test_probe_scorer_control_arm.py::test_control_solved_aggregates_best_of_n
FAILED tests/unit/test_cli_apply_py.py::test_evaluate_gate_pertask_no_regression
FAILED tests/unit/test_cli_apply_py.py::test_probe_scorer_two_arms_vectors_and_cleanup
FAILED tests/unit/test_cli_apply_py.py::test_probe_scorer_gives_each_arm_a_fresh_target_copy
FAILED tests/unit/test_probe_scorer_code_arm.py::test_timed_out_arm_keeps_the_probes_already_measured
```

## What to do

1. Find why `probe_score` returns `None` in the new tests (the new tests expect
   a scored result) and fix the implementation, not the assertions, unless an
   assertion contradicts the spec in the original kickoff.
2. Existing tests encode the single-baseline behaviour. They must pass with
   `MO_APPLY_PROBE_CONTROL_N=1`. You may add
   `monkeypatch.setenv("MO_APPLY_PROBE_CONTROL_N", "1")` to an existing test
   that counts arms/targets/copies; do NOT change any other assertion in
   existing tests.
3. `test_evaluate_gate_pertask_no_regression` now returns `quarantined` where
   `promoted` is expected: with no control data present (legacy `before`
   vectors), `evaluate_gate` must behave exactly as before.

## Files in scope

- mini_ork/learning/probe_scorer.py
- mini_ork/cli/apply.py
- tests/unit/test_probe_scorer_control_arm.py
- tests/unit/test_cli_apply_py.py (only the monkeypatch line described above)
- tests/unit/test_probe_scorer_code_arm.py (only the monkeypatch line described above)

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_probe_scorer_control_arm.py tests/unit/test_cli_apply_py.py tests/unit/test_probe_scorer_code_arm.py` exits 0.
- Run that command yourself before finishing and paste its last line into your final message.

## Rules

Edit files directly; do not emit unified diffs. Do not touch promotion_gate.py or execute.py.
