# Learning ledger — count every injection, give the pipeline a place to report each pass

Plan: `docs/plans/2026-10-07-learning-memory-page-refactor.md`, phase P1a (D5, plus the D6 storage
API). Wiring the pass stats into reflect is a separate later phase, because `reflect.py` and
`reflection_pipeline.py` are held by other worktrees right now.

## Why

The IDE can't say "this lesson was used N times", because nothing counts injections. Since
`learn-inject` (merged), every researcher / implementer / reviewer dispatch writes
`runs/<id>/learned/<node>.json` with the exact `sources` it injected:

- `{"kind": "gradient", "id": <gradient_id>, ...}`
- `{"kind": "pattern", "id": <pattern_id>, "text": ...}`
- `{"kind": "steering", "id": <row id>, ...}`

But files can't be counted across 1,000 runs. The learning pipeline also fails silently: pattern
induction wrote 0 lessons for weeks, and today every LLM call in a reflect outside a capped launch
is blocked by the $50 daily cost circuit. There is no table where a pass reports what it did.

## Files in scope (touch ONLY these)

- `mini_ork/learning/ledger.py` (new)
- `db/migrations/0062_learning_ledger.sql` (new; style of `db/migrations/0056_pattern_lesson_text.sql`)
- `mini_ork/cli/execute_handlers.py`: ONLY `_write_learned_record` (add the ledger write)
- `tests/unit/test_learning_ledger.py` (new)

Do NOT modify any other file.

## Schema (`0062_learning_ledger.sql`, additive, `IF NOT EXISTS`)

- `lesson_injections(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, node_id TEXT NOT
  NULL, node_type TEXT, lane TEXT, task_class TEXT, attempt INTEGER, source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL, held_out INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL)`
  - indexes: `(source_kind, source_id)`, `(run_id, node_id)`
  - unique `(run_id, node_id, attempt, source_kind, source_id, held_out)`, so a retried write
    can't double count
- `learning_pass_stats(id INTEGER PRIMARY KEY AUTOINCREMENT, pass_id TEXT NOT NULL, ts INTEGER NOT
  NULL, stage TEXT NOT NULL, inputs INTEGER, outputs INTEGER, failures INTEGER, lane TEXT,
  last_error TEXT)`, index `(stage, ts)`

`held_out` exists now for the holdout experiment that comes later. Nothing writes `1` yet.

## `mini_ork/learning/ledger.py` (exact API)

All functions take `db: str | None` (default `MINI_ORK_DB`, then `$MINI_ORK_HOME/state.db`), call
`ensure_schema` first, use `PRAGMA busy_timeout=5000`, and never raise on the write path (they
return 0 and write one line to stderr).

- `ensure_schema(db)`: the DDL above, idempotent.
- `record_injections(run_id, node_id, node_type, lane, task_class, attempt, sources, *, held_out=False, ts=None, db=None) -> int`:
  `INSERT OR IGNORE` one row per source that has `kind` and `id`; returns rows inserted.
- `record_pass_stat(pass_id, stage, *, inputs=0, outputs=0, failures=0, lane="", last_error="", ts=None, db=None) -> None`:
  `last_error` trimmed to its last 500 chars.
- `injection_counts(source_kind, source_ids, *, since=None, db=None) -> dict[str, dict]`:
  `{source_id: {"uses": n, "held_out": h, "last_ts": t, "runs": distinct_runs}}`. Read-only.
- `stage_health(*, window_passes=3, db=None) -> list[dict]`: per stage, the last
  `window_passes` rows newest first, plus `"alarm": True` when outputs == 0 in ALL of them while
  inputs > 0 in at least one, and `"last_error"` from the newest row with failures. Read-only.
  This drives the IDE health strip later.

## `_write_learned_record` change

After the JSON is written, call `ledger.record_injections(...)` with the same `sources`, but only
when `injected` is true. `run_id` comes from `os.path.basename(run_dir)`, or `MINI_ORK_RUN_ID`
when that is set. Wrap it in its own try/except. The ledger must never affect dispatch.

## Tests (`tests/unit/test_learning_ledger.py`, temp DB)

- `record_injections` inserts per source; a second identical call inserts 0; a source without an
  id is skipped.
- `injection_counts` aggregates uses / runs / last_ts and respects `since`.
- `stage_health`: 3 passes with inputs > 0 and outputs 0 → alarm with the last error; one
  non-zero output in the window → no alarm; a stage with inputs 0 → no alarm.
- `_write_learned_record` with a non-empty block writes ledger rows matching the JSON sources.
  With an empty block it writes none. With an unwritable DB path, files are still written and
  nothing raises.
- A DB without the migration works (`ensure_schema`).

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_learning_ledger.py tests/unit/test_learned_record.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/learning/ledger.py mini_ork/cli/execute_handlers.py tests/unit/test_learning_ledger.py` → clean.
- `git diff --stat` touches only the files in scope, and in `execute_handlers.py` only `_write_learned_record`.
