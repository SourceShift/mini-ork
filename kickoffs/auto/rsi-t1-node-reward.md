# RSI Tier-1 #1: ground the implementer's node reward in the run's verifier results

## Goal

Every node trace is stamped with `reward_from_status(status, verdict)` by
`_make_trace_fn` (`mini_ork/cli/execute.py`, the `MO_REWARD_STAMP` block). A
`success` row gets `reward_value = 1.0`, whatever the verifiers then found.
Nothing changes it afterwards: `reward_source` keeps the table default
`verifier@v1`, which names no verifier. The only override,
`MO_IMPL_REWARD=run_verified`, is opt-in and run-level (`fail_count == 0`). So
the reward the router learns from is the exit code (LLIF F1/S1: "a wrong S1
poisons all learning").

Measured on the primary `state.db`, `created_at >= '2026-10-16'`:

- 1,742 `verifier@v1` traces, mean `reward_value` 0.972. 1,662 of them (95.4%)
  are at 1.0, and every `status='success'` row is exactly 1.0.
- 243 implementer traces: mean 0.959, 233 at 1.0, 10 at 0.0.
- The evidence already exists. 180 of the 198 runs with these traces have
  `verifier_results` rows, and 41 of them have at least one `fail`. Since 10-16
  `spec-tests` alone recorded 54 fails and `test` recorded 20.

Who reads the stamp: `lane_router.recompute_advantages` (step 3 of
`_post_run_learning`) reads `reward_g`. It is the last writer of
`agent_performance_memory.relative_advantage` and of `lane_domain_advantage`,
`lane_region_advantage` and `lane_slice_baseline`; `decide()` routes from
those tables. `write_grpo_advantages` reads `process_reward`, then
status/verdict, never `reward_value`, so this change does not touch it. Other
readers (`decision_service.reward_summary`, the GEPA adapter, the reflection
per-node credit) already take a continuous `reward_g` in [-1, 1].

The fix: after the run, re-stamp each successful implementer trace with the
pass rate of the verifier rows recorded during that attempt. The window runs
from the trace's own `created_at` to the next implementer trace of the same
run. Replaying exactly this rule read-only over the 243 implementer traces
since 10-16:

- 231 have evidence in their window, and 85 of those move below 1.0.
- The share at 1.0 falls from 233 to 148, and the mean from 0.959 to 0.813.
- The new values are 0.0 ×13, 0.5 ×19, 0.67 ×1, 0.75 ×49, 0.88 ×3 and 1.0 ×146.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/writeback.py`

- **New module constants**, placed right after `_REWARD_APPROVE_VERDICTS`:

  ```
  ARTIFACT_REWARD_SOURCE = "artifact@v1"
  ARTIFACT_REWARD_METRIC = "verifier_pass_rate"
  ARTIFACT_REWARD_NODE_TYPES = ("implementer",)
  _EXIT_CODE_REWARD_SOURCES = ("", "verifier@v1", ARTIFACT_REWARD_SOURCE)
  ```

- **New function**
  `artifact_window_counts(con: sqlite3.Connection, run_id, start_epoch: int, end_epoch: int | None = None) -> tuple[int, int]`.
  - It runs
    `SELECT verdict, is_false_positive FROM verifier_results WHERE run_id=? AND created_at>=?`,
    plus `AND created_at<?` when `end_epoch` is not None. `run_id` is bound as
    `str(run_id)`.
  - It returns `(passes, fails)`:
    - a **pass** is `verdict == 'pass'` with `is_false_positive` 0 or NULL;
    - a **fail** is `verdict == 'fail'`, or `verdict == 'pass'` with
      `is_false_positive == 1`;
    - `indeterminate` and `vacuous` rows count as neither.
  - `sqlite3.OperationalError` (no table) returns `(0, 0)`.
  - It never commits and never closes `con`.
- **New function**
  `artifact_reward(passes: int, fails: int) -> float | None`. It returns `None`
  when `passes + fails == 0`, else `passes / (passes + fails)`.
- **New function** `stamp_artifact_rewards(db, run_id) -> int`.
  1. Return `0` when `db` is falsy, `db` is not an existing file, or `run_id` is
     falsy.
  2. Open `sqlite3.connect(db, timeout=5.0)` and run
     `PRAGMA busy_timeout=5000`.
  3. Read the `execution_traces` columns with `PRAGMA table_info`. Return `0`
     unless all of these are present: `run_id`, `status`, `verifier_output`,
     `created_at`, `reward_value`, `reward_anchor`, `reward_g`,
     `reward_source`.
  4. Read the run's implementer rows:

     ```
     SELECT trace_id, status, COALESCE(reward_source, ''), <V>,
            CAST(strftime('%s', created_at) AS INTEGER)
       FROM execution_traces
      WHERE run_id=? AND json_extract(verifier_output, '$.node_type') IN (<ARTIFACT_REWARD_NODE_TYPES>)
      ORDER BY created_at, trace_id
     ```

     `<V>` is `COALESCE(validity, 'valid')` when the `validity` column exists,
     else the literal `'valid'`. Drop the rows whose epoch is NULL.
  5. **The attempt window.** For the i-th remaining row:
     - `start` is its own epoch;
     - `end` is the epoch of row i+1, or `None` for the last row.

     Every implementer row bounds a window, whether or not it is eligible.
  6. **Eligibility.** A row is re-stamped only when all of these hold:
     - `status == 'success'`;
     - its `reward_source` is in `_EXIT_CODE_REWARD_SOURCES`;
     - `str(validity).strip().lower()` is `''` or `'valid'`;
     - `artifact_reward(*artifact_window_counts(con, run_id, start, end))` is
       not None.
  7. **The write.** For an eligible row with value `v`, update it with:
     - `reward_value=v`;
     - `reward_anchor=0.5`;
     - `reward_g=(v - 0.5) / 0.5`;
     - `reward_source='artifact@v1'`;
     - `reward_primary_metric='verifier_pass_rate'` and
       `reward_direction='higher_is_better'`, each only when that column exists.

     The `WHERE` clause is `trace_id=?`. No other column changes.
  8. Commit once and return the number of rows updated.
  9. It never raises. Any `sqlite3.Error` returns `0`, and the connection is
     closed in a `finally`.

  Re-stamping a row already at `artifact@v1` with the same evidence writes the
  same values, so the function is idempotent.
- **Nothing else changes in `writeback.py`:**
  - not `reward_from_status`;
  - not `write_grpo_advantages` (it does not read `reward_value`);
  - not `stamp_impl_run_verified`.

### 2. `mini_ork/cli/execute.py`, `_post_run_learning`

- **The call.** Insert this block right after the existing
  `MO_IMPL_REWARD == "run_verified"` block and before the
  `MO_GRADE_RUN_REWARD` block:

  ```
  if os.environ.get("MO_ARTIFACT_REWARD", "1") != "0":
      try:
          from mini_ork.learning import writeback as _wb_artifact  # noqa: PLC0415
          _wb_artifact.stamp_artifact_rewards(db, run_id)
      except Exception:
          pass
  ```

  It runs whatever `fail_count` is.
- **Why it sits there:**
  - after `run_verified`, so a row that stamp wrote (`run_verified@v1`) is no
    longer eligible and the operator's opt-in wins;
  - before the rubric fill (`grade_run_reward` fills only `reward_g IS NULL`) and
    before `recompute_advantages`, so the same run's recompute reads the
    grounded value.
- **The docstring.** Add an item `0b.` to the `_post_run_learning` docstring
  describing the step and the knob.
- **Nothing else changes:** not `_make_trace_fn`, not the `MO_REWARD_STAMP`
  block, not `reward_from_status`.

### 3. `docs/operator/feature-flags.md`

Add exactly this row to the "Execution / dispatch" table, directly below the
`MO_REWARD_STAMP` row:

```
| `MO_ARTIFACT_REWARD` | `"1"` | Post-run: re-stamp each successful implementer trace with the pass rate of the run's `verifier_results` rows recorded during that attempt (`reward_source='artifact@v1'`). `0` keeps the exit-code stamp |
```

### The default, and why

`MO_ARTIFACT_REWARD` defaults to ON. It changes live learning by design: the
implementer `reward_g` that `recompute_advantages` reads. It is safe to ship ON
for three reasons, each proven by a test below:

- It only rewrites rows that still carry the exit-code stamp, and only
  successful ones. A failure is never re-stamped, so evidence can confirm or
  lower a success's 1.0 but can never raise a failure.
- Rows without verifier evidence in their window are left exactly as they are.
- `MO_ARTIFACT_REWARD=0` restores today's rows. `MO_IMPL_REWARD=run_verified`
  still takes precedence.

There is no secret or remote surface. Both tiers (laptop SQLite and a shared
deployment's `state.db`) run the same code path, so one knob covers both.

## Files in scope (touch ONLY these)

- `mini_ork/learning/writeback.py`
- `mini_ork/cli/execute.py`
- `docs/operator/feature-flags.md`
- `tests/unit/test_artifact_node_reward.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_artifact_node_reward.py`: hermetic temp DB, no network, no dispatch)

### Fixtures

- **The DB.** Copy `_seed_db(tmp, name)` and `_sql(db, s)` from
  `tests/unit/test_impl_run_verified_reward.py`, adding `cwd=str(REPO)` to the
  `subprocess.run` in `_seed_db`. It runs `db/init.sh`, so the DB has the full
  schema:
  - `execution_traces.reward_source` defaults to `'verifier@v1'`;
  - `validity` defaults to `'valid'`;
  - `verifier_results.verdict` is CHECK-limited to
    `pass|fail|indeterminate|vacuous`;
  - `is_false_positive` is CHECK-limited to 0/1;
  - `verifier_results.created_at` is an INTEGER epoch.
- **Traces.** Copy `_insert_trace` from the same file and extend it:
  - signature
    `_insert_trace(db, tid, *, node_type="implementer", created_at=T0, run_id="r1", status="success", validity=None, reward_source=None)`;
  - insert `created_at` literally;
  - add the `validity` and `reward_source` columns only when they are not None;
  - leave `reward_value` unset, so it is NULL.
- **Verifier rows.** A new helper
  `_insert_vr(db, *, run_id="r1", verdict, created_at, fp=0, name="test")`
  inserts one `verifier_results` row with a unique `result_id`.
- **Reading back.** `_row(db, tid)` returns the `execution_traces` row as a dict.
- **Constants.**
  - `T0 = "2026-07-01T00:00:00Z"`; its epoch is `E0 = 1782864000`, the value
    `strftime('%s', T0)` returns.
  - `T1 = "2026-07-01T00:01:40Z"`, which is `E0 + 100`.
- **Environment.** An autouse fixture deletes `MO_ARTIFACT_REWARD` and
  `MO_IMPL_REWARD`.

### Tests

- **The pass rate is stamped.** One implementer trace at `T0` and verdicts
  `pass, pass, fail, pass`, each at `E0+10`. `stamp_artifact_rewards(db, "r1")`
  returns 1, and the row has:
  - `reward_value` 0.75;
  - `reward_g` 0.5;
  - `reward_anchor` 0.5;
  - `reward_source` `'artifact@v1'`;
  - `reward_primary_metric` `'verifier_pass_rate'`.
- **Each attempt gets its own window.**
  - Setup:
    - traces `a1` at `T0` and `a2` at `T1`;
    - `fail` at `E0+10` and `E0+20`;
    - `pass` at `E0+110` and `E0+120`.
  - The call returns 2.
  - `a1` has `reward_value` 0.0 and `reward_g` -1.0.
  - `a2` has `reward_value` 1.0 and `reward_g` 1.0.
- **Earlier rows do not count.** A `fail` at `E0-5` is all the evidence. The call
  returns 0, `reward_source` stays `'verifier@v1'`, and `reward_value` stays NULL.
- **Indeterminate and vacuous are ignored.** With only those verdicts in the
  window, the call returns 0 and the row is unchanged.
- **A false-positive pass is a fail.** A `pass` with `fp=1` plus a plain `pass`
  gives `reward_value` 0.5.
- **Ineligible rows are untouched** (parametrize, with `pass` and `fail`
  evidence in the window). The call returns 0 and the whole row dict is
  unchanged for each of:
  - `status="failure"`;
  - `validity="infra_failed"`;
  - `reward_source="run_verified@v1"`;
  - `reward_source="eval@v1"`;
  - `node_type="reviewer"`.
- **Other runs are isolated.** A `fail` for `run_id="r2"` and a `pass` for `"r1"`
  in the window give r1's trace `reward_value` 1.0.
- **Idempotent.** Two consecutive calls each return 1 and leave identical row
  dicts.
- **Fails soft.**
  - After `DROP TABLE verifier_results;`, the call returns 0.
  - A nonexistent db path returns 0.
  - An empty `run_id` returns 0.
  - None of these raises.
- **Helpers.** `artifact_reward(0, 0) is None`, and `artifact_reward(3, 1)` is
  0.75.
- **Post-run, default on.** Set `MO_GRADE_RUN_REWARD=0`,
  `MO_LEARNING_WRITEBACK=0` and `MO_LANE_ROUTER=0`. One trace at `T0` with a
  `fail` at `E0+1`. After
  `ex._post_run_learning(db, str(tmp_path), "r1", "code_fix", fail_count=0)`
  (with `from mini_ork.cli import execute as ex`), the row has
  `reward_source='artifact@v1'` and `reward_value` 0.0.
- **Post-run, opted out.** The same setup with `MO_ARTIFACT_REWARD=0` leaves
  `reward_source` at `'verifier@v1'`.
- **run_verified wins.** The same setup with `MO_IMPL_REWARD=run_verified` and
  `MO_ROUTER_COST_LAMBDA=0` ends with `reward_source='run_verified@v1'` and
  `reward_value` 1.0.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_artifact_node_reward.py tests/unit/test_impl_run_verified_reward.py tests/unit/test_stage_reward_coverage_py.py tests/unit/test_writeback_validity_masking_py.py tests/unit/test_reward_audit_py.py`
- `pytest -q -p no:asyncio tests/unit/test_hygiene_c.py tests/unit/test_eco6_k10_otel_live.py tests/unit/test_cli_apply_py.py tests/unit/test_execute_review_park.py`
- `pytest -q -p no:asyncio tests/unit/test_mini_ork_execute_py.py tests/unit/test_board_cmd.py tests/unit/test_features_store.py tests/unit/test_verify_partial_trace.py tests/unit/test_hygiene_b.py tests/unit/test_throttle_guard_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_artifact_node_reward.py --junitxml="${MINI_ORK_RUN_DIR}/node-reward-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/node-reward-junit.xml`

## Success criteria

- All three verification commands pass, with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/learning/writeback.py mini_ork/cli/execute.py`
  is clean.
- After merge, for runs on the new engine, this query returns `artifact@v1` rows
  whose mean `reward_value` is below 1.0:

  ```
  SELECT reward_source, COUNT(*), AVG(reward_value)
    FROM execution_traces
   WHERE json_extract(verifier_output,'$.node_type')='implementer'
     AND created_at >= <merge time>
   GROUP BY 1
  ```

  This is a post-merge observation, not a run gate.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes. Do not change any other node
type's reward, the trace writer, `reward_from_status`, or
`write_grpo_advantages`. Never alter the import graph or builtins to satisfy a
test.
