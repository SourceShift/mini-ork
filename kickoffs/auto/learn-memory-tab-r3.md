# Memory tab — revision 3: Lane fit from execution traces, not agent_performance_memory

WIP commit d60d1190 holds revision 2: Opus passed it, 34 tests pass, ruff clean. ONE defect showed
on live data, so fix only that.

## Why

`_lane_fit` (`mini_ork/ide_pages/learn/memory.py:142`) computes pass rate =
`success_count / runs_count` from `agent_performance_memory`. On the live DB every lane reads
about 1% (e.g. `glm ★` reviewer on chapter_review: 88 runs, 1%), because two writers give
`success_count` different meanings:

- `mini_ork/learning/writeback.py:323` stores the count of successful runs;
- `mini_ork/lane_router.py:257` stores the count of comparison groups the lane won
  (`wins = 1 if lane_adv > 0`).

Both upsert on `(agent_version_id, task_class)`, so the last writer wins. Example: lane
`minimax`, class `verified_artifact`. The table says 399 runs / 1 success; `execution_traces`
says 394 success / 5 failure (4 of them `validity='infra_failed'`). A separate kickoff fixes the
writers. This tab must not depend on that column at all.

## Files in scope

- `mini_ork/ide_pages/learn/memory.py`: ONLY `_lane_fit` and its note
- `tests/unit/test_ide_pages_learn_memory.py`

Do NOT modify any other file.

## Fix (exact)

Compute Lane fit from `execution_traces`. Read-only, last 28 days (`created_at` is ISO text;
compare against an ISO cutoff).

- Rows: `agent_version_id <> ''`, `task_class <> ''`, `COALESCE(validity,'valid') = 'valid'`
  (infra exits the agent never controlled are excluded), `status IN ('success','failure')`.
  `vacuous` and `running` are left out of both numerator and denominator.
- Group by `(task_class, role, lane)`:
  - `role` = `json_extract(verifier_output, '$.node_type')`, or the `tr-<type>-` prefix of
    `trace_id`, else `"?"`;
  - `lane` = `agent_version_id`.
- Columns stay `class | role | lane | runs | pass rate | cost / run`; runs = success + failure;
  pass rate = success / runs; cost / run = `AVG(cost_usd)`.
- Keep the existing display rules: ≥ 3 runs shown, top 8 classes by runs, top 4 lanes per class
  by pass rate, ★ on the best lane with ≥ 5 runs, colours green ≥ 70% / red < 40%.
- Note: "From execution traces of the last 28 days (valid runs only: infra exits excluded). ★ =
  best pass rate with at least 5 runs."
- Empty state unchanged.

Tests: seed `execution_traces` so one lane has 9 success + 1 failure (90%, ★), another has 2
success + 3 failure (40%), one `infra_failed` row (excluded), one `vacuous` row (excluded), and
one row older than 28 days (excluded). Assert the exact cells. Remove any test that seeded
`agent_performance_memory` for this section.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_learn_memory.py tests/unit/test_ide_pages_learn.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/ide_pages/learn/memory.py tests/unit/test_ide_pages_learn_memory.py` → clean.
- Live proof: `build_page(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "learn", "memory", {})`, Lane fit section only.
  `minimax` researcher on `verified_artifact` shows ~99%, not 1%. Paste it.
- `git diff d60d1190 --stat` touches only the files in scope.
