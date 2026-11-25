# framework-edit runs branch from the primary's stale HEAD, not origin/main

Status: **open**
Opened: 2026-11-25
Found by: landing `run-1795604701-80144` (memory backend seam S1) by hand

## 1. A run's workspace base is the primary checkout's HEAD

`workspaces.create` branches from whatever the launching clone has checked out.
The primary here was 48 behind `origin/main`, so the S1 run branched from
`2fcd5584` and re-created `docs/architecture/memory-backend.md` as a *new* file
even though that doc was already merged upstream. Applying the run's diff to a
worktree based on current `origin/main` failed with:

```
error: docs/architecture/memory-backend.md: already exists in working directory
```

Nothing in the run is wrong there — the run faithfully rendered the tree it was
given. The defect is that a run started against a stale checkout silently
duplicates merged work, and the only signal is a diff that cannot apply.

Suggested: `workspaces.create` should record its base commit in the run dir, and
`mini-ork run` should warn (or refuse without `--allow-stale-base`) when the
checkout is **behind** — `git rev-list HEAD..origin/main` non-empty, i.e.
`origin/main` is not an ancestor of HEAD. Note the direction: HEAD being *not an
ancestor of* `origin/main` means local commits exist upstream does not have,
which is a different (and usually harmless) condition. That turns a confusing
apply-time failure into a one-line message at launch.

## 2. Excluded hunk: `bin/mini-ork` engine-root resolution in a linked worktree

The same run also proposed a launcher change, deliberately left out of the S1
merge as out-of-scope (it lives in the engine-selection area listed as a footgun
in the operator notes, and nothing about the memory seam depends on it):

- When the launcher file itself sits in a linked worktree, an inherited
  `MINI_ORK_ENGINE_ROOT` / `MINI_ORK_ROOT` that names *that worktree's own main
  checkout* is now ignored, and the worktree's own code is used instead.
- Rationale in the run's own comment: `framework-edit` executes
  `<worktree>/bin/mini-ork` to reach the worktree's new CLI subcommands, but the
  inherited pointer dispatched those calls to the main checkout, so the change
  under test was invisible.
- The hunk also reorders `ENGINE_ROOT = _configure_paths()` to run *after*
  `_main_checkout_equivalent` is defined, and moves the `install` fast path with
  it.

Status: not started. Remaining: re-apply that hunk on current `main` as its own
change, with a test that a launcher inside a linked worktree picks the worktree
when the env pointer targets the worktree's main checkout and still honours the
pointer when it names a genuinely different engine.

The hunk is preserved verbatim in `docs/todos/20261125-engine-root-hunk.diff`
(74 lines, four hunks: the `own_root` branch, the moved `ENGINE_ROOT` bootstrap,
the moved `install` fast path, and the comment above the late resolution). It
was extracted from the run's `framework-edit.diff` because `.mini-ork/runs/` is
gitignored and pruned — a pointer to that path would have rotted.
