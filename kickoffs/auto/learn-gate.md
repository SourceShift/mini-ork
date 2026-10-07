# Learning gate — "approved" requires an authored lesson; extractor failures name the provider error

## Why

1. **Approval without content.** `reflection_verify_patterns`
   (`mini_ork/learning/reflection_pipeline.py:886`) moves `emergent_patterns` from `proposed` to
   `approved` when `strength_score >= MO_EMERGENT_VERIFY_MIN_STRENGTH` and the members span enough
   independent runs. Nothing checks that the pattern says anything. Today 43 rows are approved and
   0 have a `lesson_text`. Each is just the miner's cluster key, e.g.
   `cluster: task_class=recipe_authoring status=success (freq=46 in window)`, yet downstream code
   calls these "verified" / "judge-gate approved". Pattern induction
   (`mini_ork/learning/pattern_induction.py`) writes `lesson_text` into both `pattern_records`
   and `emergent_patterns`. It runs in `reflect` right AFTER `reflection_run` (which ends with
   this gate), so requiring a lesson costs one reflect pass of delay, no deadlock.
2. **Blind extractor failure.** `gradient_extractor._default_dispatch` (`:315-341`) captures the
   provider's stderr into a `StringIO` and drops it. On rc != 0, `extract` dies with the bare
   `gradient_extract: LLM dispatch failed` (`:501-502`), with no lane and no provider message. The
   sibling induction stage was silently dead for weeks this way (its default lane `codex` returns
   `400 'gpt-6-astra' model is not supported`).

## Files in scope (touch ONLY these)

- `mini_ork/learning/reflection_pipeline.py`: ONLY `reflection_verify_patterns` and the
  `[verify]` log lines in `reflection_run` (~:1041-1053)
- `mini_ork/learning/gradient_extractor.py`: ONLY `_default_dispatch` and the rc check in
  `extract` (~:501)
- `tests/unit/test_reflection_pipeline_py.py`
- `tests/unit/test_gradient_extractor_py.py`

Do NOT modify any other file.

## Fixes (exact)

1. **Require a lesson to approve.** In `reflection_verify_patterns`, also SELECT `lesson_text`.
   Probe the column the way `context_assembler._approved_emergent_rows` does: a DB without the
   column has no lessons. A `proposed` row is approved only if it clears both existing floors AND
   `COALESCE(lesson_text,'')` is non-blank. Rows that clear both floors but have no lesson stay
   `proposed`. Count them and write one stderr line:
   `  [verify] <n> pattern(s) held: floors met, no authored lesson yet`.
   Opt-out: `MO_EMERGENT_VERIFY_REQUIRE_LESSON=0` restores today's behaviour. Do NOT demote rows
   that are already `approved`. Keep the function's stdout (approved count) and return value
   unchanged in shape.
2. **Log wording.** In `reflection_run`, change `"[verify] judge-gate emergent_patterns"` to
   `"[verify] emergent_patterns gate (strength + independent runs + authored lesson)"`, and the
   result line to `f"reflection_run: {approved} emergent_patterns approved"`.
3. **Extractor error text.** Keep `_default_dispatch`'s `(rc, out)` return shape; tests
   monkeypatch it. On a failed call (non-zero rc, or an exception), store the resolved lane and
   the last 300 chars of captured stderr (or the exception text) in a module-level
   `_last_dispatch_error: str`, guarded by a `threading.Lock`. In `extract`, the failure becomes
   `_fail(f"gradient_extract: LLM dispatch failed on lane {lane}: {err}")`, falling back to the
   old text when nothing was captured.

## Tests

- `tests/unit/test_reflection_pipeline_py.py`: extend `_seed_emergent` with an optional
  `lesson_text` element (default None) without breaking existing callers. New tests:
  - a row meeting both floors with a lesson → approved
  - the same without a lesson → stays proposed, and the "held" line is on stderr
  - `MO_EMERGENT_VERIFY_REQUIRE_LESSON=0` → approved without a lesson
  - an already-approved row without a lesson stays approved
  Existing gate tests: give their seeded rows a lesson so they keep testing the floors.
- `tests/unit/test_gradient_extractor_py.py`: a fake native dispatcher that writes `boom 400` to
  stderr and returns 1 → `extract` raises `SystemExit`, and its stderr contains `boom 400` and the
  lane name.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_reflection_pipeline_py.py tests/unit/test_gradient_extractor_py.py tests/unit/test_context_assembler_py.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/learning/reflection_pipeline.py mini_ork/learning/gradient_extractor.py tests/unit/test_reflection_pipeline_py.py tests/unit/test_gradient_extractor_py.py` → clean.
- `git diff --stat` touches only the files in scope, and only the functions named above.
