# Gap S4: reflect records its learning-stage health (the pass-stats producer)

## Goal

`record_pass_stat` (`mini_ork/learning/ledger.py:201-212`) writes one row per
learning stage per reflect pass into `learning_pass_stats`. Nothing outside
`ledger.py` calls it, so the table is empty on every home. Its readers see
nothing:

- `ledger.stage_health` (`ledger.py:305-377`) raises `alarm` for a stage whose
  last 3 rows all have `outputs == 0` while one has `inputs > 0`;
- the IDE Learn overview's "N learning stage stalled" chip
  (`mini_ork/ide_pages/learn/overview.py:122-137`) and its loop-health table
  (`overview.py:374-420`, "Reflect has not reported yet").

Every reflect path goes through `reflect.main` (`mini_ork/cli/reflect.py`):
post-run (`mini_ork/cli/main.py:2271`), the in-workflow reflector node, and
manual runs. Wire the two stages whose numbers are exact.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. Pass id

In `reflect.main`, `trace_id = f"tr-reflect-…"` (~329) identifies the pass.
Around the call to the reflection pipeline (~493), apply
`MO_REFLECT_PASS_ID=<trace_id>` with `mini_ork.context.scoped_environ` (the
only sanctioned way to change process env), so the pipeline's stages can read
it.

### 2. Stage `gradient_extract` — `mini_ork/learning/reflection_pipeline.py`

At the end of `reflection_extract_gradients` (~743-862), when
`MO_REFLECT_PASS_ID` is set and the run is not a dry run, call
`record_pass_stat(pass_id, "gradient_extract", inputs=…, outputs=…, failures=…, lane=…, last_error=…)`:

- `inputs`: traces the stage could act on = traces it selected MINUS those it
  skipped because they already had a gradient (`skipped_watermark`) or were
  layer-refused. A pass where every trace was already processed writes
  `inputs=0`, so it can never look stalled.
- `outputs`: gradients extracted (`extracted`).
- `failures`: `dispatch_failed` plus the traces skipped because the cost
  circuit was open (`circuit_open`).
- `lane`: the lane used for extraction (the one `gradient_extractor` resolved;
  `""` when unknown).
- `last_error`: the last dispatch failure text when `failures > 0`, else `""`.

### 3. Stage `pattern_induction` — `mini_ork/cli/reflect.py`

After the `induce_report` block (~546-580), when `induce_report is not None`
and not a dry run, call `record_pass_stat(trace_id, "pattern_induction", …)`
with:

- `inputs` = `induced + len(skipped)`;
- `outputs` = `induced`;
- `failures` = number of `skipped` entries whose `reason == "dispatch failed"`;
- `lane` and `last_error` from the LAST such failing entry (`model`, `error`),
  else `""`.

A stage whose report is `None` (turned off, or it raised) writes nothing.

### 4. Fail-soft

Both calls are wrapped so a ledger error can never fail reflect
(`record_pass_stat` already never raises; keep any surrounding arithmetic in
the same `try`). Pass the reflect DB path explicitly (`db=db_path`).

## Files in scope (touch ONLY these)

- `mini_ork/cli/reflect.py`
- `mini_ork/learning/reflection_pipeline.py`
- `tests/unit/test_reflect_pass_stats.py` (new)

Do not modify `mini_ork/learning/ledger.py`, `mini_ork/ide_pages/`, `mini_ork/rsi/harvest.py`, `docs/`, any migration, or any path in `config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (`tests/unit/test_reflect_pass_stats.py`, hermetic, no LLM)

Use a tmp DB initialised with `mini_ork.stores.migrate.init_db` and the
reflect test fixtures in `tests/unit/test_mini_ork_reflect_py.py` (~206) as a
model; stub every LLM dispatch.

1. A reflect pass whose induction report is `{"induced": 2, "skipped": [{"reason": "dispatch failed", "model": "glm", "error": "boom"}, {"reason": "too small"}]}`
   writes one `pattern_induction` row: inputs 4 (`2 + 2`),
   outputs 2, failures 1, lane `glm`, last_error `boom`, and `pass_id` equal to
   the pass's trace id.
2. A pass where every selected trace already has a gradient writes a
   `gradient_extract` row with `inputs == 0` and `outputs == 0`, and
   `ledger.stage_health` does not alarm on three such passes.
3. A pass that extracts 3 gradients from 3 new traces writes inputs 3,
   outputs 3, failures 0.
4. With induction turned off (report `None`), no `pattern_induction` row.
5. A dry run writes no rows.
6. A `record_pass_stat` that raises (monkeypatched) does not fail reflect.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_reflect_pass_stats.py tests/unit/test_learning_ledger.py tests/unit/test_mini_ork_reflect_py.py tests/unit/test_reflect_run_scope.py tests/unit/test_reflect_trace_finalize.py tests/unit/test_reflect_deadline.py tests/unit/test_pattern_induction_py.py tests/unit/test_ide_pages_learn_overview.py tests/unit/test_rsi_harvest_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_reflect_pass_stats.py --junitxml="${MINI_ORK_RUN_DIR}/gap-s4-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/gap-s4-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/cli/reflect.py mini_ork/learning/reflection_pipeline.py tests/unit/test_reflect_pass_stats.py` is clean.
- A normal reflect pass writes `learning_pass_stats` rows, and an all-already-processed pass never reads as stalled.
