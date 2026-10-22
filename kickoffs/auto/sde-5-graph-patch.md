# SDE-5a graph patches: typed, immutable run-graph edits applied through recover

## Goal

When a run fails for a reason a plain retry cannot fix, the repair loop can
re-run nodes (`mini-ork recover --from-node`) and swap lanes (`--lane`), but it
cannot change the run's graph. `recover --workflow <path>` already accepts a
modified workflow (finished nodes upstream of `--from-node` are reused), and
the per-run orchestrator already decides MUTATE for harness-surface faults
(`mini_ork/orchestration/runner_orchestrator.py:decide`, rule 4), yet MUTATE only
emits an `orchestrator.mutate.proposed` event that nothing consumes.

Add graph patches: typed, immutable files under the run dir that are validated
by the workflow compiler and applied through `recover --workflow`, and make
MUTATE write one.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. New `mini_ork/recovery/graph_patch.py`:
   - A patch is `<run_dir>/graph-patches/NNN.yaml` (NNN = zero-padded 3-digit
     sequence from `001`). Written once with `O_CREAT|O_EXCL`; never rewritten.
     Keys: `kind` (one of `rerun`, `swap_lane`, `insert_before`, `replace`,
     `replan`), `from_node` (required), `node`, `lane`, `new_node`, `reason`
     (required, non-empty), `source` (`orchestrator`|`operator`|`supervisor`),
     `created_at`. Unknown keys or kinds are invalid.
     - `rerun`: `from_node` must name an existing node.
     - `swap_lane`: requires `node` and `lane`; `from_node` must equal `node`.
     - `insert_before`: requires `node` and `new_node` (a node mapping in the
       workflow's own node shape, with at least `name` and `type`, a `name` not
       already in the workflow); `from_node` must equal `new_node.name`.
     - `replace`: requires `node` and `new_node` with `new_node.name == node`;
       `from_node` must equal `node`.
     - `replan`: accepted by `propose` (requires `from_node` and `reason`), but
       `materialize`/`apply` refuse it with `replan patches are not applicable yet`
       (SDE-6 implements them).
   - `propose(run_dir, patch: dict) -> int` — schema check (not compile), stamp
     `created_at` when absent, write the next number, return it. A `rerun` or
     `replan` proposal identical in (`kind`, `from_node`) to an existing
     not-yet-applied proposal returns that proposal's number instead of writing
     a new file.
   - `base_workflow(home, run_id) -> str` — the newest applied patch's
     `NNN.workflow.yaml` if any patch has been applied, else the run's own
     workflow path resolved the way `mini_ork/recovery/planner.py` resolves it
     for `recover` (call its resolver; do not copy its logic).
   - `materialize(home, run_id, n) -> str` — load the base workflow YAML, apply
     the patch (`rerun` = unchanged copy; `swap_lane` sets the node's
     `model_lane`; `insert_before` inserts `new_node` before `node` in `nodes`,
     redirects every edge whose `to` is `node` to the new node, and adds a
     `depends_on` edge new node → `node`; `replace` swaps the node mapping in
     place), write `<run_dir>/graph-patches/NNN.workflow.yaml`, then compile it
     with `mini_ork.workflow.compiler.compile_workflow(path)`. A compile error, a
     missing referenced node, or any rule above failing raises
     `GraphPatchError` with the reason, and leaves no `NNN.workflow.yaml` behind.
   - `apply(home, run_id, n, *, engine_root, spawn=None, dry_run=False) -> dict`
     — refuse (raise `GraphPatchError`) when `NNN.applied.json` exists or a
     higher-numbered patch is already applied; materialize; then (unless
     `dry_run`) spawn a detached
     `<python> -m mini_ork.recovery.planner <run_id> --from-node <from_node> --workflow <NNN.workflow.yaml> --force`
     with the same environment shape as
     `mini_ork/recovery/supervisor.py:_default_spawn` (`MINI_ORK_HOME`,
     `MINI_ORK_ROOT=engine_root`, `MO_AUTO_REPAIR=1`; `MINI_ORK_RUN_ID`,
     `MINI_ORK_RUN_DIR`, `MINI_ORK_PLAN_PATH` removed; new session; output to
     `runs/<id>/graph-patch-NNN.log`); `spawn` is an injectable
     `callable(argv) -> pid`. Write `NNN.applied.json`
     (`applied_at`, `pid`, `engine_sha`, `from_node`) and update `repair.json`
     atomically keeping every other key: `state="reviving"`, `revived_at`,
     `revive_from_node`, `revive_patch="NNN"` — the SDE-4a supervisor's R2 then
     labels the revived run's outcome.
   - `list_patches(home, run_id) -> list[dict]` — every patch with its number,
     kind, from_node, reason and `status` (`proposed` / `applied`, plus
     `outcome` from `NNN.outcome.json` when present).
   - `credit(home) -> dict` — over every run's `graph-patches/NNN.outcome.json`
     (`{"passed": bool}`; written by a later step), per kind: `n`, `passed`,
     `rate`; a kind other than `rerun` is `credited` only when it and `rerun`
     each have `n >= 5` and its rate is strictly above `rerun`'s (the blind
     retry is the control arm). Missing outcome files → `n == 0`.
2. New `mini_ork/cli/graph_patch.py` — `mini-ork graph-patch list <run_id>`,
   `propose <run_id> --kind K --from-node N [--node X] [--lane L] [--new-node-json J] --reason R`,
   `validate <run_id> <n>` (materialize only), `apply <run_id> <n> [--dry-run]`,
   `credit` — `--json` on every verb; exit 2 on `GraphPatchError` with its message.
   Ends with an `if __name__ == "__main__":` guard.
3. `mini_ork/cli/main.py`: `"graph-patch": "mini_ork.cli.graph_patch"` in
   `_NATIVE_MODULE_SUBS` and one `_HELP` line; `tests/unit/test_native_dispatch_py.py`
   adds `"graph-patch"` to the exact expected set.
4. `mini_ork/cli/execute.py:_orchestrator_act`, MUTATE branch: after the existing
   `orchestrator.mutate.proposed` emit, best-effort (never raises, never blocks
   the run) call `graph_patch.propose(run_dir, ...)` with `kind="rerun"` when the
   action payload's `effective_locus == "verifier"` (re-verify), else
   `kind="replan"`; `from_node=node_id`, `reason=action.reason`,
   `source="orchestrator"`; add the patch number to the emitted proposal as
   `graph_patch`.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/graph_patch.py` (new)
- `mini_ork/cli/graph_patch.py` (new)
- `mini_ork/cli/main.py`
- `tests/unit/test_native_dispatch_py.py`
- `mini_ork/cli/execute.py`
- `tests/unit/test_graph_patch.py` (new)

Do NOT modify any other file. Do not touch `mini_ork/recovery/supervisor.py`,
`mini_ork/recovery/planner.py` or `mini_ork/workflow/compiler.py`.

## Tests (`tests/unit/test_graph_patch.py`, hermetic temp home; a small workflow.yaml written by the test in the repo's recipe workflow shape)

- `propose` numbers 001, 002…; a second identical `rerun` proposal returns 001;
  a patch file is never rewritten (writing an existing number raises);
- schema: unknown kind, unknown key, missing reason, `swap_lane` with
  `from_node != node` → invalid;
- `materialize`: `swap_lane` changes exactly one `model_lane`; `insert_before`
  adds the node, redirects the incoming edges and adds the new → old edge, and
  the result compiles; a `new_node` whose name already exists → `GraphPatchError`
  and no `NNN.workflow.yaml`; a patch that breaks compilation (an edge to a
  missing node) → `GraphPatchError`;
- `replan` proposes fine but `materialize`/`apply` raise
  `replan patches are not applicable yet`;
- `apply` with a fake `spawn`: argv contains `--from-node`, `--workflow
  <...NNN.workflow.yaml>` and `--force`; `NNN.applied.json` written;
  `repair.json` gains `state=reviving`, `revive_patch`, and keeps a pre-existing
  key; a second `apply` of the same number raises; `dry_run` spawns nothing and
  writes no `applied.json`;
- `base_workflow` after an applied patch returns that patch's workflow file;
- `credit` with outcome files: `swap_lane` 4/5 vs `rerun` 2/5 → swap_lane
  credited; with `rerun` n=3 → not credited;
- CLI: `validate` on a bad patch exits 2; `list --json` lists statuses;
- MUTATE wiring: drive `_orchestrator_act` (or the smallest seam around the
  MUTATE branch) with a harness-locus MUTATE action for `effective_locus` =
  `verifier` and = `prompt` → one `rerun` and one `replan` proposal file; a
  propose that raises does not raise out of `_orchestrator_act`.

## Verification commands

- `pytest -q tests/unit/test_graph_patch.py tests/unit/test_native_dispatch_py.py tests/unit/test_runner_orchestrator.py tests/test_recovery_closure.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_graph_patch.py --junitxml="${MINI_ORK_RUN_DIR}/graph-patch-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/graph-patch-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/recovery/graph_patch.py mini_ork/cli/graph_patch.py mini_ork/cli/execute.py mini_ork/cli/main.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
