# G1 — measure the router's calibration error and the gate's blind spot

## Goal

mini-ork routes with UCCI: `dispatch/calibration.py` fits an isotonic map from the
router's `route_margin` to an observed error rate, and `steering/decision_service.py`
`decide()` escalates a lane when that calibrated error crosses `target_error()`. The
prediction it makes is computed, returned as `predicted_error`, and then **thrown away** —
it is never persisted, so nothing can check whether the map is right.

This cycle closes that loop and adds the measurement the cost-down claim depends on.
Deliver two numbers over `execution_traces`:

- **reliability** — is `predicted_error` actually calibrated? (reliability diagram + ECE + Brier)
- **blind spot** — of the rows the router **declined to escalate** (`predicted_error <=
  target_error()`), what fraction of them errored anyway? That is the absorbed error the
  cheap lane cost, and the number that decides whether the cascade is honest.

Reference: arXiv 2605.18796 (UCCI) and 2609.01345 ("Cheap Verifiers, Large Blind Spots").

## What already exists (read it; do not rewrite it)

- `mini_ork/dispatch/calibration.py` — `pav`, `fit_error_map`, `error_probability`,
  `load_margin_rows`, `calibrated_error`, `should_escalate`, `target_error`, `min_samples`,
  `_enabled`, `clear_cache`. The new module is a **sibling**, not a replacement. Reuse
  `target_error()` for the threshold; do **not** re-derive it.
- `mini_ork/steering/decision_service.py` `decide()` — already returns
  `predicted_error` (line ~393). It is the value to persist.
- `mini_ork/dispatch/routing.py` — around line 100-115, the call site that builds the
  provenance record from `decide()`. `route_margin=decision.get("route_margin")` is there.
- `mini_ork/trace_store.py` — `record_trace` (line ~98): one `INSERT … ON CONFLICT` with the
  full column list, the VALUES placeholders, the `DO UPDATE SET` list, and a positional
  params tuple. `route_margin` is the last column in each of the four places.
- `db/migrations/0057_execution_traces_route_margin.sql` — the exact shape to copy. Latest
  applied migration is `0058_semantic_memory_attribution.sql`.

## Deliverable 1 — persist `predicted_error`

1. **`db/migrations/0059_execution_traces_predicted_error.sql`** — modelled on 0057:
   `ALTER TABLE execution_traces ADD COLUMN predicted_error REAL DEFAULT NULL;` plus a
   partial index on the same predicate style. Nullable for the same three reasons 0057
   documents (pre-migration rows, non-learned routes, exploration swaps) — say so in the
   header comment.

2. **`mini_ork/trace_store.py`** — add the column to all four places: the INSERT column
   list, one more `?`, the `ON CONFLICT DO UPDATE SET` line (use the same
   `COALESCE(excluded.X, X)` shape as `route_margin`), and the params tuple
   (`float(p["predicted_error"]) if p.get("predicted_error") is not None else None`).

3. **`mini_ork/dispatch/routing.py`** — where the provenance record is built from
   `decide()`, add `predicted_error=decision.get("predicted_error")`.

## Deliverable 2 — `mini_ork/learning/calibration_backtest.py` (new)

Pure functions over rows; DB access in one loader. No numpy. Match `calibration.py`'s
style: module docstring explaining *why*, fail-open on a database without the column.

Public surface (names are the contract):

- `load_prediction_rows(db: str, task_class: str = "") -> list[tuple[float, bool]]`
  — `(predicted_error, is_error)` for rows where `predicted_error IS NOT NULL`;
  `is_error` is `status != 'success'` (same definition `calibration.load_margin_rows`
  uses — reuse the reasoning, keep the two definitions identical). Empty list on a
  missing file, missing table, or missing column.
- `reliability(rows, bins: int = 10) -> list[dict]` — equal-width bins over `[0,1]`;
  each entry `{"lo", "hi", "n", "mean_predicted", "observed_rate"}`. Empty bins are
  omitted, not emitted with `n=0`.
- `ece(rows, bins: int = 10) -> float | None` — sample-weighted mean of
  `|observed_rate - mean_predicted|`; `None` for an empty `rows`.
- `brier(rows) -> float | None` — mean of `(predicted_error - is_error)**2`; `None` for
  an empty `rows`.
- `blind_spot(rows, target: float, *, include_boundary: bool = True) -> dict` — among
  rows the router **did not escalate** (`predicted_error <= target` when
  `include_boundary`), return
  `{"n_kept", "n_kept_errored", "blind_spot_rate", "n_escalated", "n_escalated_errored"}`.
  `blind_spot_rate` is `None` when `n_kept == 0` (no rows to speak of — do not invent 0.0).
- `summarize(db, task_class="", *, bins=10) -> dict` — one call returning
  `{"n", "ece", "brier", "target", "blind_spot": {...}, "reliability": [...]}`, reading
  `target` from `calibration.target_error()`. A thin composition, no extra logic.

## Deliverable 3 — `mini-ork calibrate` (new CLI)

- **`mini_ork/cli/calibrate.py`** — `main(rest, root) -> int` plus a `__main__` block
  (the registry runs it as `python -m mini_ork.cli.calibrate`).
  - `--task-class <name>` (default: all task classes), `--bins <n>` (default 10),
    `--json` (emit the `summarize` dict as JSON), `--help`.
  - Human output: the reliability table, then `ECE`, `Brier`, and a line naming the
    blind spot (`kept=<n> errored=<k> rate=<r>`), then `escalated=<n> errored=<k>`.
  - **A database with no `predicted_error` column must print a clear "no predictions
    recorded yet" line and exit 0** — not a traceback, not a non-zero code. The column
    will be empty on every existing DB the moment it lands; that is the expected first
    run, not an error.
  - Diagnostics to stderr; the JSON payload is the only thing on stdout under `--json`.
- **`mini_ork/cli/main.py`** — add `"calibrate": "mini_ork.cli.calibrate"` to
  `_NATIVE_MODULE_SUBS`.
- **`tests/unit/test_native_dispatch_py.py`** — add `"calibrate"` to the `expected` set in
  `test_all_former_exec_subs_registered_natively`. That exact-set assertion is a real
  contract; leaving it red is a failure.

## Tests — `tests/unit/test_calibration_backtest_py.py` (new)

Hermetic: build a temp SQLite `execution_traces` by hand; no lane, no network, no real run.
The editable install resolves `mini_ork` to **main**, so insert the repo root on `sys.path`
before importing:

```python
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
```

**These assertions are the contract** — implement all of them; do not reword, weaken, or
drop one to match what you built:

1. `ece` and `brier` on a **perfectly calibrated** synthetic set (predicted == observed in
   every bin) return ~0 (use `pytest.approx`); on a set where every prediction is 0.0 and
   every row errored, `brier == 1.0`.
2. `blind_spot` counts only the kept rows: a fixture with 3 kept (1 errored) and 2
   escalated (2 errored) yields `n_kept == 3`, `n_kept_errored == 1`,
   `blind_spot_rate == pytest.approx(1/3)`, `n_escalated == 2`.
3. `blind_spot` returns `blind_spot_rate is None` when `n_kept == 0` — not `0.0`.
4. The boundary is inclusive: a row with `predicted_error == target` is counted as **kept**.
5. `reliability` omits empty bins and its `mean_predicted` is non-decreasing across
   emitted bins for a monotone fixture.
6. `load_prediction_rows` returns `[]` for a database whose `execution_traces` lacks the
   `predicted_error` column (build one without it) — the fail-open path, which is what
   every pre-0059 database is.
7. `summarize` reports `target == calibration.target_error()` and passes the same rows
   through `blind_spot` as an explicit call would (assert the two agree exactly).
8. `trace_store.record_trace` round-trips `predicted_error`: write a row with
   `predicted_error=0.42`, read it back, assert `0.42`; write a second trace for the same
   `trace_id` with `predicted_error=None`, assert the stored value is **still 0.42**
   (the COALESCE, mirroring `test_route_margin_roundtrip` in `test_trace_store_py.py`).

## Files in scope

- `db/migrations/0059_execution_traces_predicted_error.sql` (new)
- `mini_ork/learning/calibration_backtest.py` (new)
- `mini_ork/cli/calibrate.py` (new)
- `mini_ork/cli/main.py` — one line in `_NATIVE_MODULE_SUBS`
- `mini_ork/trace_store.py` — the four `predicted_error` sites
- `mini_ork/dispatch/routing.py` — one provenance field
- `tests/unit/test_calibration_backtest_py.py` (new)
- `tests/unit/test_native_dispatch_py.py` — one name in `expected`
- `tests/unit/test_trace_store_py.py` — nothing required; leave `test_route_margin_roundtrip` green

Do **not** touch `dispatch/calibration.py` (reuse it, do not edit it), `web/**`, any recipe,
any probe, `observability/node_events.py`, or `steering/decision_service.py`. Do not add a
second escalation path, a new routing policy, or new SQL against `task_runs` / `llm_calls`.

## Verification commands

Run from the worktree root with `python3.11` explicitly — the ambient `python3` is 3.9 and
dies at collection.

```bash
python3.11 -m pytest tests/unit/test_calibration_backtest_py.py -q
python3.11 -m pytest tests/unit/test_trace_store_py.py tests/unit/test_native_dispatch_py.py -q
python3.11 -m py_compile mini_ork/learning/calibration_backtest.py mini_ork/cli/calibrate.py
python3.11 -c "from mini_ork.cli.main import SUBCOMMAND_REGISTRY as R; assert 'calibrate' in R, sorted(R); print('calibrate registered')"
python3.11 -c "import mini_ork.cli.calibrate" | wc -c    # expect 0 — stdout is data
```

## Self-application measurement

Zero model spend. Apply the migration to a **scratch copy** of a live database, then run
the real CLI against it:

```bash
cp /tmp/mo-rsi-home/state.db /tmp/g1-scratch.db
MINI_ORK_DB=/tmp/g1-scratch.db python3.11 -m mini_ork.cli.migrate apply   # or the repo's migrate entrypoint
MINI_ORK_DB=/tmp/g1-scratch.db python3.11 -m mini_ork.cli.calibrate --json
```

The expected honest result is `n == 0` on today's data: the column is new, so no row has a
prediction yet. **Report that number, not a projection of what it will be.** Do not claim a
non-empty blind-spot measurement you did not take — say "no predictions recorded yet" and
give the count. Never mutate the live `/tmp/mo-rsi-home/state.db`.

Evidence artifact `${MINI_ORK_RUN_DIR}/rsi-g1-blindspot.json`:

```json
{"migration_applies": true, "predicted_error_roundtrips": true,
 "ece_computes": true, "blind_spot_rate_computes": true,
 "cli_import_stdout_bytes": 0, "scratch_rows_with_prediction": <int>,
 "live_db_untouched": true, "native_dispatch_test_green": true}
```

## Done When

- `tests/unit/test_calibration_backtest_py.py` is green and `test_native_dispatch_py.py` is
  green with `"calibrate"` in its expected set.
- `python3.11 -c "import mini_ork.cli.calibrate" | wc -c` prints `0`.
- `db/migrations/0059_execution_traces_predicted_error.sql` exists and applies cleanly to a
  copy of a live database.
- `record_trace` persists `predicted_error` and `decide()`'s value reaches it — verifiable by
  reading the three edited files, not by reading a summary.
- `${MINI_ORK_RUN_DIR}/rsi-g1-blindspot.json` exists with the fields above, and the
  `scratch_rows_with_prediction` value is the number you actually observed.

## If you cannot finish

Say so — with the failing command and its output — rather than narrowing the tests or
reporting a partial build as done. A precise partial result beats a green claim that does
not survive `git diff`.
