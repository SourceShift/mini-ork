# RSI Tier-0 #9: supervisor rule R8 dispatches one ready epic per sweep (opt-in)

## Goal

Nothing dispatches epics on a schedule:

- The automations tick fires nothing, and there is no scheduler daemon.
- So every epic that becomes ready waits for a human to run `mini-ork scheduler`.
  That covers the 24 `not started` fix epics, split children (SDE-6b) and
  unblocked dependents.
- The self-driving loop opens fix epics (SDE-3a) and splits runs into child
  epics (SDE-6b), but the loop never closes without a dispatcher.

The supervisor already runs on a cadence (`mini-ork supervise --once` from
launchd every 300 s, or `--interval` in the service tier) under a lease. Add a
rule, R8, that starts **one** ready epic through the existing scheduler. It is
**off by default**, because a dispatch spends money.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/recovery/supervisor.py`

- **New `sweep` keyword arguments**, with defaults that keep today's behaviour:
  - `dispatch_epics: bool = False`
  - `dispatch_max_in_progress: int = 1`
  - `dispatch_only: list[str] | None = None`
  - `dispatch_fn=None`, a test seam with the signature
    `(engine_root: str, home: str, db: str, epic_id: str) -> dict`.
- **New rule** `_rule_dispatch_epic(home, db, engine_root, dry_run, *, max_in_progress, only, dispatch_fn) -> list[dict]`.
  `sweep` calls it after `_rule_backlink`, only when `dispatch_epics` is true,
  inside the same `try/except` shape as the other DB-wide rules (an error
  becomes an `{"action": "error", "reason": "dispatch rule: …"}` dict).
  - **Concurrency cap.** Count epics with `status='in progress' AND archived_at IS NULL`.
    When that count is `>= max_in_progress`, return `[]`.
  - **Pick.** `ready = mini_ork.scheduler.pick_ready(db, only=set(only) if only else None)`.
    Empty → `[]`.
  - **Dry run.** Return
    `[{"epic_id": ready[0], "action": "dispatch_epic", "reason": "ready; dry-run", "dry_run": True}]`
    and spawn nothing.
  - **Otherwise.** Call
    `(dispatch_fn or _default_dispatch_epic)(engine_root, home, db, ready[0])`
    and return `[{"epic_id": ready[0], "action": "dispatch_epic", "reason": "ready", **result}]`.
- **New `_default_dispatch_epic(engine_root, home, db, epic_id) -> dict`.**
  - Spawns, detached (`start_new_session=True`, stdin `DEVNULL`):
    `[sys.executable, <engine_root>/bin/mini-ork, "scheduler", "--once", "--epic", epic_id]`.
  - `cwd=engine_root`.
  - The env is `os.environ` plus `MINI_ORK_ROOT=engine_root`,
    `MINI_ORK_HOME=home`, `MINI_ORK_DB=db`, with every run-scoped key popped
    (the same list `_default_spawn` pops).
  - stdout and stderr go to `<home>/state/supervisor-dispatch-<epic_id>.log`.
    Create `<home>/state` if missing.
  - Returns `{"pid": <pid>, "log": <path>}`. On `OSError` it returns
    `{"error": str(exc)}`, never raises.
- **Budget.** The scheduler already enforces the per-epic cap (SDE-4c) and
  `MO_DAILY_BUDGET_USD`. R8 adds no budget logic of its own.

### 2. `mini_ork/cli/supervise.py`

- **The new config block** is read in `_load_config`:

  ```yaml
  dispatch_epics:
    enabled: false        # R8 off by default
    max_in_progress: 1
    only: []              # optional allow-list of epic ids
  ```

  - Absent, or `enabled: false` → R8 is off.
  - `enabled` must be a bool, and `max_in_progress` an integer >= 1.
  - `only` must be a list of strings.
  - A wrong type raises `ConfigError` naming the key (exit 2, fail closed).
- **Passing it on.** `run_sweep` passes `dispatch_epics`,
  `dispatch_max_in_progress` and `dispatch_only` to `supervisor.sweep`.
- **The defaults dict** gains the block (`enabled: false`).

### 3. `config/self-driving.example.yaml`

Add the block above, commented as:

- off by default because R8 starts paid runs;
- the simple tier (launchd/cron `--once`) and the service tier (`--interval`)
  read the same key.

### 4. `docs/operator/self-driving.md`

Add an "R8: dispatch ready epics (opt-in)" section, in 5–10 lines:

- what it does;
- the in-progress cap;
- the allow-list;
- that budgets are enforced by the scheduler;
- how to turn it on.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/supervisor.py`
- `mini_ork/cli/supervise.py`
- `config/self-driving.example.yaml`
- `docs/operator/self-driving.md`
- `tests/unit/test_supervisor_dispatch.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_supervisor_dispatch.py`: hermetic temp home + sqlite, `dispatch_fn` spy, never a real spawn)

Build an `epics` table the way `tests/unit/test_mini_ork_epics_py.py` or
`tests/unit/test_scheduler_py.py` does, so `scheduler.pick_ready` works on it.

- **Off by default.** `sweep(..., dispatch_epics=False)` with a ready epic
  never calls the spy, and no `dispatch_epic` action is returned.
- **One dispatch.** With `dispatch_epics=True` and one ready epic `e1`, the spy
  is called once with `epic_id="e1"`, and the action list contains
  `{"action": "dispatch_epic", "epic_id": "e1"}`.
- **The cap holds.** An epic already `in progress` with the default cap 1 →
  the spy is not called.
- **The allow-list.** With `dispatch_only=["e2"]` and ready epics `e1` and `e2`
  → the spy is called with `e2`.
- **Dry run.** `dry_run=True` returns the action with `"dry_run": True` and
  never calls the spy.
- **Errors are contained.** A spy that raises yields an `error` action, and
  `sweep` returns normally.
- **Config parsing**, via `supervise._load_config` on temp YAML files:
  - the block parsed;
  - the block absent → off;
  - `enabled: "yes"` → `ConfigError`;
  - `max_in_progress: 0` → `ConfigError`;
  - `only: "e1"` → `ConfigError`.

## Verification commands

- `pytest -q tests/unit/test_supervisor_dispatch.py tests/unit/test_supervisor.py tests/unit/test_supervisor_patches.py tests/unit/test_supervisor_superseded.py`
- `pytest -q tests/unit/test_scheduler_py.py tests/unit/test_scheduler_epic_budget.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_supervisor_dispatch.py --junitxml="${MINI_ORK_RUN_DIR}/epic-dispatch-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/epic-dispatch-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/recovery/supervisor.py mini_ork/cli/supervise.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
