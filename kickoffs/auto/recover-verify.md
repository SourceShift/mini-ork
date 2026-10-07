# `mini-ork recover`: reuse finished nodes for real, and a verify-only retry

## Why

A run that failed late — e.g. its live smoke check could not reach a surface — should be retryable
without paying again for the nodes that already succeeded. Today `mini-ork recover` cannot do that.
Measured with `recover --status` on the researcher run `run-le-1791359434-64879-1` (recipe
`acq-wave5-rsi`, a project overlay recipe; 5 LLM nodes have `success` checkpoints in
`node_checkpoints`; only `live_smoke` failed):

1. **Overlay recipes are not found.** `planner.py:191-193` builds
   `<MINI_ORK_ROOT>/recipes/<recipe>/workflow.yaml` only → `workflow.yaml not found` for any
   recipe that lives in `<MINI_ORK_HOME>/recipes/`. `_task_class_for_recipe` (`planner.py:228`)
   has the same blind spot.
2. **Everything reruns anyway.** The planner node is skipped at run time
   (`[skip] planner node handled by the Python plan runtime`), so it never writes a checkpoint.
   `_reusable_set` (`plan.py:154-205`) marks it `no_row`, it becomes the closure root, and the
   closure swallows all 5 reusable nodes (they print under both "reuse" and "rerun").
3. **The code is gone.** A rollback node reverted the implementer's edit after the failure (saved
   to a patch in the run dir). Reusing the implementer checkpoint on a clean tree would verify the
   old code.

## Files in scope

- `mini_ork/recovery/planner.py`, `mini_ork/recovery/plan.py`
- `mini_ork/recovery/restore.py` (new)
- `tests/unit/test_recover_verify.py` (new)

Do NOT edit `mini_ork/cli/execute.py` or `mini_ork/cli/execute_handlers.py` (another worktree owns
them). Do NOT create `mini_ork/recovery/retry_hint.py` (a parallel run owns it).

## Changes

1. **Recipe resolution.** Resolve the workflow and task_class with
   `mini_ork.recipes_catalog.find_recipe(recipe, home)` (project recipe shadows the engine's; follow
   symlinks), falling back to `<MINI_ORK_ROOT>/recipes/<recipe>/`. `MINI_ORK_WORKFLOW` and
   `--workflow` still win.
2. **The skipped planner is reusable.** In `_reusable_set`: a node of type `planner` with no
   checkpoint row counts as reusable (reason `plan_json`) when `<run_dir>/plan.json` exists and
   parses. Any other `no_row` node keeps today's behaviour. The status output must no longer list a
   reusable node under "rerun".
3. **`--strategy verify`.** Add `verify` to `RECOVERY_STRATEGIES`. Entry = the first node in topo
   order whose type is `verifier` (or `--from-node`); closure = entry + its dependents (same
   closure walk as today); every node upstream of the entry that has a reusable checkpoint (or is the
   planner per 2) is reused. If an upstream LLM node is NOT reusable, refuse `verify` with
   "<node> has no reusable checkpoint — use --strategy resume".
4. **Restore the work before reusing it** (new `mini_ork/recovery/restore.py`,
   `restore_carry_patch(run_dir, target) -> (status, message)`), called by `cli_main` before
   dispatch for `verify`, `resume` and `retry` whenever the reuse set contains a node of type
   `implementer` (or any code-changing type) and the run was rolled back (`rolled-back.json`
   exists, or the carry patch exists):
   - Carry patch = `--carry-patch <run-dir-relative path>` if given, else the workflow's optional
     top-level `recovery: {carry_patch: <name>}`, else `salvage.patch`.
   - Target tree = `implementer-summary.json` `worktree_path`, else the run profile's target cwd;
     none → refuse.
   - Already applied (`git apply --check -R`) → `already_applied`, continue. Applies cleanly
     (`git apply --3way --check`) → apply, `applied`. Otherwise → `conflict`, refuse with the
     patch path and the git stderr; do not dispatch.
   - `--status` prints the restore plan (patch path, target, would-apply/already-applied/conflict)
     without touching the tree.
5. **Needs-change gate.** Before dispatch, read the run's retry hint: call
   `mini_ork.recovery.retry_hint.load_or_compute(home, run_id)` when that module imports, else read
   `<run_dir>/retry-hint.json` if present. Contract (written by the parallel run):
   `{"retryable": bool, "strategy": "verify|resume|retry|none", "from_node": str|null,
   "needs_change": null | {"kind": str, "summary": str, "detail": str, "evidence": str},
   "command": str}`.
   - `retryable: false` → refuse, print `needs_change.summary`/`detail`; `--force` overrides.
   - `needs_change` set → refuse unless `--ack-change` is passed; print
     "Needs a change before retrying (<kind>): <summary>\n<detail>\nFix it, then rerun with --ack-change."
   - `--status` prints the hint block at the top. No hint → today's behaviour.
6. Update `_USAGE` for `verify`, `--carry-patch`, `--ack-change`, `--force`.

## Tests (`tests/unit/test_recover_verify.py`; tmp home + tmp git repo, no LLM, no real home)

- Overlay recipe under `<home>/recipes/<r>/workflow.yaml` (none in the engine) → `--status` finds it.
- Planner without a checkpoint + `plan.json` present + 5 success checkpoints upstream of a failed
  verifier → `--status` shows reuse = planner + those 5, entry = the first verifier, and no reused
  node in the rerun list.
- `--strategy verify` with an upstream LLM node lacking a checkpoint → refused with the message in 3.
- Restore: rolled-back run + `salvage.patch` → applied to the target repo; second call →
  `already_applied`; a conflicting tree → `conflict` and the executor is never called (inject
  `execute_fn`). Workflow `recovery.carry_patch` and `--carry-patch` override the default name.
- Hint gate: `retry-hint.json` with `needs_change` → refused without `--ack-change`, dispatched
  with it; `retryable: false` → refused, `--force` dispatches.
- Existing recovery tests still pass.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_recover_verify.py` plus every existing test
  file matching `tests/unit/test_recover*.py tests/unit/test_recovery*.py` passes — paste the
  summary lines.
- `ruff check` on the touched files is clean.
- Read-only proof on the real run (status only — never dispatch against a real home):
  `MINI_ORK_HOME=/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork bin/mini-ork recover run-le-1791359434-64879-1 --status --strategy verify --carry-patch cycle-delta.patch`
  → paste the output: reuse = planner, 3 lenses, synthesizer, implementer; entry `cycle_gate`;
  rerun = cycle_gate, live_smoke, scope_guard, opus_judge, publisher; the restore line.
- Diff touches only files in scope.
