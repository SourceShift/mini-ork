# RSI Tier-1 #12b: let the circuit breaker act on the diversity / pass@k floor, once per breach

## Goal

`rsi-t1-collapse-floor` (prerequisite; it must be merged first) built
`collapse_detector.floor` and `collapse_floor.evaluate`. They give a per-class
`halt` / `watch` / `none` from terminal-run outcomes and implementer-lane
diversity. Backtested over 1,131 evaluations they recommend `halt` 2 times;
today they recommend `none` for every class. Nothing acts on the result: the
breaker (`mini_ork/recovery/circuit_breaker.py`) trips only on its three-signal
vote and on the score-vs-anchor `collapse_halt`.

**Why the trip is edge-triggered.** Wiring the floor in as a plain independent
trip would deadlock a scope. Every run of a breached scope would trip. Each
tripped run ends `failed`, which keeps pass@k down, so the breach never clears.
The floor therefore **trips once per breach episode** and then latches. The
latch is the existing `circuit_breaker_state.last_reason` column, holding the
token `collapse_floor`. While the breach persists, later runs proceed with the
breach reported. The first evaluation without a breach clears the latch.

**Prerequisite: an operator token.** `mini_ork/recovery/circuit_breaker.py` is a
frozen path, listed in both `config/kernel-frozen.yaml` and
`mini_ork/vcs/repo_integrity_guard.FROZEN_PATHS_DEFAULT`. The publisher refuses
the patch (`publish-frozen-path`) unless the operator launches this run with
`MO_ALLOW_FROZEN_EDIT=1`.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/recovery/circuit_breaker.py`

**Env docs.** Add two lines to the module docstring's env-knob list:

- `MO_CB_COLLAPSE_FLOOR → diversity / pass@k floor (default ON; "0" disables; also off when MO_CB_COLLAPSE=0)`
- `MO_CB_FLOOR_WINDOW → floor window in terminal runs (default 20; read by collapse_floor.resolve_window)`

**New helper `_eval_floor_signal(con, tr, floor_runs, enabled: bool) -> tuple[bool, dict]`.**

- **Disabled.** When `enabled` is false, return
  `(False, {"recommendation": "none", "breach": False, "reason": "collapse floor disabled (MO_CB_COLLAPSE_FLOOR=0 or MO_CB_COLLAPSE=0)"})`.
- **Import.** Lazily import `collapse_detector` and `collapse_floor` from
  `mini_ork.learning`. On any exception, call
  `_log_collapse_once(f"floor import failed: {type(exc).__name__}: {exc}")` and
  return `(False, {"recommendation": "none", "breach": False, "reason": f"floor import failed: {exc}"})`.
- **Report.**
  - When `floor_runs is not None`:
    `report = collapse_detector.floor(list(floor_runs), window=collapse_floor.resolve_window())`.
  - Otherwise: `report = collapse_floor.evaluate(con, tr["task_class"])`.
- **Errors.** On any exception, call
  `_log_collapse_once(f"floor raised {type(exc).__name__}: {exc}")` and return
  `(False, {"recommendation": "none", "breach": False, "reason": f"floor raised {type(exc).__name__}: {exc}"})`.
- **Returns** `(report.get("recommendation") == "halt", report)`.

**Changes to `check_liveness_breaker`:**

- **Signature.** Add the keyword parameter
  `floor_runs: Sequence[dict] | None = None` after `collapse_history`. It is a
  TEST SEAM: when not `None`, the floor DB read is skipped.
- **Enablement.** Directly after `collapse_enabled` is computed:
  `floor_enabled = collapse_enabled and os.environ.get("MO_CB_COLLAPSE_FLOOR", "1") != "0"`.
- **Evaluation.** Directly after the existing `_eval_collapse_signal(...)` call:
  - `floor_fired, floor_report = _eval_floor_signal(con, tr, floor_runs, floor_enabled)`;
  - `floor_latched = st is not None and "collapse_floor" in str(st["last_reason"] or "").split(",")`;
  - `floor_trip = floor_fired and not floor_latched`.
- **Trip.** Directly after the existing `independent_collapse_trip` block, add:

  ```
  independent_floor_trip = False
  if floor_trip and not trip:
      trip = True
      independent_floor_trip = True
  ```

- **Verdict branch.** In the `if verdict == "LIVENESS_TRIP":` branch, keep the
  `if independent_collapse_trip:` arm byte-for-byte. Add an
  `elif independent_floor_trip:` arm before the existing `else:` arm:
  - `top_rationale = f"collapse_floor fired under policy={policy} ({fired_count}/{signal_count} stagnation signals fired) — halting"`;
  - `last_reason_tokens = ["collapse_floor"]`.
- **Latch token.** After the whole verdict/rationale `if/elif/else`, and before
  the `circuit_breaker_state` upsert, add:

  ```
  if floor_fired and "collapse_floor" not in last_reason_tokens:
      last_reason_tokens.append("collapse_floor")
  ```

  This applies to every verdict, PROCEED and PROBE included. It is what keeps
  the latch set while the breach persists.
- **Output.** Add five keys to `out["collapse"]`:
  `"floor_enabled": floor_enabled`, `"floor_fired": floor_fired`,
  `"floor_latched": floor_latched`,
  `"floor_independent_trip": independent_floor_trip` and
  `"floor": floor_report`. All existing keys keep their meaning:
  `out["collapse"]["fired"]` is still the divergence signal only.
- **Unchanged:** the vote, `signals`, `fired_count`, `signal_count`, the
  unknown-run shape, the `MO_CB_DISABLE` path and every existing rationale
  string.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/circuit_breaker.py`
- `tests/unit/test_circuit_breaker_floor.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_circuit_breaker_floor.py`: hermetic temp sqlite, no network)

**Fixtures and helpers.** Copy the `db` fixture, `_seed_task_runs` and
`_reset_cb_state` from `tests/unit/test_circuit_breaker_collapse.py`. Seed one
run `{"id": "run-live", "task_class": "code_fix", "recipe": "code-fix", "artifact_hash": "1111"*4, "cost_usd": 0.05, "created_at": now - 1}`
so the vote is silent (`fired_count == 0`).

- `BREACH` rows:
  - prior 20: the first 10 passed, lanes alternate `["a"]`/`["b"]`;
  - recent 20: the first 2 passed, every lane `["a"]`;
  - each row `{"run_id": f"r{i}", "passed": ..., "task_key": f"k{i}", "lanes": ...}`.
- `HEALTHY` rows: 40 passing rows with lanes `["a", "b"]`.
- Call `cb.check_liveness_breaker("run-live", db=db, floor_runs=...)` and read
  `circuit_breaker_state.last_reason` for scope `code_fix::code-fix`.

**Trip and latch:**

- **First breach trips.** Monkeypatch-delete `MO_CB_COLLAPSE` and
  `MO_CB_COLLAPSE_FLOOR`, reset the state, then call with `BREACH`:
  - `rc == 1` and `verdict == "LIVENESS_TRIP"`;
  - `out["collapse"]["floor_fired"] is True`,
    `out["collapse"]["floor_independent_trip"] is True`,
    `out["collapse"]["floor_latched"] is False`;
  - `out["collapse"]["fired"] is False`;
  - `out["rationale"].startswith("collapse_floor fired under policy=majority")`;
  - `last_reason == "collapse_floor"`;
  - `out["collapse"]["floor"]["recommendation"] == "halt"`.
- **The latch holds.** An immediate second call with `BREACH` gives:
  - `rc == 0` and `verdict == "PROCEED"`;
  - `floor_fired is True`, `floor_latched is True`,
    `floor_independent_trip is False`;
  - `last_reason` is still `"collapse_floor"`.
- **The latch clears.** A third call with `HEALTHY` gives `rc == 0`,
  `floor_fired is False` and `last_reason is None`. A fourth call with `BREACH`
  trips again (`rc == 1`).

**Opt-outs:**

- `MO_CB_COLLAPSE_FLOOR=0` with `BREACH` gives `rc == 0`,
  `floor_enabled is False` and `floor_fired is False`.
- `MO_CB_COLLAPSE=0` with `BREACH` gives `rc == 0` and `floor_enabled is False`.

**Production read path (no seam):**

- Insert 40 terminal `code_fix` / `code-fix` task runs into the DB:
  - status `published`/`failed` following the `BREACH` pattern;
  - a distinct `kickoff_path` and a distinct `artifact_hash` (`f"h{i:04d}"`)
    each;
  - `created_at` from `now - 1000 + i`.
- Use explicit INSERTs with a status column, because `_seed_task_runs`
  hard-codes `classified`.
- Add implementer traces
  `INSERT INTO execution_traces (trace_id, run_id, task_class, agent_version_id, status, verifier_output, created_at) VALUES (f"tr-implementer-implementer-{i:08x}", <run id>, 'code_fix', <lane>, 'success', '{"node_type": "implementer"}', <iso ts>)`.
- `check_liveness_breaker("run-live", db=db)` (status `classified`, so excluded
  from the floor) gives `rc == 1` and `floor_independent_trip is True`.

**Fail-open and interaction:**

- **Fail-open.** Monkeypatch `mini_ork.learning.collapse_detector.floor` to
  raise `RuntimeError`. A `BREACH` call then gives `rc == 0`, `floor_fired is False`
  and `"floor raised RuntimeError"` in `out["collapse"]["floor"]["reason"]`.
- **Both signals.** Seed a halting `collapse_history`: the
  `test_seeded_rows_trip` rows from `test_circuit_breaker_collapse.py`, with
  `floor_runs=BREACH`. The result is:
  - `rc == 1`;
  - `out["rationale"]` starts with `"collapse_halt fired under policy="`;
  - `last_reason == "collapse_halt,collapse_floor"`.

## Verification commands

- `pytest -q tests/unit/test_circuit_breaker_floor.py tests/unit/test_circuit_breaker_collapse.py tests/unit/test_circuit_breaker_py.py`
- `pytest -q tests/unit/test_native_oracle_gates_py.py tests/unit/test_frozen_kernel.py tests/unit/test_collapse_floor.py tests/unit/test_collapse_detector_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_circuit_breaker_floor.py --junitxml="${MINI_ORK_RUN_DIR}/collapse-floor-breaker-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/collapse-floor-breaker-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/recovery/circuit_breaker.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
