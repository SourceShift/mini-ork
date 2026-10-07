# "Needs you" tells the truth: one count, no stale gates, no stuck spinners

## Why (live, 2026-10-07)

The IDE runs sidebar showed the tile **"0 need you"** above a list section **"NEEDS YOU · 5"**.
All 5 were framework-edit runs whose Opus reviewer said needs_revision. They rolled back, and
the retry loop opened a `retry_precondition` gate ("The change was judged wrong — it needs a
revision"). The revision of every one of them had ALREADY been finished and merged (61545110,
90ce6ebe, a491b79c, 1895af19, dea58248), yet each kept asking the user for a fix. One also showed
its `rollback` node spinning for 1h55m although it had finished.

Three causes:

1. **Two definitions of "needs you".**
   - `mini_ork/acp/fleet.py:fleet_rows` counts tiles from the cheap
     `task_state.run_mark(status, run_dir)` (`mini_ork/acp/task_state.py:509`).
   - The list uses the precise `task_state(...)`, whose **rule 0** (:291-321) makes a
     failed/rolled_back run with a PENDING retry gate (`<run_dir>/retry-gate.json` →
     `retry_notify.pending_fix_for_run`) `needs_you`.
   - `run_mark` has no such rule, so it counts the run as failed.
2. **Gates outlive the work.**
   - Work on a framework-edit run happens in a task worktree (`run_profile.json` →
     `"target_repo": "/…/mini-ork-worktrees/<slug>"`).
   - When that worktree is merged to main (`scripts/mini_ork_worktree.py merge`) or removed
     (`clean`), every failed run that targeted it is settled. Its fix landed or was dropped.
   - Nothing closes their gates, so they ask forever.
3. **Same-second events race.**
   - `RunDetailRepository.fetch_node_lifecycle_events_bulk` (`mini_ork/web/repositories.py`
     ~:380) orders by `created_at ASC` only.
   - `created_at` has 1-second resolution. A node whose `node_start` and `node_end` share a
     second (rollback: 853 ms) can come back end-then-start.
   - `derive_node_statuses` then leaves it `running`.

## Files in scope (touch ONLY these)

- `mini_ork/acp/task_state.py`: ONLY `run_mark`
- `mini_ork/recovery/retry_notify.py`: one new public function (below); nothing else changes
- `scripts/mini_ork_worktree.py`: ONLY `merge_worktree` and `clean_worktree` (one call each)
- `mini_ork/web/repositories.py`: ONLY the ORDER BY of the lifecycle-events queries feeding
  `derive_node_statuses`, and `derive_node_statuses` itself
- `tests/unit/test_needs_you_truth.py` (new)

Do NOT modify any other file.

## Changes (exact)

1. **`run_mark`:** for `status in ("failed", "rolled_back")` with a `run_dir`, return
   `MARKS["needs_you"]` when `retry_notify.pending_fix_for_run(home, run_dir)` returns a dict.
   `home` is `run_dir.parent.parent`, as `task_state` rule 0 does.
   - The check is cheap: `pending_fix_for_run` returns at once when `retry-gate.json` is absent.
     Keep it that way: no DB read unless the pointer file exists.
   - Fail soft (`except Exception`).
   - Result: the tile count and the list agree.
2. **`retry_notify.supersede_gates_for_target(home, target_dir, note) -> list[str]`:**
   - For every `<home>/runs/*/retry-gate.json` (pointer present), read that run's
     `run_profile.json`. Match when `target_repo` OR `roots.target` resolves (realpath) to
     `target_dir`.
   - If the gate row is still pending, resolve it `rejected` with `note` via
     `oversight_inbox.resolve`. Write `"abandoned": true` and `"superseded": note` into the
     pointer file (the same marker `board gate reject` writes).
   - Returns the run ids closed. Never raises: a bad file is skipped.
3. **`merge_worktree`:** after the successful `push`, call
   `supersede_gates_for_target(<home>, wt, f"superseded: merged to main as {short_sha}")`.
   - `<home>` is `MINI_ORK_HOME` or `<ROOT>/.mini-ork`.
   - Print one line naming the closed run ids (nothing when none).
   - Wrap it in `try/except`: a gate problem must never fail a merge that already pushed.
4. **`clean_worktree`:** same call before removal, with note
   `"superseded: worktree <slug> removed"`. Same fail-soft rule.
5. **Ordering:** the lifecycle-event queries feeding `derive_node_statuses` order by
   `created_at ASC, rowid ASC` (or the table's id column).
   - In `derive_node_statuses`, a `node_start` never overwrites an entry that already has a
     `node_end` with `ended_at >= ` this start's `created_at`. A same-second end stays done,
     whatever the row order.
   - A real re-run (start strictly later than the end) still flips it to running.

## Tests (`tests/unit/test_needs_you_truth.py`, temp home + temp state.db)

- **Gate counts as needs you:** a `rolled_back` run with `retry-gate.json` pointing at a pending
  `retry_precondition` row → `run_mark` gives the needs_you mark. `fleet_rows` counts it under
  `needs_you`, and the row's state is `needs_you` (they agree).
- **Resolved or absent gate:** the gate row resolved, or no pointer → failed mark. No DB opened
  when the pointer is absent (monkeypatch `oversight_inbox.get` to raise if called).
- **Supersede:** `supersede_gates_for_target`
  - closes a pending gate whose run `target_repo` is the worktree path (including a
    symlinked/realpath-equal path);
  - leaves a gate for another target pending;
  - is idempotent on a second call (returns []);
  - the pointer file gets `abandoned: true`.
- **Merge:** `merge_worktree` with git/subprocess stubbed calls `supersede_gates_for_target`
  once after the push. An exception inside it does not fail the merge.
- **Same-second events:**
  - start and end with equal `created_at`, rows given end-first → `done`;
  - start-first → `done`;
  - a start strictly later than the end → `running`.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_needs_you_truth.py tests/unit/test_acp_task_state.py tests/unit/test_retry_notify.py tests/unit/test_web_repositories_py.py tests/unit/test_worktree_script_py.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/acp/task_state.py mini_ork/recovery/retry_notify.py scripts/mini_ork_worktree.py mini_ork/web/repositories.py tests/unit/test_needs_you_truth.py` → clean.
- **Live proof (read-only):** with the live home, `fleet_rows(Path(".mini-ork"))` counts and the
  number of rows with state `needs_you` among the shown rows agree. Paste both.
- `git diff --stat` touches only the files in scope.
