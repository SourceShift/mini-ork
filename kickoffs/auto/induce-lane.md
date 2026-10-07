# Pattern induction — use the reflect lane, and say so when every analyst call fails

## Why

`pattern_records` has 57 rows and **0** with `lesson_text`; `emergent_patterns` has 43 approved
rows and **0** with a lesson. Induction (`mini_ork/learning/pattern_induction.py`, called from
`mini_ork/cli/reflect.py:305`) is on by default and runs after every reflect. It has never written
a lesson, because:

- `_default_dispatch` (`pattern_induction.py:381`) defaults the lane to
  `MINI_ORK_INDUCE_MODEL` or `"codex"`. The codex lane is dead. Probed 2026-10-07:
  `400 invalid_request_error: The 'gpt-6-astra' model is not supported when using Codex with a
  ChatGPT account`.
- `reflect` resolves a working lane for the sibling gradient extractor from the `reflector` role
  (`reflect.py:191-196` → `MINI_ORK_GRADIENT_MODEL`), but never gives that lane to induction.
- Failures are silent. `_default_dispatch` captures stderr into a `StringIO` and discards it. A
  non-zero rc becomes "no proposals", so reflect prints only `authored 0 lesson(s), N cluster(s)
  left without one`, which is indistinguishable from "the traces had nothing to teach".

## Files in scope (touch ONLY these)

- `mini_ork/learning/pattern_induction.py`
- `mini_ork/cli/reflect.py`: ONLY the induction block (~:297-320)
- `docs/CONFIG.md`: ONLY the env-var rows for `MINI_ORK_GRADIENT_MODEL` and a new
  `MINI_ORK_INDUCE_MODEL` row
- `tests/unit/test_pattern_induction_py.py`

Do NOT modify any other file.

## Fixes (exact)

1. **Lane chain.** In `_default_dispatch`, the model is `model or
   os.environ.get("MINI_ORK_INDUCE_MODEL") or os.environ.get("MINI_ORK_GRADIENT_MODEL") or
   "codex"`. In `reflect.py`, pass `model=os.environ.get("MINI_ORK_INDUCE_MODEL") or
   gradient_model` explicitly to `induce_pending`.
2. **Keep the `(rc, out)` return shape** of `_default_dispatch`; tests monkeypatch it. On a failed
   call (non-zero rc, empty stdout, or an exception), store the last 300 chars of the captured
   stderr (or the exception text) in a module-level `_last_dispatch_error: str`, guarded by a
   `threading.Lock`. Calls run in a `ThreadPoolExecutor`.
3. **Count calls.** `propose_lessons` gets a keyword-only `stats: dict | None = None`. When given,
   set `stats["calls"]` (batches dispatched) and `stats["failed"]` (calls with rc != 0 or empty
   output).
4. **Say why.** In `induce_cluster`, when `stats["calls"] > 0 and stats["failed"] ==
   stats["calls"]`, return `("", {"target": ..., "reason": "dispatch failed", "model":
   <resolved lane>, "error": <_last_dispatch_error>})` instead of "no proposal survived the
   guardrails".
5. **Reflect prints it.** After the existing `[pattern_induct] authored …` line, if any `skipped`
   entry has `reason == "dispatch failed"`, write ONE line to stderr:
   `  [pattern_induct] every analyst call failed on lane <model>: <error>`.
6. **Docs.** `MINI_ORK_GRADIENT_MODEL` default → "the `reflector` role in agents.yaml". Add
   `MINI_ORK_INDUCE_MODEL` | "same lane as `MINI_ORK_GRADIENT_MODEL`" | "Provider lane for pattern
   induction (authors `lesson_text` for mined clusters)".

## Tests (`tests/unit/test_pattern_induction_py.py`, no network)

- With `MINI_ORK_INDUCE_MODEL` unset and `MINI_ORK_GRADIENT_MODEL=glm`, `_default_dispatch` passes
  `--model glm`. With both set, INDUCE wins. With neither, `codex`. Use the existing
  `_fake_native` helper and capture argv.
- Every call fails (`_default_dispatch` → `(1, "")` after setting `_last_dispatch_error` via a fake
  native that writes `boom 400` to stderr and returns 1) → `induce_cluster` returns `reason ==
  "dispatch failed"`, `"boom 400" in detail["error"]`.
- One of two batches fails → NOT "dispatch failed" (normal guardrail path).
- The existing tests at lines ~411-440 keep passing unchanged.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_pattern_induction_py.py tests/unit/test_reflection_pipeline_py.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/learning/pattern_induction.py mini_ork/cli/reflect.py tests/unit/test_pattern_induction_py.py` → clean.
- `git diff --stat` touches only the files in scope.
