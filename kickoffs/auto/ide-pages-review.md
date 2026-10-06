# Review: `board page` — the mini-ork IDE pages, before they merge

## Problem

Commit `3b291826` on branch `wt/ide-pages` (worktree
`/Volumes/docker-ssd/ps/mini-ork-worktrees/ide-pages`) adds `mini-ork board page
<key>`: twelve modules under `mini_ork/ide_pages/` that build every page of the
mini-ork IDE (a Zed build) as JSON, plus a `header` block in `board --json`. The
IDE renders the JSON with one generic renderer and polls `board --json` every 5 s.
The code was written quickly by four parallel authors against one contract
(`mini_ork/ide_pages/spec.py`). Review it before it reaches main.

Read-only review: no code changes. Every finding needs a `file:line` anchor in the
worktree path above and a concrete failure (input → wrong output / crash / leak).

## Files in scope (all under the worktree)

- `mini_ork/ide_pages/spec.py` — the contract (shapes, actions, colours)
- `mini_ork/ide_pages/__init__.py`, `header.py`
- `mini_ork/ide_pages/{run,runs,changes,verify,recipes,autos,lanes,learn,orch,context,nodes,setup}.py`
- `mini_ork/cli/board_cmd.py` — the `page` verb and `header`
- `tests/unit/test_ide_pages_*.py`

## What to look for

1. **Invented or wrong numbers.** The rule is real data only; an empty source must
   show an honest empty state. Flag any count, cost, rate or state that is guessed,
   mislabelled, from the wrong table/column, or double-counted (e.g. cost joins that
   multiply rows, ms-vs-s timestamps, 24 h windows that are really "all time").
2. **Secrets.** Any path that can put a key value, token or credential-bearing
   command line into the page JSON.
3. **Actions.** Every `cli(...)` action runs `mini-ork <args> --home <home>` from the
   IDE: flag subcommands that do not exist, do not accept `--home`, are destructive
   without `confirm`, or start long/expensive work from one click.
4. **Speed and load.** Each page must build in < 1.5 s on a home with 1,300 runs;
   `board --json` (polled every 5 s) measured 3.5–7.6 s. Find the expensive calls
   (eager imports, YAML parsing, unbounded queries, subprocesses, network) and how
   to cut them.
5. **Robustness.** Sections must fail alone (`spec.guarded`); flag code outside a
   guard that can raise on an old/partial `state.db`, a missing run dir, or odd YAML.
6. **Contract drift.** Shapes that do not match `spec.py` (wrong keys, colours that
   are not in `COLOURS`/`fam:<lane>`, actions with unknown keys).
7. **Tests.** What the 84 tests do not cover that would catch the above.

## Definition of done

Five lens reports and a synthesis ranking findings by severity × leverage / effort,
each with `file:line`, the failure, and the smallest fix. Consensus markers for
findings raised by 2+ lenses. Read-only: the synthesis is published by the recipe;
no source files change.
