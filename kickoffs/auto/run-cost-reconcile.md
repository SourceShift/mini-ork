# A run's reported cost is everything it spent

## Goal

`task_runs.cost_usd` only carries what node handlers charged. A run also pays for
stage calls — the profile answerer before it, the rubric judge, reflection and gradient
extraction after it — and those are recorded only in `llm_calls`, stamped with the run's
`run_id`. Measured on a real `docs` run: task_runs said $0.394, its llm_calls rows summed
$1.75. The budget guards already read the ledger (`mini_ork/cost_ledger.py`), but every
place that reports a run's cost reads task_runs: the web UI's cost and fleet views, the
`mcp-context` tools the Zed orchestrator uses (`list_runs`, `run_status`,
`wait_for_run`, the `cost` tool), and the ACP history. In Zed the orchestrator told the
user "$0.39" for a run whose thread showed $0.65.

Fix it at the source: when a `mini-ork run` lifecycle finishes — after verify, rubric,
reflect — set the run's `task_runs.cost_usd` to its full ledger total, and report it in
the `mini_ork_result` line.

## Files in scope

- `mini_ork/cost_ledger.py` — add two functions.
- `mini_ork/cli/main.py` — call it at the end of `_run_lifecycle`.
- `mini_ork/mcp_context/server.py` — `list_runs` reports a total.
- `tests/unit/test_cost_ledger.py`, `tests/unit/test_mcp_context_server.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `cost_ledger.py`

```python
def run_cost(db: str | os.PathLike | None, run_id: str) -> float | None
def reconcile_run_cost(db: str | os.PathLike | None, run_id: str) -> float | None
```

- `run_cost`: `SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE run_id = ?`;
  `None` when the db/table/column is missing, any `sqlite3.Error`, or no rows for the run;
  else the sum. Same plain-connection rule as `spent_last_24h` (never `mode=ro` — see its
  comment).
- `reconcile_run_cost`: `ledger = run_cost(db, run_id)`; read the run's
  `task_runs.cost_usd` (`None` when no row → return `None`, change nothing). When `ledger`
  is not `None` and is greater than the stored value, `UPDATE task_runs SET cost_usd = ?,
  updated_at = <now epoch> WHERE id = ?` (never lower it: a node handler may charge spend
  the ledger lacks) and return the new value; otherwise return the stored value. Never
  raises (errors → `None`). Commit and close.

### `cli/main._run_lifecycle`

After `_run_lifecycle_impl` returns (inside the existing structure, before the
`emit_json` block, on every return path, including a failed run): when `sink` has a
`run_id` and `MINI_ORK_DRY_RUN` is not `"1"`, `cost = reconcile_run_cost(<db>, run_id)`
where `<db>` is `context_env("MINI_ORK_DB")` or `<MINI_ORK_HOME>/state.db` (resolve it the
way the lifecycle already does — read the module before coding); when `cost` is not
`None`, set `sink["cost_usd"] = round(cost, 6)`. Wrap in `try/except Exception` — cost
reporting must never change a run's return code.

### `mcp_context/server._list_runs`

Add `"total"`: the number of runs matching the same filter (`SELECT COUNT(*) FROM
task_runs [WHERE status = ?]`), so a caller cannot mistake the page for the whole set
(observed: the orchestrator answered "10 runs" from a default page of 10). Update the tool
description: "`total` is the number of matching runs; `runs` holds at most `limit`
(default 20)." Keep every existing field.

## Tests

`tests/unit/test_cost_ledger.py` (temp dbs, relative timestamps): `run_cost` sums only the
run's rows, `None` for no rows / no table / no db; `reconcile_run_cost` raises the stored
value to the ledger total, never lowers it, returns `None` and writes nothing for an
unknown run, survives a WAL db with no sidecar files.

A lifecycle test (add to `tests/unit/test_cost_ledger.py` or the existing lifecycle test
module that drives `_run_lifecycle` with fakes — find it with
`grep -rln "_run_lifecycle" tests`): with `_run_lifecycle_impl` monkeypatched to put a
`run_id` in the sink and seed llm_calls rows, the emitted `mini_ork_result` carries
`cost_usd` and the task_runs row is updated; a reconcile error does not change the return
code; a dry run does not reconcile.

`tests/unit/test_mcp_context_server.py`: `list_runs` returns `total` (with and without a
status filter) larger than the page when there are more runs than `limit`.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_cost_ledger.py tests/unit/test_mcp_context_server.py` plus the lifecycle test module pass.
- `ruff check` clean; diff touches only the files in scope (plus the lifecycle test module if used).
