# remote-nodes-07 — Target tree sync via git snapshots

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
**D1** (the local tree is the truth, the remote tree is a replica) and the
"Sync protocol" section. This is the epic that gives remote nodes "access to
the code we're targeting". Depends on epic 01 (pinned target root) and epic 06
(remote backend).

## Goal

Before a remote spawn or exec, the remote replica at `/workspace/target`
matches the local target checkout exactly. That includes uncommitted and
untracked non-ignored files, minus secret-named files. After a remote **agent
spawn**, the agent's edits land in the local checkout as uncommitted changes,
exactly as if the agent had edited locally. Git computes every diff and
integrity is verified on both sides. A conflicting local edit is never
clobbered.

## Why

- The executor runs about 20 git operations on the local target around every
  agentic node: the baseline `git stash create` and `update-ref`
  (`cli/execute.py:1482-1540`), the ground-truth harvest (`:1543-1684`),
  `implementer-summary.json` (`:1761-1821`), `review-diff.patch` (`:2012`),
  rollback (`cli/execute_handlers.py:1077-1262`) and the publisher commit
  (`cli/publisher.py:124-130`). D1 keeps all of them unchanged by syncing the
  tree at the boundaries instead of remoting each call.
- With no edits arriving locally, `code-fix` fails with `impl_no_changes`
  (`execute_handlers.py:803-811`).
- Nothing in `mini_ork/` clones, bundles or uploads a repo today (searches for
  `git clone|git bundle` found nothing). The sandbox only bind-mounts a local
  dir (`runtime/backends/docker.py:124-125`).

## Requirements

1. `mini_ork/remote/tree_sync.py` holds pure git logic with no HTTP. It is
   unit-testable on two temp repos.
   - `snapshot(repo, *, parent, excludes) -> Snap(commit, tree, excluded:
     list[str])`. Uses a temp `GIT_INDEX_FILE`, then `git add -A` (respects
     `.gitignore`), excludes via `:(exclude)` pathspecs, `git write-tree`,
     `git commit-tree -p <parent>`. The user's index, HEAD and stash are never
     touched (assert in tests).
   - Default excludes: `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`,
     `id_ed25519*`, `*.tfvars`, `secrets.local.sh`, `.mini-ork/state.db*`.
     Extend with `MO_REMOTE_SYNC_EXCLUDE` (comma-separated). Excluded *names*
     are reported in a `remote.sync.excluded` event, never their contents.
   - `initial_bundle(repo, snap) -> (path, mode)` follows the ladder `full` →
     `branch` → `squashed`. Each step is tried in order; a bundle over
     `MO_REMOTE_BUNDLE_MAX_MB` (default 100) falls to the next step. If the
     squashed bundle is still too big, raise `SyncTooLargeError`.
   - `incremental_bundle(repo, new, base)` → `git bundle create - new ^base`.
   - `materialize(remote_repo, bundle, snap, head)`: fetch the bundle into
     `refs/mo/sync/*`, then `git checkout --detach <head>`. Make the worktree
     equal `snap.tree`: restore from the snapshot, and remove files present
     in HEAD but absent in the snapshot. Then `git reset -q` so the index
     equals HEAD and untracked files stay untracked. Afterwards the remote
     `git status --porcelain` matches the local one.
   - `apply_delta(local_repo, base, new)`:
     - **Precondition:** the current local snapshot tree equals `base.tree`.
       Otherwise raise `SyncConflictError(paths)`, keep `new` fetched at
       `refs/mo/remote/<run>/<node>`, and do not touch the worktree.
     - **Apply:** `git diff --binary base new | git apply` to the worktree
       only.
     - **Postcondition:** the local snapshot tree equals `new.tree`. Otherwise
       raise `SyncIntegrityError`.
   - Remote HEAD moved: fetch the remote commits to
     `refs/mo/remote/<run>/head`, still apply the tree delta, and return
     `head_moved=True`.
2. Session-level sync state in `RemoteWorkspace` (`runtime/backends/remote.py`):
   - Keep **one** `last_synced` snapshot per session, not per spawn. All sync
     operations are serialized by a per-session lock.
   - This makes parallel-pool nodes correct. Remote nodes A and B share the
     replica. After A's sync-down, `last_synced = snapA`. B's sync-down
     precondition then checks local == snapA, and only the delta `snapA →
     snapB` is applied.
   - Hooks:
     - `up()` does the initial upload.
     - Before `spawn()`/`exec()`: sync-up if the local snapshot tree differs
       from `last_synced.tree`.
     - After `spawn()`: sync-down.
     - After `exec()`: **no** sync-down, because verifiers and tool exec never
       write the truth (epic 11).
3. Node-agent side (`mini_ork/remote/node_agent/`): `tree/bundle` calls
   `materialize`, and `tree/snapshot` calls `snapshot` plus
   `incremental_bundle` relative to the given base. Snapshotting reuses the
   same `tree_sync.py` module, because the engine is shared (D8).
4. Failure mapping:
   - `SyncConflictError` → node `failure_class=sync_conflict`, with the
     conflicting paths in the event.
   - `SyncIntegrityError` → `failure_class=infra_interruption`.
   - `SyncTooLargeError` → fail the run at `up()` with the sizes in the
     message.
5. Events: `remote.sync.up` and `remote.sync.down` carry `{bytes, files_changed,
   ms, tree}`. `remote.head_moved` is emitted when the remote HEAD moved. All
   go through the existing run-event emitter.
6. Submodules and Git LFS: detect them and emit a WARN event (unsupported in
   v1, transferred as gitlinks or pointers). Document this in
   `docs/operator/remote-nodes.md`.

## Files in scope (touch ONLY these)

- `mini_ork/remote/tree_sync.py` (new)
- `mini_ork/runtime/backends/remote.py` (sync hooks + session state)
- `mini_ork/remote/node_agent/sessions.py` (tree endpoints call tree_sync)
- `tests/unit/test_tree_sync.py` (new), `tests/unit/test_remote_tree_sync_e2e.py` (new, in-process node-agent)
- `docs/operator/remote-nodes.md` (append "What gets synced")

## Out of scope

- The run dir (epic 08). Pushing to GitHub: the publisher stays local and
  unchanged.

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_tree_sync.py tests/unit/test_remote_tree_sync_e2e.py && make lint
```

## Acceptance

- Round trip on temp repos:
  - The local repo has a modified tracked file, a new untracked file, an
    ignored file and a `.env`.
  - After the initial upload, the remote `git status --porcelain` equals the
    local one, minus the `.env` and the ignored file.
  - The remote edits one file, creates one and deletes one. After sync-down,
    the local worktree shows exactly those three changes.
  - The local HEAD, index and stash list are unchanged.
- A local edit made between sync-up and sync-down raises
  `SyncConflictError`. The local file keeps the user's edit, and
  `refs/mo/remote/<run>/<node>` exists.
- Parallel case: two spawns with interleaved sync-downs on one session apply
  both deltas, and the final local tree equals the final remote tree.
- The bundle ladder picks `squashed` when the full and branch bundles exceed a
  tiny test cap.
- Through the in-process node-agent, a fake "agent" spawn (`sh -c 'echo x >>
  file'`) produces that change in the local checkout.

## Review bar (added after epics 02–04 were reviewed)

Each earlier epic passed its gate and still failed review for the same reason:
the acceptance tests that drive the REAL seam were never written, so unwired or
broken code looked green. This epic is rejected unless:

- every Acceptance bullet above has its own test, and that test exercises the
  production path (the executor / dispatch / CLI entrypoint), not only the new
  module in isolation;
- every new public function or class has at least one production caller
  (`git grep` for it outside its own module and tests);
- nothing is gated only by a skip: a daemon- or network-gated test must run in
  this environment (Docker is available via colima; only `/Volumes/docker-ssd`
  is bind-visible), or the epic says why it cannot;
- the default path (feature env unset) is proven byte-identical by a test.
