# A kickoff bullet that wraps onto a second line stays one item; the run hero doesn't repeat the title

## Why (live, 2026-10-08)

The run page's "What this run is for" hero lists the run's success criteria from
`run_profile.json`. For run `ide-orca-b2b-story-20261008113340` it showed one criterion split
in two:

```
'`script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings from'
'`crates/mini_ork_ui`.'
```

The kickoff bullet wrapped onto an indented continuation line:

```
- `script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings from
  `crates/mini_ork_ui`.
```

**Root cause.** `mini_ork/cli/main.py` `gen_profile` → the inner `bullets(lines)` makes every
non-empty line its own item. The same helper builds `scope_allow` and `scope_deny`, so a wrapped
"Files in scope" bullet is split the same way.

**Second problem.** The hero's goal line is `user_goal`, which is the kickoff's first heading.
That is also the run page title (spec v2), so the hero repeats the title word for word.

## Files in scope (touch ONLY these)

- `mini_ork/cli/main.py`: ONLY the inner `bullets(lines)` helper of `gen_profile` (~:289-295)
- `mini_ork/ide_pages/run.py`: ONLY `_story_tab`'s hero block (~:1357-1371)
- `tests/unit/test_run_profile_bullets.py` (new)

Do NOT touch any other file.

## Changes (exact)

1. **`bullets(lines)`** keeps its signature and output type (a list of str). A line starts a NEW
   item when either:
   - it is a list item: `^\s*[-*]\s+` or `^\s*\d+[.)]\s+` (strip the marker as today; keep the
     number for numbered items as today, i.e. only strip `-`/`*` markers); or
   - it is not indented (no leading whitespace).

   An INDENTED line that is not a list item, after at least one item, is a continuation: append
   it to the previous item with one space (`prev + " " + line.strip()`).

   Nested bullets (indented `- …`) stay separate items, as today.
2. **`_story_tab` hero:** when `goal` equals the page title, pass `goal=""` to `S.hero` so the
   hero shows only the criteria.
   - Compare case-insensitively, ignoring surrounding whitespace.
   - The page title is the run's kickoff title: the same value `build` uses for the v2 page
     title.
   - When both the goal is blanked AND there are no criteria, omit the hero entirely.

## Tests (`tests/unit/test_run_profile_bullets.py`)

- `gen_profile` on a temp kickoff whose "Done when" section has:
  - a two-line wrapped bullet;
  - a plain one-line bullet;
  - a nested indented `- sub` bullet.

  → `success_criteria` has 3 items. The wrapped one is joined with a single space and contains
  both halves.
- **Scope.** A wrapped bullet under "Files in scope" → one `scope_allow` entry that contains both
  backticked paths.
- **Existing behaviour.** Unchanged for one-line bullets: `"- a"`, `"- b"` → `["a", "b"]`.
- **Hero.** A run whose `user_goal` equals its title → the hero section has an empty goal and
  still lists the criteria. Build it with the run-page test helpers in
  `tests/unit/test_ide_pages_run.py` (copy the fixture pattern; do not import private test
  helpers across files).

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_run_profile_bullets.py tests/unit/test_ide_pages_run.py tests/unit/test_profile_gate_py.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/cli/main.py mini_ork/ide_pages/run.py tests/unit/test_run_profile_bullets.py` → clean.
- `git diff --stat` touches only the files in scope.
