# Overview — why a class fails, one-click reap for stuck runs, learning-loop health, recent learning events

Plan: `docs/plans/2026-10-07-learning-memory-page-refactor.md`, phase P5b. Read its "Usefulness
contract". Every section must fill all of its columns, and the reviewer must reject any that
doesn't.

## Why

The new Overview (`mini_ork/ide_pages/learn/overview.py`, merged today) answers "is it getting
better?" per task class. Three gaps remain, seen on the live DB:

1. **The class detail can't say why runs failed.** For `framework_edit`, the failed run
   `node-commands-20261007114301` shows `—` in the failure column, because it has no
   `failure_memory` row and no verdict. `mini_ork.recovery.retry_hint.load_or_compute(home,
   run_id, write=False)` already explains it: `failed_node="reviewer"`,
   `needs_change={"kind": "code", "summary": "The change was judged wrong — it needs a
   revision", "detail": …}`.
2. **"214 runs stuck in executing > 24h"** links to the runs list, but the fix is `mini-ork reap`
   (`mini_ork/orchestration/run_reaper.py`; `--dry-run`, `--stale-after 6h`, `--json`). There's
   no button for it.
3. **Nothing shows whether the learning loop itself works.** Pattern induction wrote 0 lessons for
   weeks (dead lane), and today every reflect LLM call outside a capped launch was refused by the
   $50 daily cost circuit. Nobody could see either. `mini_ork.learning.ledger.stage_health()`
   (merged) returns per-stage rows from `learning_pass_stats` with an `alarm` flag and
   `last_error`. Reflect starts writing those rows in a parallel phase. Until then the table is
   empty and the section must say so honestly.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/learn/overview.py`
- `tests/unit/test_ide_pages_learn_overview.py` (new)

Do NOT modify any other file.

## Changes (exact)

1. **Class detail failure column.** For each failed run, call `retry_hint.load_or_compute(home,
   run_id, write=False)`. If it returns a hint, the cell is
   `f"{failed_node}: {needs_change['summary']}"`[:110], and the row gets a second action via a new
   last column `retry` showing `"retryable"` (green) or `"no"` (muted) from `hint["retryable"]`.
   Fall back to today's failure_memory / verdict chain, then `"no reason recorded"`. Cap at 10 hint
   computations per render. Any exception from retry_hint → the fallback, never a page error.
2. **Stuck runs chip → reap.** Keep the chip, and add a section action button on "Needs you" when
   stuck > 0: `S.btn("Reap stuck runs", S.cli("reap", "--stale-after", "24h", confirm=f"Mark the
   {n} run(s) executing for more than 24 h as failed? Their files stay."), "warn")`. The chip
   still links to the runs page.
3. **New section "Learning loop health"** (`S.table`, full width), placed after the outcomes
   table. Rows from `ledger.stage_health(window_passes=3)`: `stage | last pass | in → out |
   failures | lane | state`.
   - `state` = `"⚠ produced nothing for 3 passes"` (red) when `alarm`; `"failing"` (yellow) when
     the newest row has failures > 0; else `"ok"` (green).
   - When `last_error` is set, add it as a row `sub` (muted, [:160]). A `last_error` containing
     `cost_circuit_open` gets the extra text: `"daily budget reached — raise MO_DAILY_BUDGET_USD
     or wait"`.
   - Note: "What each learning stage did in its last 3 reflect passes. A stage that keeps
     producing nothing raises an alarm here."
   - Empty state (no rows / no table): one `S.lst("Learning loop health", [S.dot("Reflect has not
     reported yet", "Stages start reporting after the next reflect pass.")])`.
   - Any alarm also adds a chip to "Needs you": `f"{k} learning stage(s) stalled"` →
     `S.set_args()` (stays on Overview).
4. **New section "Recent learning events"** (`S.lst`, full width), last 14 days, newest first, at
   most 12. Tolerate any missing table or column:
   - `emergent_patterns` approved (`resolved_at`): `S.ok(f"Pattern approved: {lesson_text or
     cluster_label}"[:140], f"{date} · pattern {id}")`
   - `semantic_memory` retired (`retired_at`): `S.warn(f"Memory retired: {text}"[:140],
     f"{date} · {retire_reason}")`
   - `promotion_records` (`decided_at`): `S.item(f"Promotion {decision}: {candidate_id}",
     f"{date} · utility {before} → {after}")`, coloured with `_outcome_colour`
   - `bug_reports` with `agent_role='learning'` (`first_seen_at`): `S.bad(f"mini-ork issue:
     {title}"[:140], f"{date} · seen {frequency}×")`, action → `S.page_link("verify", "bugs")`
   Empty: `S.dot("No learning events in 14 days", "Approvals, retirements and promotions appear
   here.")`.

Keep everything else in the module unchanged.

## Tests (`tests/unit/test_ide_pages_learn_overview.py`, temp home + DB, frozen time; monkeypatch `retry_hint.load_or_compute`)

- A failed run with a hint → `reviewer: The change was judged wrong…` and `retryable` / `no`;
  without a hint → the fallback chain; a raising hint → the fallback, no page error.
- The reap button appears only with stuck > 0, with the exact cli args and a confirm.
- Health: seed `learning_pass_stats` with 3 zero-output passes for `pattern_induction` and a
  `cost_circuit_open` error → the alarm state, the budget text in the sub, and a "stalled" chip in
  Needs you. A healthy stage → `ok`. No table → the empty state.
- Events: one of each kind within 14 days appears in order; one older than 14 days doesn't;
  missing tables are fine.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_learn_overview.py tests/unit/test_ide_pages_learn.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/ide_pages/learn/overview.py tests/unit/test_ide_pages_learn_overview.py` → clean.
- Read-only proof on the live DB: `build_page(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"),
  "learn", "overview", {"cls": "framework_edit"})`, printed as JSON. The failed `node-commands`
  run shows a reason, and the health section shows its empty state or real rows. Paste it.
- `git diff --stat` touches only the files in scope.
