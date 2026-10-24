# RSI Tier-0 #2: give the diagnosis ledger rows from normal repairs, and label them

## Goal

The calibrated escalation bar (SDE-3b2) learns the model diagnoser's error rate
from **labelled `model`-source rows** in `diagnosis_ledger`
(`mini_ork/stores/diagnosis_ledger.labelled`). Until it has labels it stays at
the 0.85 bootstrap bar. The primary home has 36 diagnoses (9 from the model) and
**0 ledger rows**. The ledger is starved by construction:

- **Only one writer.** `mini_ork/recovery/escalation._record_ledger` is the
  only writer, and it runs only inside `escalation.escalate`. That function is
  reached only when `auto_repair.decide` returns `escalate`, i.e. only for an
  engine-layer diagnosis that routes to a fix epic. Every other diagnosis the
  repair loop acts on (`lane`, `infra`, `reverify`, `prove`, `revise`) is
  persisted to `diagnosis.json` but never reaches the ledger.
- **Only one labeller.** The supervisor's R2 (`_rule_label` in
  `mini_ork/recovery/supervisor.py`) labels only runs it revived itself (the
  `revived_at` key in `repair.json`).
- **The loop is circular.** The bar is what admits an escalation, and only
  escalations write rows.

Record every diagnosis the repair loop acts on with a spawnable action, and let
R2 label it once the repaired run finishes.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/recovery/auto_repair.py`

- **New helper** `_record_repair_ledger(home: Path, run_id: str, diagnosis: dict, action: str) -> bool`.
  - Calls `diagnosis_ledger.record(Path(home) / "state.db", run_id, diagnosis, action=action)`.
  - Import `mini_ork.stores.diagnosis_ledger` locally inside the helper.
  - Returns `True` when `record` returned a truthy id.
  - Returns `False` on any exception. It never raises: the ledger is a side
    channel and must never change a repair.
- **Call site, in `apply`.** It goes in the existing block where `attempts` is
  appended and `repair["state"]` is set, before the single
  `_write_repair(run_dir, repair)` call. When all three of these hold:
  - `action in _SPAWNABLE`;
  - the decision's `diagnosis` (`dx`) is a dict;
  - `str(dx.get("source") or "")` is non-empty;

  then call `_record_repair_ledger(home, run_id, dx, action)`. When it returns
  `True`:
  - set `repair["ledger_recorded_at"] = int(time.time())`;
  - `repair.pop("labelled_at", None)`, so R2 labels the outcome of the newest
    repair attempt.

  The recording happens whether or not `spawn` is true: the attempt is
  persisted either way.
- **Nothing else changes in `auto_repair.py`:** not `decide`, not the action
  set, not `escalate` handling, not the human hand-off.

### 2. `mini_ork/recovery/supervisor.py`

- **The R2 gate in `sweep`.** It currently reads:

  ```
  if repair.get("revived_at") and not repair.get("labelled_at"):
  ```

  Replace it with:

  ```
  if (repair.get("revived_at") or repair.get("ledger_recorded_at")) and not repair.get("labelled_at"):
  ```

  The rest of that block is unchanged.
- **`_rule_label`.** When `repair.get("revived_at")` is truthy, its behaviour is
  **byte-for-byte unchanged**. Otherwise (the new repair-ledger path):
  - **When to act.** Look up the run with `_task_run(db, run_id)`. Return
    `None` when:
    - the run is missing; or
    - its status is not in `_TERMINAL_RUN_STATUS`; or
    - `_as_epoch(updated_at)` is not greater than
      `_as_epoch(repair["ledger_recorded_at"])`.
  - **The verdict.** `correct = status == "published"`.
  - **The reason.** `reason = f"repaired run {status} — diagnosis was {'right' if correct else 'wrong'}"`.
  - **Writes, skipped entirely when `dry_run`:**
    - for every id from a new helper
      `_unlabelled_ledger_ids(db, run_id) -> list[int]`, call
      `diagnosis_ledger.set_label(db, ledger_id, correct, reason)`. The helper
      runs `SELECT id FROM diagnosis_ledger WHERE run_id=? AND label IS NULL`
      through `_query`, and `[]` on any error;
    - write `repair.json` with `labelled_at = int(now)` and
      `label_correct = bool(correct)`, and do **not** change `state`.
  - **Return value**, in dry runs too:
    `{"run_id": run_id, "action": "label", "reason": reason, "correct": correct}`.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/auto_repair.py`
- `mini_ork/recovery/supervisor.py`
- `tests/unit/test_diagnosis_ledger_repair_path.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_diagnosis_ledger_repair_path.py`: hermetic temp home + sqlite, no network, no real spawn)

Build the temp home the way `tests/unit/test_auto_repair.py` does: copy its
`home` fixture and its `_seed(home, status=...)` helper, which creates
`runs/<run_id>/` and a `state.db` whose `task_runs` row has the columns
`_task_run` reads (`id`, `status`, `updated_at`). `failed` and `published` are
in `mini_ork.run_status.TERMINAL`; `running` is not. Call
`auto_repair.apply(home, run_id, decision, spawn=False)` with a hand-built
decision dict:

```
{"action": "lane", "from_node": "implementer", "lanes": {}, "reason": "r",
 "signature": "sig-1",
 "diagnosis": {"source": "model", "layer": "provider", "routes_to": "lane",
               "confidence": 0.7, "rule_id": "model", "stage": "execute",
               "node": "implementer", "summary": "s"}}
```

Tests:

- **A row is recorded.** After `apply`, `diagnosis_ledger` has exactly one row
  for the run, with `source='model'` and `action='lane'`. `repair.json` has an
  integer `ledger_recorded_at` and no `labelled_at`.
- **No source, no row.** A decision whose diagnosis has `source: ""` writes no
  ledger row and no `ledger_recorded_at`.
- **Escalate is untouched.** A decision with action `escalate` writes no ledger
  row through this path.
- **The ledger can never fail a repair.**
  - Monkeypatch `mini_ork.stores.diagnosis_ledger.record` to raise. `apply`
    still returns normally and `repair.json` still records the attempt.
  - `_record_repair_ledger(...)` returns `False` in that case.
- **A published run is labelled right.** Set the run `published` with an
  `updated_at` later than `ledger_recorded_at`, then call
  `supervisor.sweep(str(home), db, engine_root=<tmp dir>, dry_run=False)`:
  - the ledger row's `label` is 1;
  - `repair.json` has `labelled_at` and `label_correct: true`;
  - `state` is unchanged;
  - the returned actions contain `{"action": "label", "correct": True}` for the
    run.
- **A failed run is labelled wrong.** The same with status `failed` gives
  `label` 0 and `correct` False.
- **No label while running.** A run still `running` gets no label and no action
  for this run.
- **A dry run writes nothing.** `dry_run=True` returns the label action but
  leaves the ledger row's `label` NULL and `repair.json` without
  `labelled_at`.
- **The revived path is unchanged.** A `repair.json` with `revived_at` (and no
  `ledger_recorded_at`) still follows the existing revived path.

## Verification commands

- `pytest -q tests/unit/test_diagnosis_ledger_repair_path.py tests/unit/test_auto_repair.py tests/unit/test_supervisor.py tests/unit/test_supervisor_patches.py`
- `pytest -q tests/unit/test_supervisor_superseded.py tests/unit/test_diagnosis_ledger.py tests/unit/test_escalation.py tests/unit/test_triage_auto_repair_gate.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_diagnosis_ledger_repair_path.py --junitxml="${MINI_ORK_RUN_DIR}/diag-ledger-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/diag-ledger-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/recovery/auto_repair.py mini_ork/recovery/supervisor.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
