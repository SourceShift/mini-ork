# Budget guards and run cost read the llm_calls ledger, not task_runs.cost_usd

## Goal

`task_runs.cost_usd` only carries what a node handler explicitly charged. Stage
spend (the profile answerer before a run, the rubric judge, gradient extraction
and reflection after it, the jury, lens panels) is recorded only in `llm_calls`,
each row stamped with its `run_id`. Measured on a real `docs` run
(run-1791012731-afb70f): task_runs said $0.394, its `llm_calls` rows sum to
$1.75 (profile_answerer 0.26, worker 0.39, rubric 0.39, gradient-extract 0.31 +
0.40). `mini_ork.scheduler.today_cost_usd` was already moved to the ledger for
exactly this reason (read its docstring), but four other budget guards still sum
`task_runs` and so see roughly a third of real spend. Separately, the ACP/web
run views gather a run's `llm_calls` by trace id plus a plain time window, which
also picks up every concurrent run's calls in that window.

Make every budget guard read the ledger through one shared function, and make a
run's `llm_calls` mean the rows stamped with that run's id.

## Files in scope

- `mini_ork/cost_ledger.py` — NEW: the shared ledger reader.
- `mini_ork/scheduler.py` — `today_cost_usd` delegates to the new function (keep its name and behaviour).
- `mini_ork/dispatch/llm_dispatch.py` — `cost_circuit_open` and the `spent_today=` message near line 437.
- `mini_ork/cli/main.py` — `_spent_last_24h` (display mirror of the circuit).
- `mini_ork/cli/self_improve.py` — `pre_iter_cost_check`.
- `mini_ork/orchestration/conductor.py` — `_today_cost`.
- `mini_ork/web/repositories.py` — `RunDetailRepository.fetch_llm_calls_by_run_id` (NEW method).
- `mini_ork/acp/history.py` — `read_snapshot`.
- `mini_ork/web/routes/run_detail.py` — the llm-calls bridge near line 605–620.
- Tests: `tests/unit/test_cost_ledger.py` (NEW), and the existing tests of the guards above
  (`tests/unit/test_scheduler_py.py`, `tests/unit/test_mini_ork_execute_py.py`,
  `tests/unit/test_dispatch_shape_check_py.py`, plus any other test that seeds only
  `task_runs` cost to trip one of these guards — find them with
  `grep -rn "cost_circuit_open\|pre_iter_cost_check\|_today_cost\|_spent_last_24h\|today_cost_usd" tests`).
  `tests/unit/test_acp_history.py` for the snapshot change.

No other file changes.

## Mechanism (exact spec)

### `mini_ork/cost_ledger.py`

```python
def spent_last_24h(db: str | os.PathLike | None) -> float
```

- Missing/None db path or a file that does not exist → `0.0`.
- Open read-only (`file:<db>?mode=ro`, `uri=True`), `PRAGMA busy_timeout=5000`.
- `SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S','now','-24 hours')`
  (llm_calls.ts is ISO-8601 text, e.g. `2026-10-03T07:32:35.320Z`; this is the
  query `scheduler.today_cost_usd` uses today).
- `sqlite3.OperationalError` on that query (no llm_calls table) → fall back to
  `SELECT COALESCE(SUM(cost_usd),0) FROM task_runs WHERE created_at >= ?` with
  `int(time.time()) - 86400`; an error there too → `0.0`.
- Any other `sqlite3.Error` → `0.0`. Always close the connection.

### Callers

- `scheduler.today_cost_usd(db=None)`: resolve the db exactly as today (`_conn`'s
  path logic), then `return cost_ledger.spent_last_24h(path)`.
- `llm_dispatch.cost_circuit_open(db, budget)`: `return spent_last_24h(db) >= float(budget)`
  (keep: missing db → False). The `[cost_circuit_open] spent_today=$…` message uses
  the same function.
- `cli/main._spent_last_24h(db)`: returns `spent_last_24h(db)`; on exception `None`
  as today. Update its docstring (it mirrors the circuit, which now reads the ledger).
- `cli/self_improve.pre_iter_cost_check(db, budget)`: keep the env switch and the
  missing-file guard; compare `spent_last_24h(db)`.
- `orchestration/conductor._today_cost(db)`: `return spent_last_24h(db)`.

### A run's llm_calls

- `RunDetailRepository.fetch_llm_calls_by_run_id(run_id) -> list[dict]`: same
  column list as the other two fetchers (`self._llm_calls_select()`),
  `WHERE run_id = ? ORDER BY ts ASC`; `[]` when the table or the column is missing
  (guard with `has_table` and catch `sqlite3.OperationalError`).
- `acp/history.read_snapshot` and the `run_detail` route's bridge: call
  `fetch_llm_calls_by_run_id(run_id)` first; when it returns rows, use exactly
  those; only when it returns none, use the existing trace-id + time-window bridge
  (unchanged, for old databases whose rows predate `run_id`).

## Tests

`tests/unit/test_cost_ledger.py` (temp sqlite files built with the minimal
`llm_calls(id, run_id, cost_usd, ts)` and `task_runs(id, cost_usd, created_at)`
columns; timestamps relative to now — never absolute dates):
- ledger sum includes stage rows that task_runs does not see (task_runs 0.39,
  llm_calls 0.26 + 0.39 + 0.39 → 1.04);
- rows older than 24h are excluded;
- no llm_calls table → task_runs fallback; no db → 0.0; garbage file → 0.0.

Guards: for each of `cost_circuit_open`, `pre_iter_cost_check`, `_today_cost`,
`_spent_last_24h`, `today_cost_usd` — a db whose task_runs spend is under budget
but whose llm_calls spend is over budget trips the guard (or reports the ledger
sum). Update existing tests that seeded only task_runs: seed `llm_calls` instead
(or as well) so they still test what they meant to; say so in a comment.

`tests/unit/test_acp_history.py`: a snapshot for run A ignores a concurrent run
B's calls in the same window when A's rows carry `run_id`; with no `run_id` rows
the old bridge still applies.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_cost_ledger.py tests/unit/test_scheduler_py.py tests/unit/test_mini_ork_execute_py.py tests/unit/test_dispatch_shape_check_py.py tests/unit/test_acp_history.py tests/unit/test_acp_agent_py.py` passes.
- `grep -rn "FROM task_runs" mini_ork/dispatch/llm_dispatch.py mini_ork/cli/main.py mini_ork/cli/self_improve.py mini_ork/orchestration/conductor.py` finds no budget-sum query left.
- `ruff check` clean; diff touches only the files in scope.
