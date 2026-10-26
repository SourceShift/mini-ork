# rev6 · R5b: record rounds-to-pass for every run with a revise loop, and report it

## Goal

`revise_rounds` (`db/migrations/0081_revise_rounds.sql`) has had no writer
since eco6 K2 (3de6813e) deleted `mini_ork/learning/revise_value.py`. It holds
18 rows, so nobody can tell whether a reviewer change makes the loop converge
faster. The table:

```sql
CREATE TABLE IF NOT EXISTS revise_rounds (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, task_class TEXT,
    target_node TEXT NOT NULL, round INTEGER NOT NULL, passed INTEGER,
    cost_usd REAL, created_at INTEGER NOT NULL, UNIQUE (run_id, target_node, round));
```

Record one row per attempt of each retry target: round 0 is the first attempt,
round n the n-th revise round. `passed` says whether that attempt ended with
every gate green. Then add `mini-ork metrics revise` to report the distribution.

The executor's round counter (`round_used` in `_dispatch_dependency_graph`,
`mini_ork/cli/execute.py`) is in memory, so a `recover` process starts again at
1. The writer therefore numbers rows from an offset read from the table, and a
recover's attempts continue the run's numbering instead of overwriting it.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/stores/revise_rounds.py` (new)

```
def next_round(db: str, run_id: str, target: str) -> int
def record_grant(db: str, run_id: str, task_class: str | None, target: str,
                 db_round: int, cost_usd: float | None = None, now: int | None = None) -> None
def record_outcome(db: str, run_id: str, task_class: str | None, target: str,
                   db_round: int, passed: bool, cost_usd: float | None = None,
                   now: int | None = None) -> None
```

- **`next_round`** returns `MAX(round) + 1` over rows for `(run_id, target)`,
  or 0 when there are none.
- **`record_grant(db_round)`** means attempt `db_round - 1` failed and attempt
  `db_round` starts. It writes two rows:
  - upserts row `db_round - 1` with `passed = 0`;
  - inserts row `db_round` with `passed = NULL` (`INSERT OR IGNORE`).
- **`record_outcome`** upserts row `db_round` with `passed = 1 if passed else 0`.
- **Upsert** means
  `INSERT INTO revise_rounds (...) VALUES (...) ON CONFLICT(run_id, target_node, round) DO UPDATE SET passed = excluded.passed, cost_usd = COALESCE(excluded.cost_usd, revise_rounds.cost_usd)`.
  `created_at` stays the first insert's value.
- **Never raise.** All three functions return silently when `db` is empty, is
  not a file, or lacks the table, and on any `sqlite3.Error`. Connect with
  `timeout=15` and `PRAGMA busy_timeout=15000`.

### 2. `mini_ork/cli/execute.py`, inside `_dispatch_dependency_graph`

1. **Before the scheduling loop:** `retry_targets` = the sorted set of
   `edge.target` over the `retries`-type edges in `repair_edges` (the
   `retry_edges` dict built from `compile_workflow`; not the `escalates_to`
   edges). When `not dry_run and run_id`, set
   `rr_base = {t: revise_rounds.next_round(db, run_id, t) for t in retry_targets}`;
   otherwise `rr_base = {}`.
2. **When a round is granted** (right after `round_used[target] = round_no`), if
   `target in rr_base`:
   `revise_rounds.record_grant(db, run_id, task_class, target, rr_base[target] + round_no, cost_usd=<run cost>)`.
3. **When the scheduling loop has finished**, before the function returns, for
   every `t in rr_base`:
   `revise_rounds.record_outcome(db, run_id, task_class, t, rr_base[t] + round_used.get(t, 0), passed=(fail_count == 0), cost_usd=<run cost>)`.
   `fail_count` is the function's own failure count at that point (before
   rollback).
4. `<run cost>` is `_cost_ledger.run_cost(db, run_id)` (already used near the
   budget check), wrapped so any exception gives `None`.
5. Import `mini_ork.stores.revise_rounds` at module top. Change nothing else in
   the loop: no cap, edge or round-counter behaviour.

### 3. `mini_ork/cli/metrics_revise.py` (new) + dispatch in `mini_ork/cli/metrics.py`

`mini-ork metrics revise [--db PATH] [--days N] [--task-class X] [--runs-dir DIR] [--json]`,
following the `metrics_sdd.main` argparse pattern. The default DB is the same as
`metrics_sdd._default_db()`; import and reuse it.

- **`revise_rounds` section**, over rows with `created_at >= now - days*86400`
  (default days 30), optionally filtered by `task_class`. Per task class:
  - `runs`: distinct run_ids;
  - `passed_first_try`: runs whose row 0 has passed=1;
  - `rounds_to_pass`: a histogram `{n: count}` of the smallest round with
    passed=1, per (run_id, target_node);
  - `failed`: runs with no passed=1 row and a final row (max round) passed=0;
  - `open`: runs whose max-round row is NULL.
- **`history` section**: per run, the implementer attempts from `node_attempts`
  (`MAX(attempt_no)` for `node_id='implementer'`), joined to
  `task_runs.task_class`, over the same window, as a histogram. Label it
  "implementer attempts per run (node_attempts)". A missing table → section
  omitted.
- **`convergence` section** (when `--runs-dir`, default `<db dir>/runs`,
  exists): over run dirs modified within the window, read `revise/findings.json`
  (the R5a review ledger: `{"rounds": [{"new", "reported_again", "fixed", "reopened", "outside_delta", ...}]}`)
  and sum each counter. Report `runs_with_ledger` and the sums. Skip unreadable
  files.
- **Output:** markdown tables by default; `--json` prints one sorted-keys
  object `{"revise_rounds": ..., "history": ..., "convergence": ..., "window_days": N}`.
- **Exit codes:** rc 0; no DB → message on stderr and rc 1.

In `mini_ork/cli/metrics.py` `main`, add next to the `sdd`/`context`
dispatches:
`if args[:1] == ["revise"]: from mini_ork.cli import metrics_revise; return metrics_revise.main(args[1:], stdout=out, stderr=err)`.
Add one line for `revise` to `_usage()`.

### 4. `docs/operator/feature-flags.md`

Add a row: `MO_REVIEW_CONVERGE` | `"on"` | `off`: the classic reviewer gets no
earlier-findings block or "changes since your last review" delta (the findings
ledger `revise/findings.json` is still written). Add one sentence pointing to
`mini-ork metrics revise` for rounds-to-pass.

## Files in scope (touch ONLY these)

- `mini_ork/stores/revise_rounds.py` (new)
- `mini_ork/cli/execute.py`
- `mini_ork/cli/metrics_revise.py` (new)
- `mini_ork/cli/metrics.py`
- `docs/operator/feature-flags.md`
- `tests/unit/test_revise_rounds_store.py` (new)
- `tests/unit/test_metrics_revise.py` (new)

Do NOT modify `db/migrations/0081_revise_rounds.sql` or any migration, the
round cap / edge logic, `mini_ork/cli/execute_handlers.py`, or any path in
`config/kernel-frozen.yaml`.

## Tests (hermetic)

Use a DB from `mini_ork.stores.migrate.init_db(db=..., root=REPO)` (assert rc 0).

`tests/unit/test_revise_rounds_store.py`:
1. `next_round` is 0 on an empty table.
2. A run that passes first try writes `record_outcome(..., 0, True)` → one row, round 0, passed 1.
3. `record_grant(…, 1)` then `record_outcome(…, 1, True)` → rows (0, 0) and (1, 1).
4. A second process (recover) starts at `next_round` = 2: `record_grant(…, 3)` then `record_outcome(…, 3, False)` → rows 2 and 3 exist; 3 has passed=0; rows 0 and 1 are unchanged.
5. A missing DB file, or a DB without the table → no exception.
6. `cost_usd=None` on a later upsert keeps an earlier non-null cost.

`tests/unit/test_metrics_revise.py`:
7. Seed rows for three runs: first-try pass, pass at round 2, fail at round 2 → `--json` reports `passed_first_try=1`, `rounds_to_pass={"0":1,"2":1}`, `failed=1`.
8. Seed `node_attempts` + `task_runs` → the `history` histogram counts them.
9. A run dir with `revise/findings.json` rounds → the `convergence` sums match.
10. `mini-ork metrics revise --help` (via `mini_ork.cli.metrics.main(["revise", "--help"])`) exits 0, and a missing DB gives rc 1.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_revise_rounds_store.py tests/unit/test_metrics_revise.py tests/unit/test_eco6_k2_revise_caps.py tests/unit/test_review_rounds.py tests/unit/test_revise_error_lines.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_revise_rounds_store.py tests/unit/test_metrics_revise.py --junitxml="${MINI_ORK_RUN_DIR}/rev6-r5b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rev6-r5b-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/stores/revise_rounds.py mini_ork/cli/metrics_revise.py mini_ork/cli/metrics.py mini_ork/cli/execute.py` is clean.
- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_mini_ork_execute_py.py` passes.
- A database error in the writer can never change a run's outcome.
