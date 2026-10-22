# SDE-6b split graph patches: hand a failed run's remaining work to child epics

## Goal

The repair ladder ends in "replan the remaining subgraph → split → fix epic →
human". Graph patches (`mini_ork/recovery/graph_patch.py`) now cover rerun,
lane swap, node insert/replace and replan (SDE-5a, SDE-6a), and the supervisor
applies proposed patches (SDE-5b, rule R6). The `split` rung is missing: when a
run's remaining work is too large for one run, break it into child epics that
the scheduler delivers, and close the parent once they are done.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. `mini_ork/recovery/graph_patch.py`:
   - New kind `split` (add it to `KINDS`; add `children` to the allowed keys).
     Schema: requires `from_node`, `reason`, and `children`: a list of 1 to 4
     mappings, each with non-empty string `title` and `goal` and no other keys.
   - `materialize` refuses `split` with `split patches do not change the
     workflow; apply them` (there is no workflow to compile).
   - `apply` for `split` (no recover spawn; `spawn` is never called):
     - Parent kickoff text: `context-pack.v2.json` `kickoff_snapshot` in the run
       dir when present and non-empty, else the file named by
       `run_profile.json` `kickoff_path`; neither → `GraphPatchError`.
     - For child `i` (1-based) create epic id `split-<run_id>-<NNN>-<i>` and
       write its kickoff `<run_dir>/graph-patches/NNN-split/<epic_id>.md`: the
       parent kickoff text, then a section `## This child's scope` with the
       child title and goal, then `Parent run: <run_id> (failing node
       <from_node>, graph patch NNN).`
     - DB (`MINI_ORK_DB` or `<home>/state.db`): call
       `mini_ork.scheduler.ensure_retry_schema(db)`, then insert one `epics` row
       per child: `id`, `title`, `status='not started'`, `kickoff_path` (the file
       above), `recipe` (the parent's `task_runs.recipe`), `notes`
       (`split from <run_id> graph patch NNN`). All inserts in one transaction;
       any failure rolls back, removes the kickoff files written, and raises.
       An epic id that already exists → `GraphPatchError` (no partial split).
     - Write `NNN.applied.json` (`applied_at`, `child_epic_ids`, `from_node`) and
       update `repair.json` keeping every other key: `state="waiting_on_children"`,
       `child_epic_ids`, `split_patch="NNN"`.
     - The live-run refusal SDE-5a added applies to `split` too.
2. `mini_ork/cli/graph_patch.py`: `propose` accepts `--children-json J` (a JSON
   list) for `--kind split`.
3. `mini_ork/recovery/supervisor.py`, new rule **R7 — close a split**: for every
   `repair.json` with `state == "waiting_on_children"`:
   - every child epic `done` → compare-and-set the parent's `task_runs.status`
     from `failed` to `superseded` with note ` superseded: delivered by split
     children <ids>`, set `repair.json` `state="split_done"`, and write
     `graph-patches/<split_patch>.outcome.json` `{"passed": true, ...}`
     (write-once). Action `split_done`;
   - any child `escalated` or `abandoned` → `repair.json` `state="gave_up"` with
     a note naming the child, outcome `{"passed": false, ...}`, and one deduped
     oversight item (`gate_id="self-driving-supervisor"`,
     `feature=f"split child failed: {run_id}"`, skipped when that row exists in
     any status). Action `split_failed`;
   - otherwise no action. `dry_run` writes nothing.
4. `docs/operator/self-driving.md`: document the split rung and R7.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/graph_patch.py`
- `mini_ork/cli/graph_patch.py`
- `mini_ork/recovery/supervisor.py`
- `docs/operator/self-driving.md`
- `tests/unit/test_graph_patch_split.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_graph_patch_split.py`, hermetic temp home + sqlite built by applying the repo's real migrations)

- schema: 0 children, 5 children, a child missing `goal`, a child with an extra
  key → invalid; `materialize` on a split → the refusal message;
- `apply` with 2 children: two `epics` rows `not started` with the parent's
  recipe, kickoff files exist and contain the parent text and the child goal,
  `applied.json` lists both ids, `repair.json` is `waiting_on_children` and
  keeps a pre-existing key, `spawn` was never called;
- `apply` when one child id already exists → `GraphPatchError`, no new epics
  rows, no kickoff files left behind;
- parent kickoff falls back from `kickoff_snapshot` to `run_profile.json`
  `kickoff_path`; neither present → `GraphPatchError`;
- a live parent run (`executing`) → refused;
- R7: both children `done` → parent `superseded` with the note, state
  `split_done`, outcome `passed: true`; one child `escalated` → `gave_up`, one
  oversight row, outcome `passed: false`, and a second sweep adds no second row;
  children still `in progress` → no action; `dry_run` → same action, no writes.

Epic statuses allowed by the schema: `not started`, `in progress`, `in review`,
`done`, `blocked`, `escalated`, `abandoned`. `task_runs.status` values:
`classified, planned, executing, verifying, reviewing, published, rolled_back,
failed, not_needed, superseded`. Never use another value in a test fixture.

## Verification commands

- `pytest -q tests/unit/test_graph_patch_split.py tests/unit/test_graph_patch.py tests/unit/test_graph_patch_replan.py tests/unit/test_supervisor.py tests/unit/test_supervisor_superseded.py tests/unit/test_supervisor_patches.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_graph_patch_split.py --junitxml="${MINI_ORK_RUN_DIR}/split-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/split-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/recovery/graph_patch.py mini_ork/recovery/supervisor.py mini_ork/cli/graph_patch.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
