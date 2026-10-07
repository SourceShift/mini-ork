# Rules that follow your code — path-scoped preferences + `mini-ork prefs preview`

Plan: `docs/plans/2026-10-07-learning-memory-page-refactor.md`, engineer-first redesign, step E2.

## Why

An engineer wants to say "when you touch `mini_ork/ide_pages/**`, run `tests/unit/test_ide_pages_*.py`
and keep docstrings in sync". They want that rule given ONLY to runs that touch those files, and
they want to see exactly what the agents will be told before starting a task. Today:

- `mini-ork prefs` (merged) supports scopes `global | task_class | workflow` only. The table
  `user_preference_memory` enforces it with `CHECK (scope IN ('global','task_class','workflow'))`;
  PK `(user_id, preference_key, scope, scope_target)`, 0 rows live.
- `_learned_block` (`mini_ork/cli/execute.py`) injects `prefs_for(task_class)` first in every
  researcher / implementer / reviewer prompt. It has no notion of which files the run touches.
- A run's file scope is known: `runs/<id>/run_profile.json` → `scope_allow` is a list of the
  kickoff's "Files in scope" bullet lines, e.g.
  ``"`mini_ork/learning/themes.py`, `tests/unit/test_themes.py`"`` or
  ``"`mini_ork/cli/reflect.py` (only the themes block, if a signature changes)"``. The paths sit
  inside backticks.
- There is no way to preview the learned block for a task.

## Files in scope (touch ONLY these)

- `db/migrations/0065_preference_path_scope.sql` (new)
- `mini_ork/memory/preferences.py`
- `mini_ork/cli/prefs_cmd.py`
- `mini_ork/cli/execute.py`: ONLY `_learned_block` (pass the run's scope paths to `prefs_for`)
- `tests/unit/test_prefs_path_scope.py` (new)

Do NOT modify any other file.

## Changes (exact)

1. **Migration 0065.** Rebuild `user_preference_memory` with
   `CHECK (scope IN ('global','task_class','workflow','path'))`: create a new table with the
   identical columns and PK, copy the rows, drop the old table, rename. Wrap it in a transaction.
   `preferences.ensure_schema` must apply the same rebuild idempotently when it detects the old
   CHECK (inspect `sqlite_master.sql`), so an unmigrated DB still accepts path rules.
2. **`set_pref(..., scope="path", target="<glob>")`.** Glob syntax: `**` matches any number of
   directories, `*` matches within one segment, a trailing `/` means the directory and everything
   under it. Validate the glob is non-empty and relative (no leading `/`, no `..`).
3. **`scope_paths(run_dir) -> list[str]`.** Read `run_profile.json` → `scope_allow`, extract every
   backticked token that looks like a path (contains `/` or a `.ext`), and normalize (strip `./`).
   Missing file → `[]`.
4. **`prefs_for(task_class, workflow="", *, paths=None, db=None)`.** Path rules are included only
   when `paths` is given and the glob matches at least one path. Order: global, then task_class,
   then workflow, then path, each by `set_at`. Keep the existing caps (12 entries, 600 chars).
   `render_block` shows the scope tag `[scope: path=<glob>]`.
5. **`_learned_block`.** Compute `paths = scope_paths(<this run's run dir>)` (the dir already in
   use there; else `MINI_ORK_RUN_DIR`) and pass `paths=` to `prefs_for`. Source dicts for path
   rules: `id = f"pref:path:{glob}:{key}"`. Nothing else changes.
6. **`mini-ork prefs preview <kickoff.md> [--task-class X] [--node implementer|researcher|reviewer] [--json]`.**
   Print EXACTLY the learned block a node of that type would get for that kickoff, with no
   steering (that's run-specific, and `fetch_for` would consume it):
   - paths = the backticked paths in the kickoff's `## Files in scope` section;
   - task class = `--task-class`, else a `task_class:` line in the kickoff front matter, else
     `"framework_edit"`;
   - assemble with the same functions `_learned_block` uses (`prefs_for(..., paths=)`, then
     `context_assembler.failure_modes_md(task_class, 5, node_type=node)`), in the same order and
     with the same headers.
   `--json` returns `{"task_class", "node", "paths", "block", "sources"}`. Read-only: it must not
   write the retrieval ledger. Pass whatever flag/env `failure_modes_md` / `semantic_lessons_md`
   use to skip `record_retrievals` (`MINI_ORK_RUN_ID` unset), and say in the docstring why.
7. `prefs list` shows path rules with their glob.

## Tests (`tests/unit/test_prefs_path_scope.py`, temp home + DB)

- Migration / `ensure_schema` on a DB with the old CHECK: an existing global row survives, and a
  path rule can then be inserted.
- Glob matching: `mini_ork/ide_pages/**` matches `mini_ork/ide_pages/learn/memory.py`, not
  `mini_ork/cli/x.py`. `tests/unit/test_*.py` matches one segment only. Trailing `/` works.
- `scope_paths` on the two live `scope_allow` shapes quoted in Why → the right paths.
- `prefs_for(paths=…)` includes a matching path rule and excludes a non-matching one; ordering.
- `_learned_block` for a run dir whose `run_profile.json` scopes `mini_ork/ide_pages/node.py`
  carries the path rule; for a run scoped elsewhere it doesn't.
- `prefs preview` on a temp kickoff with a "Files in scope" section prints the path rule and the
  failure-mode header, and writes no retrieval rows.
- `set --scope path --target /abs/path` → exit 2.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_prefs_path_scope.py tests/unit/test_prefs.py tests/unit/test_learned_record.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/memory/preferences.py mini_ork/cli/prefs_cmd.py mini_ork/cli/execute.py tests/unit/test_prefs_path_scope.py` → clean.
- Proof in a THROWAWAY home (copy the live DB with `sqlite3 backup` into a temp dir; never write
  the live DB):
  - `prefs set ide-tests "After editing ide_pages, run tests/unit/test_ide_pages_*.py" --scope path --target 'mini_ork/ide_pages/**'`
  - `prefs preview /Volumes/docker-ssd/ps/mini-ork-worktrees/eng-path-rules/kickoffs/auto/eng-path-rules.md --node implementer`
    → the path rule must NOT appear (this kickoff doesn't touch ide_pages)
  - preview a temp kickoff that scopes `mini_ork/ide_pages/node.py` → it appears.
  Paste both outputs, then delete the temp home.
- `git diff --stat` touches only the files in scope.
