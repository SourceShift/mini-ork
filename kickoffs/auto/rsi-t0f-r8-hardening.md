# RSI Tier-0 follow-up: R8 refuses to dispatch into the engine checkout, and reclaims stale epics first

## Goal

R8 (`mini_ork/recovery/supervisor.py:_rule_dispatch_epic`, e15d8f42) spawns
`mini-ork scheduler --once --epic <id>` with `cwd=engine_root`. Two problems
follow from that.

**1. Runs edit the engine checkout.** The scheduler runs each epic as
`<root>/bin/mini-ork run <recipe> <kickoff>` with no `cwd`
(`mini_ork/scheduler.py:dispatch_epic`), so the run inherits the engine
checkout as its working tree.
- Every dispatched epic would therefore edit the operator's primary checkout.
- A framework-edit run also stashes and rolls back the tree it runs in.
- Every manual run so far has used a dedicated clean worktree for exactly this
  reason.

**2. A stuck epic wedges R8.** R8 counts `in progress` epics against its cap.
The scheduler's own stale-epic recovery (`mini_ork/scheduler.py:reclaim_stale`)
only runs when a scheduler starts, so a crashed scheduler's epic holds the cap
forever, and no scheduler is ever started again to reclaim it.

Fix both. R8 stays off by default.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/cli/supervise.py`, `_parse_dispatch_block`

- The block gains an optional key, `target_root`, defaulting to `""`.
  - When present and not `None`, it must be a `str`; otherwise raise
    `ConfigError("supervise config <path>: dispatch_epics.target_root must be a string")`.
  - Expand `~` with `os.path.expanduser` and keep the result.
- `DEFAULTS["dispatch_epics"]` gains `"target_root": ""`.
- `run_sweep` passes `dispatch_target_root=str(dispatch["target_root"])` to
  `supervisor.sweep`.

### 2. `mini_ork/recovery/supervisor.py`

- `sweep` gains the keyword `dispatch_target_root: str = ""` and passes it to
  `_rule_dispatch_epic` as `target_root=`.
- `_rule_dispatch_epic(home, db, engine_root, dry_run, *, max_in_progress, only, dispatch_fn, target_root="")`
  runs these steps, in order:
  1. **Refuse when the target is unsafe.** When any of the following holds,
     return exactly
     `[{"epic_id": "", "action": "dispatch_refused", "reason": <reason>}]`
     and spawn nothing:

     | Condition | `<reason>` |
     |---|---|
     | `target_root` is empty | `"dispatch_epics.target_root is required: a dispatched run edits its working tree, which must never be the engine checkout"` |
     | `os.path.isdir(target_root)` is false | `f"dispatch_epics.target_root does not exist: {target_root}"` |
     | `os.path.realpath(target_root) == os.path.realpath(engine_root)` | `"dispatch_epics.target_root is the engine checkout"` |

  2. **Reclaim stale epics.** Unless `dry_run`, call
     `mini_ork.scheduler.reclaim_stale(db)`. When it returns a non-empty list,
     append `{"epic_id": "", "action": "reclaim_stale", "epic_ids": <list>}`
     to the actions this rule returns. A raised exception here becomes an
     action `{"epic_id": "", "action": "error", "reason": f"reclaim: {type(exc).__name__}: {exc}"}`,
     and the rule continues.
  3. **Cap, pick, dry-run** exactly as today.
  4. **Dispatch** by calling
     `(dispatch_fn or _default_dispatch_epic)(engine_root, home, db, epic_id, target_root)`,
     which now takes 5 positional arguments.
- **`_default_dispatch_epic(engine_root, home, db, epic_id, target_root)`:**
  - `cwd=target_root`.
  - The env additionally sets `MO_TARGET_CWD=target_root` and
    `MO_ALLOW_FRAMEWORK_CWD=1`, and sets `MO_PLACEMENT` to `"local"` unless
    the env already has a non-empty `MO_PLACEMENT`.
  - Everything else is unchanged: the argv, the log path, the popped keys, the
    detach, and the `{"pid", "log"}` / `{"error"}` return.

### 3. `config/self-driving.example.yaml` and `docs/operator/self-driving.md`

Document `target_root`:
- it is required to enable R8;
- what it should be: a dedicated, clean checkout or worktree, for example one
  made with `make worktree SLUG=dispatch`, never the engine checkout;
- dispatched runs commit there, and landing them on `main` is the operator's
  `make worktree-merge`;
- R8 reclaims stale `in progress` epics with the scheduler's own rules before
  it counts them.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/supervisor.py`
- `mini_ork/cli/supervise.py`
- `config/self-driving.example.yaml`
- `docs/operator/self-driving.md`
- `tests/unit/test_supervisor_dispatch.py`

Update the existing tests in `tests/unit/test_supervisor_dispatch.py` for the
new required `target_root` and the 5-argument `dispatch_fn`:
- pass `dispatch_target_root=<a tmp dir that is not the engine_root>`;
- give spies 5 parameters.

Do NOT modify any other file.

## Tests (add to `tests/unit/test_supervisor_dispatch.py`, hermetic, spy `dispatch_fn`, never a real spawn)

- **Refusals.** With `dispatch_epics=True`, a ready epic, and
  `dispatch_target_root` set to each of `""`, a missing path, and the same
  directory as `engine_root`:
  - the action list contains one `dispatch_refused` action with the exact
    reason from the table;
  - the spy is never called.
- **A valid target.** With a valid `target_root` (a tmp dir distinct from
  `engine_root`), the spy is called once with
  `(engine_root, home, db, "e1", target_root)`.
- **Reclaim runs first.** Monkeypatch `mini_ork.scheduler.reclaim_stale` to
  return `["old"]`:
  - the actions include `{"epic_id": "", "action": "reclaim_stale", "epic_ids": ["old"]}`;
  - with `dry_run=True` the patched function is not called.
- **A failing reclaim is contained.** Monkeypatch `reclaim_stale` to raise:
  there is an `error` action whose reason starts with `reclaim:`, and the spy is
  still called for the ready epic.
- **The default spawn uses the target.** Call `_default_dispatch_epic` with
  `subprocess.Popen` monkeypatched to a recorder:
  - `cwd == target_root`;
  - `env["MO_TARGET_CWD"] == target_root`;
  - `env["MO_ALLOW_FRAMEWORK_CWD"] == "1"`;
  - `env["MO_PLACEMENT"] == "local"` when unset, and an existing
    `MO_PLACEMENT=remote` is kept.
- **Config.**
  - `target_root: 5` → `ConfigError`.
  - An absent key → `""`.
  - `~/x` → expanded.

## Verification commands

- `pytest -q tests/unit/test_supervisor_dispatch.py tests/unit/test_supervisor.py tests/unit/test_supervisor_superseded.py tests/unit/test_supervisor_patches.py`
- `pytest -q tests/unit/test_scheduler_py.py tests/unit/test_diagnosis_ledger_repair_path.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_supervisor_dispatch.py --junitxml="${MINI_ORK_RUN_DIR}/r8-hardening-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/r8-hardening-junit.xml`

## Success criteria

- Both verification commands pass.
- `ruff check --select F,E9 mini_ork/recovery/supervisor.py mini_ork/cli/supervise.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
