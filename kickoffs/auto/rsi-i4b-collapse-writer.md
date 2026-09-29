# I4b apply loop writes collapse_history (G01-T04, part b)

## Goal

`mini_ork/recovery/circuit_breaker.py` now trips on a collapse halt read from
the `collapse_history` table (migration 0060: task_class, step, score, anchor,
directives, run_id, created_at). Nothing writes that table yet, so the halt
cannot fire. The apply loop must append one row per scored decision. The
anchor must be a measurement the loop cannot optimize: probe tasks tagged as
anchors are scored but EXCLUDED from the accept decision.

## Mechanism (exact spec)

1. Probe files under `recipes/<recipe>/probes/*.md` may carry `anchor: true` in
   their YAML frontmatter. In `mini_ork/learning/probe_scorer.py`, anchor probes
   are run for both arms like any probe but are reported separately
   (`anchor_solved_frac` for the candidate arm) and are NOT counted in the
   solved sets the gate compares. With no anchor probes, `anchor_solved_frac`
   is None.
2. In `mini_ork/cli/apply.py`, after each scored gate decision, if
   `anchor_solved_frac` is not None, insert a row into `collapse_history`:
   task_class of the recipe, step = 1 + the max existing step for that
   task_class (0-based start), score = candidate solved fraction on non-anchor
   probes, anchor = anchor_solved_frac, directives = number of directives the
   candidate adds (0 if unknown), run_id if available. Missing table → skip
   silently (older DBs). Never let a write failure change the gate decision.
3. No anchor probes → no row (the breaker then fails open, as designed).

## Files in scope

- mini_ork/learning/probe_scorer.py
- mini_ork/cli/apply.py
- tests/unit/test_collapse_history_writer.py (new)

## Tests

Hermetic `tests/unit/test_collapse_history_writer.py` (tmp sqlite with the 0060
table, stubbed arm runner):
- an anchor probe is excluded from the gate's solved-set comparison (a candidate
  that only gains on the anchor probe is NOT credited);
- a scored decision with an anchor probe appends a row with the expected
  score/anchor/step; a second decision increments step;
- no anchor probes → no row; missing table → no error, decision unchanged.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_collapse_history_writer.py tests/unit/test_probe_scorer_control_arm.py tests/unit/test_cli_apply_py.py tests/unit/test_probe_scorer_code_arm.py tests/unit/test_circuit_breaker_collapse.py` exits 0.
- Run it yourself and paste its last line into your final message.

## Rules

Edit files directly; do not emit unified diffs. Do not touch circuit_breaker.py or promotion_gate.py.
