# Overview additions — revision 2 (Opus review of run learn-overview2-20261007133743)

WIP commit 338d850e holds revision 1 (30 passed, ruff clean). Opus returned `needs_revision`. Fix
ONLY these. Keep everything else, including the accepted choices: the retry flag inside the
failure cell, and the muted follow-up row instead of `sub`.

## Files in scope

- `mini_ork/ide_pages/learn/overview.py`
- `tests/unit/test_ide_pages_learn_overview.py`

Do NOT modify any other file.

## Fixes (exact)

1. **Recent learning events: newest first, 12 total** (`overview.py:521-537`). Today it's up to
   12 per kind (48 total), grouped by kind. Collect `(ts_epoch, item)` across all kinds. Convert
   ISO `decided_at` (and any other ISO timestamp) to epoch. Sort descending and keep the first 12.
2. **"no reason recorded" fallback** (`overview.py:345`). `str(verdict or "—")` still shows `—`
   for a failed run, which is the bug this phase exists to fix. The chain is: retry_hint summary →
   failure_memory category (+ stage) → verdict → `"no reason recorded"`. A failed run never shows
   `—`.
3. **Tests that test what they claim:**
   - `test_recent_learning_events_tolerate_missing_tables` (~:367) must actually DROP the
     `promotion_records`, `semantic_memory` and `bug_reports` tables, then render.
   - The events test asserts global newest-first order across kinds: seed interleaved timestamps
     and check the exact title order.
   - The stalled-chip test runs the REAL path. Seed `learning_pass_stats` with 3 zero-output
     passes and call the page, with no monkeypatch of `_stalled_stage_count`.
   - Add a test: a failed run with no hint, no failure_memory row and no verdict →
     `"no reason recorded"`.
4. **Comments match the code:** the `_HINT_CAP` comment (`:19-24`) and the
   `_stalled_stage_count` docstring (`:119`).

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_learn_overview.py tests/unit/test_ide_pages_learn.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/ide_pages/learn/overview.py tests/unit/test_ide_pages_learn_overview.py` → clean.
- Live proof: `build_page(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "learn", "overview", {"cls": "framework_edit"})`.
  No failed row shows `—` as its reason, and events are ≤ 12 and newest first. Paste it.
- `git diff 338d850e --stat` touches only the files in scope.
