# I4a repair: collapse halt that can actually fire, as an independent trip

## Context

The previous pass for `kickoffs/auto/rsi-i4-collapse-halt.md` is committed on
this branch (HEAD). Two defects:

1. **It can never fire in production.** `check_liveness_breaker` passes an empty
   history to `collapse_detector.detect` when `collapse_history` is None, so the
   detector always answers "none". The signal is installed but inert.
2. **It changed the voting math.** Appending a 4th entry to `signals_fired`
   changes the `and` and `majority` policies (`and` now needs 4 signals).
   `tests/unit/test_circuit_breaker_py.py::test_policy_and_requires_all_signals`
   fails (it passed on origin/main).

## Mechanism (exact spec)

1. New migration `db/migrations/0060_collapse_history.sql`: table
   `collapse_history(id INTEGER PRIMARY KEY AUTOINCREMENT, task_class TEXT NOT NULL,
   step INTEGER NOT NULL, score REAL NOT NULL, anchor REAL NOT NULL,
   directives INTEGER NOT NULL DEFAULT 0, run_id TEXT, created_at INTEGER NOT NULL)`
   plus an index on `(task_class, step)`. Follow the style of the existing
   migrations (look at 0057–0059) and whatever registration the migration loader
   requires.
2. In `circuit_breaker.py`, when `collapse_history` is None, read the rows for
   the run's task class from `collapse_history` (ordered by step) and feed them to
   `collapse_detector.detect` as `[{step, score, anchor, directives}]`. A missing
   table or no rows is not a trip (fail-open, logged once).
3. Collapse is an INDEPENDENT trip, not a vote: keep `signals_fired` as the
   original three signals so `fired_count`, `signal_count` and all policies are
   unchanged; when the collapse signal fires, the breaker trips (state OPEN,
   verdict LIVENESS_TRIP, rc=1) regardless of policy, with the collapse
   rationale in the output. Add a `collapse` sub-object to the JSON output.
4. Keep `MO_CB_COLLAPSE=0` as the opt-out.

## Files in scope

- mini_ork/recovery/circuit_breaker.py
- db/migrations/0060_collapse_history.sql (new)
- tests/unit/test_circuit_breaker_collapse.py

## Tests

Update `tests/unit/test_circuit_breaker_collapse.py` so it covers: rows seeded in
the `collapse_history` table (no `collapse_history=` kwarg) that show
score-rise/anchor-drop → trip; healthy rows → no trip; missing table → no trip;
opt-out → no trip; a collapse trip with zero stagnation signals still trips
under `policy="and"`.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_circuit_breaker_collapse.py tests/unit/test_circuit_breaker_py.py tests/unit/test_collapse_detector_py.py` exits 0, and the migration-loader tests (`python3.11 -m pytest -q tests -k migrat`) still pass.
- Run both commands yourself and paste their last lines into your final message.

## Rules

Edit files directly; do not emit unified diffs. Do not touch apply.py or promotion_gate.py (the apply loop's writer is a later task).
