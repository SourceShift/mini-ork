# Learning & memory page — new tab structure, Overview outcomes, Bug reports moved

Plan: `docs/plans/2026-10-07-learning-memory-page-refactor.md`, phase P8 plus the outcomes half of
P5. Decisions already taken by the user: Bug reports move to Verify & safety; TraceOtter becomes a
section of Self-improve.

## Why

`mini_ork/ide_pages/learn.py` is one 357-line module with five tabs of raw table dumps
(`learnings`, `memory`, `improve`, `bugs`, `traceotter`). None answers the operator's first
question: is mini-ork getting better at my work? The data exists. `task_runs` over the last 4
weeks: `verified_artifact` 399 runs / 219 published / 28 failed; `code_fix` 191 / 82 / 96;
`framework_edit` 73 / 5 / 62. 214 runs have been stuck in `executing` for more than a day and would
corrupt any rate. Later phases rebuild Lessons and Memory in parallel, so each tab needs its own
module now.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/learn.py` → DELETE, replaced by the package `mini_ork/ide_pages/learn/`:
  `__init__.py`, `overview.py`, `lessons.py`, `memory.py`, `improve.py`, `_common.py`
- `mini_ork/ide_pages/verify.py`: ONLY add a `bugs` tab and its builder
- `tests/unit/test_ide_pages_learn.py`, `tests/unit/test_ide_pages_verify.py`

Do NOT modify any other file. `from mini_ork.ide_pages import learn` and `learn.build(home, tab,
args)` must keep working.

## Structure (exact)

- `learn/__init__.py`: `TITLE = "Learning & memory"`, `SUB = "Is mini-ork getting better at your
  work, what has it learned, and what needs you."`, `TABS = [("overview", "Overview"), ("lessons",
  "Lessons"), ("memory", "Memory"), ("improve", "Self-improve")]`. Default tab `overview`. `build()`
  dispatches to `<module>.sections(home, args, errors)`, which returns a list of sections; each
  module guards its own sections with `S.guarded`. Unknown old tab keys map: `learnings` →
  `lessons`; `traceotter` → `improve`; `bugs` → `overview`.
- `learn/_common.py`: `_db`, `_count`, `_short`, `_outcome_colour` (moved as-is).
- `learn/lessons.py`: today's Gradients, Patterns and Failure modes sections, moved unchanged. A
  later phase replaces them.
- `learn/memory.py`: today's Namespaces and Semantic memory lifecycle, moved unchanged.
- `learn/improve.py`: today's Loop, Candidate, Self-improve ledger and Health probes, plus the Idea
  tree (moved from Memory) and TraceOtter as one section titled `"Training data export ·
  TraceOtter"` (its flow + corpus + failure modes, keep the Ingest action).
- `verify.py`: append `("bugs", "Bug reports")` to `TABS`; move `_bugs` there unchanged,
  including the Sweep and Promote actions.

## Overview tab (new; `learn/overview.py`)

Usefulness contract. Every section answers one operator question, shows a denominator, links to
evidence, and says why it's empty when it is:

| Section | Question it answers | Denominator / baseline | Action / link |
|---|---|---|---|
| Needs you | "Is anything waiting on me?" | counts | each chip opens the place to act |
| Outcomes by task class | "Is mini-ork getting better at my work?" | terminal runs per window; previous 4 weeks | row → class detail |
| Class detail (when a class is selected) | "Why is this class failing?" | last 10 terminal runs | each run opens it |

1. **Needs you** (`S.chips`, full width). One chip per non-zero item:
   - open bug reports (`bug_reports.status='open'`) → `S.page_link("verify", "bugs")`
   - runs stuck in `executing` with `created_at` older than 24 h → `S.page_link("runs", None,
     filter="working")`
   - decaying memories (same rule as today's lifecycle) → `S.page_link("learn", "memory")`
   - promotions decided `quarantined` in the last 7 days → `S.page_link("learn", "improve")`
   If all are zero, a single `S.lst("Needs you", [S.ok("Nothing needs you")])`.
2. **Outcomes by task class** (`S.table`, full width; note: "Last 28 days vs the 28 before.
   Pass = published; fail = failed. Runs still executing after 24 h are counted as stuck, not as
   failures.").
   - Columns: `class | runs | pass rate | Δ pass | cost / pass | Δ cost | stuck`.
   - Terminal = `published` or `failed` (also accept `completed` / `success` as pass and
     `rolled_back` / `error` as fail if present). Cost from `task_runs.cost_usd`; cost / pass =
     window cost ÷ passes.
   - Show a rate only with ≥ 5 terminal runs in that window, else `"— (n<5)"`.
   - Δ in percentage points, coloured: green if pass rate is up ≥ 5 pts or cost / pass down ≥ 15%,
     red for the reverse, else muted.
   - Top 10 classes by runs in the current window. Each row:
     `"do": S.set_args(cls=<task_class>)`, `"sel"` when it's the selected class.
   - Empty state: "No finished runs in the last 56 days."
3. **Class detail**, only when `args["cls"]` is set (`S.table`, title `f"{cls} · last 10
   finished runs"`, full width): the last 10 terminal runs of that class, newest first:
   `status | run | cost | age | failure`. `failure` = `failure_memory.failure_category` (+
   `workflow_stage`) for that run, else `task_runs.verdict`. The row `do` is
   `S.open_run(run_id, title)`.

All SQL is bound-parameter; time windows use `int(time.time())` passed in (tests freeze it).

## Tests

- `tests/unit/test_ide_pages_learn.py`: update for the new tabs, default `overview`, and old keys
  mapping. Overview with seeded `task_runs` across both windows: rates, Δ pts, cost / pass, the
  `n<5` rule, stuck runs counted separately and NOT in the rate, `cls` selection → detail table
  with failure categories, and empty states. Needs-you chips appear only when non-zero, with the
  links above. Lessons / Memory / Self-improve still render the moved sections; Self-improve has
  the TraceOtter and Idea tree sections.
- `tests/unit/test_ide_pages_verify.py`: the `bugs` tab renders the Bug reports table and its two
  actions.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_learn.py tests/unit/test_ide_pages_verify.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/ide_pages/learn mini_ork/ide_pages/verify.py tests/unit/test_ide_pages_learn.py tests/unit/test_ide_pages_verify.py` → clean.
- Read-only proof on the live DB: `python3.11 -m mini_ork.cli.main board page learn --tab overview`
  (or `bin/mini-ork board page learn --tab overview`) and the same with `--arg cls=framework_edit`.
  Paste both JSONs (trimmed to the sections). The outcomes table must show `framework_edit` with
  its low pass rate, and the detail table its recent failures.
- `git diff --stat` touches only the files in scope.
