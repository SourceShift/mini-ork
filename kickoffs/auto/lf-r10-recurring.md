# LF-R10 "Recurring problems" means recurring across kickoffs

## Goal

The "Your code" tab's `recurring()` (`mini_ork/ide_pages/learn/code.py`) counts
cluster members and distinct `run_id`s, so it calls things "recurring" that
happened once: for area `mini_ork/ide_pages`, 4 of the 5 listed clusters came
from ONE kickoff each — "4× in 1 run" is one review restating one bug four
ways, and "2 runs" are review-loop rounds of the same kickoff
(`ide-node-stream-r3`/`-r4`, `ide-kickoff-search-r4`/`-r5`). Across all areas 60
of 99 listed clusters are singletons. `context_v2.kickoff_stem()` already maps
round kickoffs (`foo-r3.md`, `foo-r4.md`) to one identity and is unused here.
Decision record: `docs/plans/2026-10-15-learning-loop-fixes.md` (R10).

All paths are relative to the repository root.

## Mechanism (exact)

1. `_select_findings` also returns each finding's `kickoff_path` (it already
   joins `task_runs`; NULL when the run has no row).
2. In `recurring()`, each member gets a **stem**:
   `context_v2.kickoff_stem(kickoff_path)`, or the member's `run_id` when the
   stem is `""` (generic kickoff names) — so unrelated generic runs never merge.
3. Each cluster additionally reports:
   - `kickoffs`: number of distinct stems;
   - `occurrences`: number of distinct `(stem, file)` pairs — a restatement in the
     same kickoff and file counts once (`n` becomes this value; the raw member
     count moves to `n_members`);
   - `files`: distinct member files (the existing top-3 list stays for display);
   - `mixed`: True when the cluster spans ≥ 2 stems but no pair of members from
     DIFFERENT stems has cosine ≥ the cluster threshold (`_SIM`) — the stems are
     linked only through the centroid, so it is probably two defects.
4. A cluster is **recurring** only when `kickoffs >= 2` and `occurrences >= 2`.
   `recurring()` returns only recurring clusters (sorted by kickoffs, then
   occurrences, then severity). Add `seen_once(findings, top=10)` returning the
   other clusters, same shape.
5. The area detail renders the "Recurring problems" table with columns
   `problem`, `kickoffs`, `files`, `worst`, `action` (a `mixed` cluster shows
   `2 (mixed)` in the kickoffs cell), and below it a "Seen in one kickoff" list
   of `seen_once` items, each with its kickoff stem — shown, not hidden, and
   without a rule button. The "Make it a rule" action is offered only for
   recurring clusters (R4's target rules and 2-member floor are unchanged).
   The areas table's "recurring problems" cell uses recurring clusters only.

## Files in scope

- `mini_ork/ide_pages/learn/code.py`
- `tests/unit/test_ide_pages_learn_code.py`

## Tests (`tests/unit/test_ide_pages_learn_code.py`, hermetic findings dicts)

- four restatements of one bug in one run/kickoff and one file → not recurring;
  appears in `seen_once` with `occurrences == 1`;
- the same bug in `ide-node-stream-r3.md` and `ide-node-stream-r4.md` (one stem)
  → not recurring;
- the same bug in kickoffs `node-overview.md` and `ide-node-stream.md`
  (two stems) → recurring with `kickoffs == 2`;
- two generic kickoffs (`kickoff.md`) in different runs bucket by run id
  (two stems), and findings from runs with no `task_runs` row are kept;
- a cluster whose cross-stem members are each below `_SIM` of one another is
  `mixed`;
- the rendered area detail has the new columns, a "Seen in one kickoff" section,
  and no "Make it a rule" action on a `seen_once` item.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_ide_pages_learn_code.py tests/unit/test_ide_pages_learn.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_ide_pages_learn_code.py tests/unit/test_ide_pages_learn.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r10-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r10-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change `context_v2.py` (import
`kickoff_stem` only), `code_findings.py`, `preferences.py`, or R4's rule-target
logic, and do not write to the live DB under
`/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
