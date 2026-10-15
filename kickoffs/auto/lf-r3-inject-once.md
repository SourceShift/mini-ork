# LF-R3 each lesson reaches a prompt once, and the planner credits only what it shows

## Goal

Two injection defects contaminate both the prompts and the efficacy data:

- `mini_ork/cli/execute.py` `_learned_block` returns `pref_block + v2_block + block`,
  where `block` is `context_assembler.failure_modes_md(...)`. In the v2 arm both
  the v2 block and the v1 block render the induced (verified) themes, so 982 of
  982 v2 theme items were injected twice into the same node prompt.
  `context_v2.injects()` claims v2 "replaces the v1 blocks"; it has no callers.
- `mini_ork/cli/plan.py` `_inject_context` computes the v1 producers
  (`failure_modes_md`, `prior_runs_md`) BEFORE it knows the v2 block, then drops
  them when the v2 block is non-empty. `failure_modes_md` runs the UCB pattern
  path, which records retrievals, so UCB was credited for 345 pattern
  retrievals (294 win / 51 loss, 111 runs) that the planner never saw.

Decision record: `docs/plans/2026-10-15-learning-loop-fixes.md` (R3; Q5: runs
before this change are reported as their own arm, which R13 reads from the
pack marker added here).

All paths are relative to the repository root.

## Mechanism (exact)

1. `context_assembler.failure_modes_md(..., include_themes=True)`: a new keyword;
   with `include_themes=False` the induced-themes section is omitted, every
   other section is unchanged. Default behaviour is byte-identical.
2. `_learned_block`: decide on the ARM, not on whether the v2 block came out
   non-empty — `if context_v2.arm(run_id) == "v2"` pass `include_themes=False`;
   the holdout/shadow/off arms are unchanged (byte-identical to today).
3. `_inject_context` (planner): build the v2 block FIRST via
   `_context_v2_planner`. Call the v1 producers only when the v2 block is empty
   (the arm is not v2, or the pack rendered nothing), so a planner prompt that
   carries the v2 block never triggers v1 retrieval logging. Everything else in
   the function (other blocks, skipped-block ledger, injection record) keeps its
   current order and shape.
4. `context_v2.build` stamps `pack["themes_once"] = True` (R13 reports v2 runs
   without the marker as arm `v2_legacy`).
5. Delete `context_v2.injects()` (no callers) and fix the module docstring so it
   no longer says v2 "replaces the v1 blocks" without qualification.

## Files in scope

- `mini_ork/cli/execute.py`
- `mini_ork/cli/plan.py`
- `mini_ork/context_assembler.py`
- `mini_ork/context_v2.py`
- `tests/unit/test_context_v2_wiring.py`
- `tests/unit/test_inject_once.py` (new)

## Tests (`tests/unit/test_inject_once.py`, hermetic temp DB with two verified task themes and one approved pattern)

- v2 arm (`MO_CONTEXT_V2=on`, `MO_CONTEXT_V2_HOLDOUT=0`): the node block from
  `_learned_block` contains each `t:<theme_id>` / theme lesson text exactly once;
- holdout arm (`MO_CONTEXT_V2_HOLDOUT=1`): the node block is byte-identical to
  the block produced before this change (assert against `failure_modes_md`
  with `include_themes=True`);
- v2 arm planner `_inject_context` with a non-empty v2 pack writes zero
  `semantic_memory_uses` rows with `node_id=''` (no phantom retrievals), and a
  v2-arm planner whose pack is empty still gets the v1 blocks;
- a freshly built pack carries `themes_once: true`.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_inject_once.py tests/unit/test_context_v2_wiring.py tests/unit/test_context_v2.py tests/unit/test_inject_verified_only.py tests/unit/test_planner_context_cleanup.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_inject_once.py tests/unit/test_context_v2_wiring.py tests/unit/test_planner_context_cleanup.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r3-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r3-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change theme or pattern SELECTION (R1/R2
own it), `parse_contract`, `scope_findings`, the steering block, or the
preference block, and do not write to the live DB under
`/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
