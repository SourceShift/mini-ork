# I2 repair: feed the audit's detectors from collapse_history (they see 0 rows today)

## Context

The previous pass for `kickoffs/auto/rsi-i2-verifier-audit.md` is committed on
this branch; read it — it is the spec. Its tests pass, but in production it is
inert: run against the live DB,
`verifier_audit.audit(None, <state.db>)` returns `ok=True` with
`hack_probe.n_generations=0`, `metric_anchor.n_generations=0`, and
`gate_fuzzer: skipped`. `_read_hack_history` / `_read_metric_history` do
`SELECT *` from `benchmark_results` / `self_improve_runs`, whose columns are not
the detectors' row shape, so both detectors see nothing.

The right source now exists on origin/main: `collapse_history` (migration 0060)
is written by the apply loop after every scored decision with
`score` = the optimized (visible) probe solve fraction and `anchor` = the solve
fraction on anchor probes that the gate never optimizes against (a frozen core).

## Mechanism (exact spec)

1. First rebase this branch onto origin/main (`git fetch origin && git rebase
   origin/main`) so the collapse_history writer and migration are present.
2. Replace `_read_hack_history` / `_read_metric_history` with readers over
   `collapse_history`, ordered by `step`, filtered by task_class when one is
   given. Map rows to the exact row shapes `hack_probe.monitor` and
   `metric_anchor.audit` document in their module docstrings (read them): the
   generation index is `step`, the visible score is `score`, the frozen-core /
   anchor measurement is `anchor`. Where a detector needs counts rather than
   fractions, document the mapping you choose in a comment and keep it
   monotonic. Missing table or too few rows → that detector contributes no flag.
3. `promotion_evaluate` must pass the candidate's task_class to `audit` when it
   has one (derive it from the arguments/records already available; fall back
   to None only if genuinely unavailable).
4. Leave the gate_fuzzer arm as is (skipped without a corpus), but say so in the
   audit's evidence.

## Files in scope

- mini_ork/learning/verifier_audit.py
- mini_ork/gates/promotion_gate.py
- tests/unit/test_verifier_audit_py.py

## Tests (add)

- seed `collapse_history` rows (no history kwargs) with score rising while
  anchor falls over ≥ the detectors' minimum generations → `ok=False` with a
  hack_probe and/or metric_anchor flag, and a promotion that would be promoted
  is quarantined;
- healthy rows (score and anchor rise together) → no flags;
- rows for a different task_class are ignored when a task_class is given.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_verifier_audit_py.py tests/unit/test_promotion_gate_py.py` exits 0.
- `python3.11 -c "from mini_ork.learning import verifier_audit as v; print(v.audit(None, '/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db')['ok'])"` runs without error.
- Run both and paste their last lines into your final message.

## Rules

Edit files directly; do not emit unified diffs. Do not touch apply.py or the detectors' logic.
