# SDE-5b supervisor applies proposed graph patches and records their outcomes

## Goal

SDE-5a added typed graph patches (`mini_ork/recovery/graph_patch.py`: `propose`,
`materialize`, `apply`, `list_patches`, `credit`), and the per-run orchestrator
now proposes one on MUTATE. Nothing applies a proposal unless an operator runs
`mini-ork graph-patch apply`, and nothing writes the `NNN.outcome.json` files
`credit()` reads, so the blind-retry control arm has no data. Close both halves
in the SDE-4a supervisor (`mini_ork/recovery/supervisor.py:sweep`).

All paths are relative to the repository root.

## Mechanism (exact spec)

1. **Outcome recording (extends R2).** When R2 labels a revived run whose
   `repair.json` carries `revive_patch` (`"NNN"`), also write
   `<run_dir>/graph-patches/NNN.outcome.json` =
   `{"passed": <the same correct/incorrect R2 computed>, "status": <task_runs.status>, "labelled_at": <now>}`
   — write-once: never overwrite an existing outcome file. `dry_run` writes
   nothing (the action dict gains `"patch": "NNN"`).
2. **New rule R6 — apply a proposed patch.** For each run dir with a
   `graph-patches/` directory whose `task_runs.status` is `failed` (not live, not
   `published`/`superseded`/`not_needed`) and whose `repair.json` state is absent,
   `gave_up` or `revive_failed`:
   - skip when any applied patch of this run has no `NNN.outcome.json` yet (one
     in flight);
   - skip when the run already has `MO_SUPERVISE_MAX_PATCHES_PER_RUN` applied
     patches (default 2; tighten-only: a larger env value clamps down to 2);
   - pick the lowest-numbered patch with status `proposed`, skipping a
     `replan` patch unless `getattr(graph_patch, "REPLAN_SUPPORTED", False)` is
     True (SDE-6a sets that flag when replan becomes applicable; read it at sweep
     time, never cache it at import);
   - control-arm gate: compute `graph_patch.credit(home)` once per sweep; when
     the picked kind is not `rerun`, and both it and `rerun` have `n >= 5`, and
     it is NOT `credited`, skip it with reason
     `<kind> is not better than a blind rerun (<rate> vs <rerun rate>)`;
     fewer samples → apply (exploration);
   - apply with `graph_patch.apply(home, run_id, n, engine_root=engine_root,
     spawn=...)`, sharing the sweep's `max_revives` budget with R1. A
     `GraphPatchError` → action `error` for that run with its message, never a
     raise out of `sweep`;
   - action `{"run_id", "action": "apply_patch", "patch": "NNN", "kind", "reason"}`;
   - `dry_run` computes the same action and applies nothing.
   - The sweep's injectable `spawn` (the R1 seam) must also reach R6: pass a
     spawn adapter to `graph_patch.apply` that calls the injected `spawn(run_id,
     from_node)` in tests; production uses `graph_patch`'s default spawn.
3. `docs/operator/self-driving.md`: document R6 and the outcome files, and how
   `mini-ork graph-patch credit` reads them.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/supervisor.py`
- `docs/operator/self-driving.md`
- `tests/unit/test_supervisor_patches.py` (new)

Do NOT modify any other file. Do not touch `mini_ork/recovery/graph_patch.py`.

## Tests (`tests/unit/test_supervisor_patches.py`, hermetic temp home + sqlite; a tiny workflow.yaml pinned via `MINI_ORK_WORKFLOW`; patches created with `graph_patch.propose`)

- R2 on a revived run with `revive_patch="001"` and `task_runs.status='published'`
  writes `001.outcome.json` with `passed: true`; a second sweep does not rewrite it;
- R6: a failed run with one proposed `rerun` patch and `repair.json` `gave_up`
  → one `apply_patch` action; `001.applied.json` exists; `repair.json` is
  `reviving` with `revive_patch: "001"`; the injected spawn was called;
- R6 skips: a run whose applied patch has no outcome yet; a run at the
  per-run cap; a run that is `executing`; a `replan`-only run (with the flag absent, and also with `graph_patch.REPLAN_SUPPORTED` monkeypatched to False); a run whose
  `repair.json` is `waiting_on_fix`;
- control arm: outcome files giving `swap_lane` 1/5 and `rerun` 4/5 → a
  proposed `swap_lane` is skipped with the "not better than a blind rerun"
  reason; with `swap_lane` 2/3 (below n=5) it is applied;
- `MO_SUPERVISE_MAX_PATCHES_PER_RUN=9` clamps to 2;
- `max_revives=1` with one R1 revive pending and one R6 candidate → exactly one
  of them acts;
- `dry_run=True` → same R6 action, no `applied.json`, spawn not called;
- a patch that fails to materialize (edge to a missing node) → `error` action,
  the sweep continues.

## Verification commands

- `pytest -q tests/unit/test_supervisor_patches.py tests/unit/test_supervisor.py tests/unit/test_supervisor_superseded.py tests/unit/test_graph_patch.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_supervisor_patches.py --junitxml="${MINI_ORK_RUN_DIR}/patch-apply-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/patch-apply-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/recovery/supervisor.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes. `task_runs.status` values are
`classified, planned, executing, verifying, reviewing, published, rolled_back,
failed, not_needed, superseded`; epic statuses are `not started, in progress,
in review, done, blocked, escalated, abandoned`. Never use another value in a
test fixture.
