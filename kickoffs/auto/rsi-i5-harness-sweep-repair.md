# I5 repair: make cost-per-solved-task a real, per-arm gate condition

## Context

The previous pass for `kickoffs/auto/rsi-i5-harness-sweep.md` is committed on
this branch; read it — it is the spec (rule 2: "A candidate that keeps the
solved set and lowers cost-per-solved-task is eligible; one that raises it is
not"). Its tests pass, but the cost term is wrong in two ways:

1. In `mini_ork/cli/apply.py` it computes
   `cps_before = cost / n_solved_before` and `cps_after = cost / n_solved_after`
   with the SAME `cost` (`probe_result["cost_usd"]`, the total across every
   launch of both arms). The two arms' costs are never separated.
2. It only appends the numbers to `gate_rationale`; it never changes
   `gate_decision`. A harness candidate that raises cost still promotes.

Per-arm costs already exist: `probe_score` returns `runs` =
`[{probe, arm, run_id, outcome, cost_usd}]` (see probe_scorer.py ~line 522).

## Mechanism (exact spec)

1. First rebase onto origin/main (`git fetch origin && git rebase origin/main`).
2. Compute `baseline_cost` = sum of `cost_usd` over runs whose arm is the
   baseline/control arm(s), and `candidate_cost` = sum over the candidate arm.
   With the control arm (MO_APPLY_PROBE_CONTROL_N > 1), normalise the baseline
   to one attempt: divide the control arms' total by the number of control
   attempts. `cps = arm_cost / max(1, n_solved_in_that_arm)`.
3. For `harness.*` targets ONLY: if the gate would promote and
   `cps_candidate > cps_baseline`, change the decision to `quarantined` with
   reason `harness-cost-regression`. Non-harness targets keep today's decision
   (cost is still logged for them).
4. Missing per-arm costs (zero or absent) → do not block; log that cost was
   unmeasured.

## Files in scope

- mini_ork/cli/apply.py
- tests/unit/test_apply_harness_targets.py

## Tests (make these real — stub probe_score to return a `runs` list with per-arm costs)

- harness target, same solved set, candidate arm cheaper → promoted;
- harness target, same solved set, candidate arm more expensive → quarantined
  with `harness-cost-regression` (this must fail on the current code);
- agent.* target with a more expensive candidate → decision unchanged by cost;
- control arm N=3: baseline cost is normalised per attempt.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_apply_harness_targets.py tests/unit/test_cli_apply_py.py tests/unit/test_probe_scorer_control_arm.py tests/unit/test_collapse_history_writer.py` exits 0.
- Run it yourself and paste its last line into your final message.

## Rules

Edit files directly; do not emit unified diffs. Do not touch probe_scorer.py or promotion_gate.py.
