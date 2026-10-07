# Learning injection — record what each node was given; inject only patterns that carry a lesson

## Why

The IDE node "Learning" tab cannot show what a node actually received, because the text is
built and then thrown away:

- `_learned_block()` (`mini_ork/cli/execute.py:2376`) builds the block appended to every
  researcher / implementer / reviewer prompt: learned failure modes (`failure_modes_md`),
  emergent patterns (`semantic_lessons_md` or `_static_emergent_block`) and operator steering
  (`operator_steering.fetch_for`, which CONSUMES the rows). Its caller
  (`mini_ork/cli/execute_handlers.py:438`) passes it on and never writes it anywhere.
- Approved emergent patterns are injected even when `lesson_text` is empty. Today all 43
  approved rows in `state.db` have no lesson, so every LLM node prompt carries lines such as
  `[best_practice_rule] cluster: task_class=recipe_authoring status=success (freq=46 in window)`.
  That is a frequency count, not guidance (`context_assembler.py:484` says so).
- The block header says `judge-gate approved`, but approval
  (`mini_ork/learning/reflection_pipeline.py:931`) only checks strength ≥ 3 and an
  independent-run count. Nothing reads the content.

## Files in scope (touch ONLY these)

- `mini_ork/context_assembler.py`
- `mini_ork/cli/execute.py`: ONLY `_learned_block`. Another worktree is editing
  `_run_verifier_ref` in this file; do not touch anything else here.
- `mini_ork/cli/execute_handlers.py`: ONLY the lines around the `_learned_block` call (~:431-440)
- `tests/unit/test_context_assembler_py.py`
- `tests/unit/test_learned_record.py` (new)

Do NOT modify any other file.

## Fixes (exact)

1. **Source IDs out-param.** Add a keyword-only `sources: list[dict] | None = None` to
   `failure_modes_md`, `_static_emergent_block` and `semantic_lessons_md`. When a list is given,
   append one dict per row that was actually injected, in prompt order:
   - gradient: `{"kind": "gradient", "id": <gradient_id>, "target": ..., "signal": ...,
     "suggested_change": ...}`. Add `gradient_id` to the SELECT at `context_assembler.py:385`.
   - pattern: `{"kind": "pattern", "id": <pattern_id>, "text": <the exact injected line without
     the "- " prefix>}`. In `semantic_lessons_md`, keep a `memory_id → pattern_id` map while
     building `candidates` so each `ordered` hit can be traced back to its pattern.
   With `sources=None` the returned markdown must be byte-identical to the same call with a list.
2. **Inject only patterns that have a lesson.** In both injection paths (`_static_emergent_block`
   and `semantic_lessons_md`), skip approved rows whose `lesson_text` is NULL or blank. Do it in
   the injection paths, not in `_approved_emergent_rows`, which other code reads. Opt-out:
   `MO_EMERGENT_INJECT_UNLESSONED=1` restores today's behaviour. If nothing is left, the pattern
   block is omitted entirely (no empty header).
3. **Honest header.** Replace `--- Verified emergent patterns (cross-run, judge-gate approved) ---`
   with `--- Lessons from recurring patterns (prior runs) ---`, and the closer with
   `--- /lessons from recurring patterns ---`, in BOTH paths. Update the four asserts in
   `tests/unit/test_context_assembler_py.py` (lines ~234, 250, 549, 619) and give the seeded
   approved patterns there a `lesson_text`, so they still exercise injection.
4. **`_learned_block` collects sources.** Add a keyword-only `sources: list[dict] | None = None`.
   Pass it through to `failure_modes_md`. For each steering row appended, add
   `{"kind": "steering", "id": row["id"], "severity": ..., "source": ..., "message": ...}`.
   Return type stays `str`.
5. **Persist the record.** Add `_write_learned_record(run_dir, node_id, node_type, lane,
   task_class, attempt, block, sources)` in `execute_handlers.py`. Call it right after
   `_learned_block`, with `run_dir_eff` (resolved at :293) and the attempt number from
   `_node_attempt_no(db, run_id, node_id)`. Only call it when `node_type in ("researcher",
   "implementer", "reviewer")`. It writes:
   - `<run_dir>/learned/<node_id>.md`: `block.strip()` + `"\n"`, only when the block is
     non-empty. If the block is empty, remove any stale `.md` from an earlier attempt.
   - `<run_dir>/learned/<node_id>.json`, always:
     `{"node_id", "node_type", "lane", "task_class", "attempt": int, "written_at": int epoch
     seconds, "injected": bool, "reason": "" | "opt-out" | "nothing matched", "sources": [...]}`.
     `reason` is `"opt-out"` when `MO_INJECT_LEARNINGS != "1"`, `"nothing matched"` when the
     block is empty, else `""`.
   Write atomically (tmp file + `os.replace`). Never raise: any exception is swallowed and the
   node dispatches exactly as before.

## Tests (`tests/unit/test_learned_record.py`, temp DB, no network)

- `failure_modes_md(..., sources=lst)` returns the same markdown as without; `lst` holds the
  injected `gradient_id`s in order.
- An approved pattern with `lesson_text` is injected (its lesson text, its `pattern_id` in
  sources). One without `lesson_text` is not. With `MO_EMERGENT_INJECT_UNLESSONED=1` both are.
  Cover both the static path (`MO_SEMANTIC_INJECT=0`) and the semantic path.
- No pattern has a lesson → no `Lessons from recurring patterns` header at all.
- `_write_learned_record` with a block → both files, exact JSON keys. With `""` → JSON only,
  `injected: false`, `reason: "nothing matched"`, and a pre-existing `.md` is removed.
  Unwritable run_dir → no exception.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_learned_record.py tests/unit/test_context_assembler_py.py tests/unit/test_mini_ork_plan_py.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/context_assembler.py mini_ork/cli/execute.py mini_ork/cli/execute_handlers.py tests/unit/test_learned_record.py` → clean.
- `git diff --stat` touches only the files in scope, and in `execute.py` only `_learned_block`.
