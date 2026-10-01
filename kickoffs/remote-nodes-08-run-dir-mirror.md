# remote-nodes-08 — Run-dir mirror

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
"Run-dir mirror" section and **D4** (`/workspace/run` and
`/workspace/mo-home`). Depends on epic 03 (prompt paths already point at
`/workspace/run`) and epic 06 (remote backend + files API).

## Goal

Whatever a node reads from or writes to the run dir works remotely the same as
locally. Before remote work, the remote `/workspace/run` holds the current
local run dir. Afterwards, files the remote side created or changed are back
in the local run dir, so the executor's existing readers keep working
unchanged. A read-only subset of `MINI_ORK_HOME/config` is available at
`/workspace/mo-home`.

## Why (traced 2026-10-01)

- **Prompts point agents at run-dir files they must read:** framework-edit
  lens outputs `${MINI_ORK_RUN_DIR}/lens-*.md`, which are never inlined, and
  input manifests (`mini_ork/workflow/artifacts.py:211-221`). They also point
  at files the agent must write: researcher/reviewer `{out_file}`
  (`cli/execute_handlers.py:679`, `:847`) and `framework-edit.diff`.
- **Executor readers expect those files locally:** the agent-wins-by-mtime
  output read (`execute_handlers.py:499-504`), usage/cost sidecars
  (`dispatch/providers.py:1531`, `:1543`, `:1347-1368`) and codex/opencode
  `agent-<node>.stream.jsonl` (`providers.py:1511-1529`). Remotely those reads
  return zeros or stale data, so cost accounting silently breaks.
- **The codex pricing file is read from `MINI_ORK_HOME`**
  (`codex_transport.py:177`). Without mo-home it falls back to default rates.

## Requirements

1. `mini_ork/remote/run_mirror.py`:
   - `manifest(root, *, excludes) -> {relpath: (sha256, size, mtime)}` walks
     the tree locally. The remote side reuses the node-agent's
     `files/manifest`.
   - `push(ws, local_run_dir, remote_manifest)` uploads local files whose sha
     differs, as a tar over `PUT …/files?root=run`.
   - `pull(ws, local_run_dir, before, after, local_at_push)` downloads files
     whose remote sha changed between `before` and `after`. If the local copy
     also changed since push (its sha differs from `local_at_push`), the local
     copy wins and a `remote.mirror.conflict` event is emitted. Otherwise the
     local file is written atomically (temp + rename). A freshly written file
     gets the current mtime, so the existing "agent wins if mtime > marker"
     logic at `execute_handlers.py:499-504` keeps its meaning. An unchanged
     remote file is not rewritten, so the reader falls back to stdout exactly
     as it does locally.
   - Default excludes, all executor-owned or huge: `execute.log`, `*.pid`,
     `.stop-requested`, `.workspace-session.json`, `state.db*`, and
     `agent-*.live.jsonl` (epic 09 tees those live).
   - Per-file cap `MO_REMOTE_MIRROR_MAX_FILE_MB` (default 50) and per-sync cap
     `MO_REMOTE_MIRROR_MAX_TOTAL_MB` (default 500). An over-cap file is
     skipped with a `remote.mirror.skipped` event naming it; it is never
     truncated.
2. mo-home subset, uploaded once at `up()` to `/workspace/mo-home`: `config/*.yaml`
   (`providers.yaml`, `agents.yaml`, pricing and lane-policy files), the
   effective run-snapshot config under `<run_dir>/config/` (which shadows
   home), and `recipes/<recipe>/` for the run's recipe. Never upload
   `state.db*`, `secrets*`, `auth-tokens.txt` or `*.env`. Enforce this with a
   deny-list test.
3. Hooks in `RemoteWorkspace`:
   - Before `spawn()`/`exec()`, `push`.
   - After `spawn()` **and** after `exec()`, `pull`. Verifiers write
     `verifier_*.json` into the run dir; the tree is still never synced down
     after `exec` (epic 07).
   - Serialize the mirror under the same per-session lock as tree sync.
4. Events `remote.mirror.push` and `remote.mirror.pull` carry `{files, bytes,
   ms}`.

## Files in scope (touch ONLY these)

- `mini_ork/remote/run_mirror.py` (new)
- `mini_ork/runtime/backends/remote.py` (mirror hooks + mo-home upload)
- `tests/unit/test_run_mirror.py` (new)
- `docs/operator/remote-nodes.md` (append "Run dir on the remote side")

## Out of scope

- Live streaming of in-progress output (epic 09). The mirror is a
  boundary-time sync.
- Mirroring `state.db`. The control plane is the only DB writer (D5).

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_run_mirror.py tests/unit/test_remote_workspace.py && make lint
```

## Acceptance

- An in-process node-agent session whose fake agent spawn (1) reads
  `lens-a.md` (which exists only locally before the push), (2) writes
  `research.md` and `framework-edit.diff`, and (3) writes a usage sidecar
  JSON. After `pull`, all three exist locally with remote content. The
  executor's usage reader returns the remote tokens and cost, not zeros.
- A file changed on both sides keeps the local copy and emits one conflict
  event.
- An over-cap file is skipped and named in an event. The run continues.
- The mo-home upload contains no deny-listed file names. The test seeds
  `state.db`, `secrets.local.sh` and `auth-tokens.txt` locally and asserts
  none arrives.

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
