# RSI gap: CoBa value-of-another-round stop rule for the revise loop

## Goal

CoBa (arXiv 2608.07424) allocates a fixed budget between generating another
attempt, verifying, and stopping, using the measured marginal value of each.
mini-ork's revise loop (`mini_ork/cli/execute.py`, inside
`_dispatch_dependency_graph`, the cap checks at the `cap = min(max_rounds,
revise_rounds_cap)` lines) always spends up to `MO_REVISE_ROUNDS` (default 2)
implementer rounds, whatever history says a further round is worth. Nothing
records per-round outcomes, so that value cannot even be measured today.

Record every revise round's outcome, estimate the success probability of the
next round from that history, and stop early when the measured value is
clearly too low.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. New migration `db/migrations/0081_revise_rounds.sql` (this exact name is
   claimed for this worktree): table `revise_rounds` — `id INTEGER PRIMARY KEY`,
   `run_id TEXT NOT NULL`, `task_class TEXT`, `target_node TEXT NOT NULL`,
   `round INTEGER NOT NULL`, `passed INTEGER` (NULL until known, 1 passed,
   0 failed), `cost_usd REAL`, `created_at INTEGER NOT NULL`; unique
   `(run_id, target_node, round)`. Use `CREATE TABLE IF NOT EXISTS` and record
   itself in `schema_migrations` the way the neighbouring migrations do.
2. New `mini_ork/learning/revise_value.py` (sqlite, creates the table if absent
   so an unmigrated home still works; every function swallows DB errors and
   returns a neutral value — the revise loop must never crash on it):
   - `record_round(db, run_id, task_class, target_node, round_no, cost_usd=None)`
     — idempotent on the unique key.
   - `settle_round(db, run_id, target_node, round_no, passed: bool)`.
   - `next_round_value(db, task_class, target_node, round_no) -> dict` —
     among settled history rows of this `task_class` and `target_node` that
     reached round `round_no` (the round about to be granted), the fraction
     that passed at that round: `{"p": float | None, "n": int}`; `p` is None
     when `n == 0`.
   - `should_stop(db, task_class, target_node, round_no) -> tuple[bool, str]` —
     True only when `n >= MO_COBA_MIN_SAMPLES` (default 20, tighten-only: a
     lower value clamps up to 20) AND `p < MO_COBA_MIN_GAIN` (default 0.05;
     tighten-only in the other direction: a higher value clamps down to 0.05,
     so the env can only make stopping RARER). Returns the reason text.
     `MO_COBA=0` → always `(False, "coba disabled")`.
3. `mini_ork/cli/execute.py`, at both revise cap checks (rc-fired and
   condition-fired): after the existing `round_used.get(target, 0) >= cap`
   check and before the target is added to `revise_groups`, call
   `revise_value.should_stop(db, task_class, target, round_used.get(target, 0) + 1)`;
   when it says stop, print `[revise] coba stop for {target}: {reason}` to
   stderr and `continue` (the source's ordinary failure path still fires, as
   with the existing skips). When a round IS granted, `record_round` it. When
   the source that fired the round later passes or fails in a following wave,
   `settle_round` the most recent unsettled round of that target. Use the run's
   existing `db`/`task_class`/`run_id` variables in that scope; do not add new
   global state.

## Files in scope (touch ONLY these)

- `db/migrations/0081_revise_rounds.sql` (new)
- `mini_ork/learning/revise_value.py` (new)
- `mini_ork/cli/execute.py`
- `tests/unit/test_revise_value.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_revise_value.py`, hermetic temp sqlite)

- `record_round` twice with the same key → one row;
- `next_round_value` with no history → `{"p": None, "n": 0}`;
- 25 settled round-2 rows, 1 passed → `should_stop` True; 10 rows all failed
  → False (below min samples); 25 rows, 5 passed → False;
- `MO_COBA_MIN_SAMPLES=3` clamps to 20; `MO_COBA_MIN_GAIN=0.5` clamps to 0.05;
- `MO_COBA=0` → never stops;
- a DB path in a non-existent directory → every function returns its neutral
  value without raising.

Also run the existing revise-loop suites in the verification command; they must
pass unchanged (with an empty `revise_rounds` history the loop behaves exactly
as before).

## Verification commands

- `pytest -q tests/unit/test_revise_value.py tests/unit/test_revise_loop.py tests/unit/test_revise_non_verdict.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_revise_value.py tests/unit/test_revise_loop.py --junitxml="${MINI_ORK_RUN_DIR}/coba-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/coba-junit.xml`

## Success criteria

- The verification command passes.
- `ruff check --select F,E9 mini_ork/learning/revise_value.py mini_ork/cli/execute.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
