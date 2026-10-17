# LF-R14 the file-reversion guard must not heal a rebase

## Goal

`.githooks/post-commit` spawns a watchdog that restores a just-committed file
when its working-tree blob equals one of the commit's PARENT blobs
(`.githooks/post-commit:132-152`). `git rebase origin/main` produces exactly
that state: the rebase checks out the new base, whose content for the files the
commit changed IS the parent's content (the commit is the delta). The watchdog
then writes HEAD's (pre-rebase) content back into the working tree, so the tree
no longer matches the rebase's checkout, and the next `pick` aborts:

    error: Your local changes to the following files would be overwritten by merge:
    error: The following untracked working tree files would be overwritten by merge:
    hint: Could not execute the todo command … git rebase --continue

Two witnesses on 2026-10-16, both wedging `make worktree-merge`
(`scripts/mini-ork-worktree.sh` runs `git -C "$wt" rebase origin/main`):

- `lf-r5`: `.mini-ork/file-reversion-guard.log` rows
  `2026-10-16T21:15:11Z  eced5db3…  mini_ork/context_v2.py  restored-reverted-to-2e8367f7…`
  (also `learning/code_findings.py`, `tests/unit/test_code_findings.py`, and
  `reflect.py` restored 8 times at 5 s intervals).
- `lf-r2`: same shape for `71ff42ae…`, migration + `context_assembler.py` +
  `reflection_pipeline.py`.

The workaround used to ship both was `MO_REVERSION_GUARD_DISABLED=1` plus a
re-run after the 60 s window — the guard's own feature is what made the merge
flow unreliable.

All paths are relative to the repository root.

## Mechanism (exact)

1. The watchdog's stated premise is "HEAD is intact, the working tree was
   reverted". Make that premise a precondition instead of an assumption: before
   restoring anything, require the watched commit to still be HEAD
   (`git -C <repo> rev-parse HEAD` equals the `sha` the watchdog was spawned
   with). During a rebase, `git am`, a cherry-pick or a merge, HEAD is that
   operation's own commit, not `sha`, so **no restore may happen** — the drifted
   blob is the operation's legitimate checkout, not a reversion.
2. Also skip while an operation is in progress, detected worktree-safely via
   `git rev-parse --git-path rebase-merge` / `rebase-apply` / `MERGE_HEAD`
   (the path is per-worktree; a `Path(...).exists()` check on the returned
   path). Both guards ship: (1) is the semantic one and also covers a
   cherry-pick, (2) stops the watchdog from fighting the sequencer even if HEAD
   is momentarily the watched commit.
3. Everything else is unchanged: while HEAD is still `sha` and no operation is
   in progress, a file whose blob equals a parent's blob is still restored from
   HEAD and logged, exactly as today. This is the case the hook exists for (a
   racing dispatcher reverting committed files), and it must keep working.
4. A skip is NOT a recovery: append no row to the guard log for it (the log is
   a recovery ledger, and a rebase is routine), but do record the reason in the
   hook's header comment so the behaviour is discoverable. Keep the existing env
   knobs (`MO_REVERSION_GUARD_DISABLED`, `_WATCH_S`, `_POLL_S`) working, and
   keep `MO_REVERSION_GUARD_DISABLED=1` skipping the watchdog entirely.

## Files in scope

- `.githooks/post-commit`
- `tests/unit/test_githooks_py.py`

## Tests (`tests/unit/test_githooks_py.py`, hermetic temp git repos)

Follow the shape of the file's existing tests for this hook (they build a temp
repo, commit, then invoke the watchdog in its `__watchdog__` argv mode with a
short `MO_REVERSION_GUARD_WATCH_S`/`_POLL_S`).

- **a rebase is not healed**: commit A, then commit B changing `f`; check out A
  detached and write `f` to A's content (what a rebase checkout produces); run
  the watchdog for `B`'s sha; `f` still holds A's content — no restore, no new
  row in the guard log;
- **a real reversion is still healed**: HEAD is B, `f` is written back to A's
  content, no operation in progress; the watchdog restores B's content and logs
  `restored-reverted-to-<A>` — the pre-existing behaviour, unchanged;
- **an in-progress rebase is detected**: with a real `git rebase` stopped
  mid-way (e.g. a `rebase-merge` directory present, or a conflict left in
  place), the watchdog skips even when HEAD equals the watched sha;
- **a linked worktree is detected**, not just the main checkout: run the
  reversion case inside `git worktree add`ed tree and assert the guard still
  fires (its `--git-path` lookup is per-worktree).

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_githooks_py.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_githooks_py.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r14-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r14-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change the guard's log row format for a
recovery, do not remove the env knobs, and do not write to the live DB under
`/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
