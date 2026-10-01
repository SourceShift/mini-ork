# remote-nodes-02 — Run-scoped workspace session

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
decision **D2**. Runs locally against the existing `docker` backend; no cloud needed.

## Goal

A run gets **one** isolated workspace session. Every agent spawn in the run
reuses it: every node, every retry, every fallback lane. It is torn down
exactly once: at run end, on SIGTERM, or on `kill_run`. The run's drive/volume
is the pet; the container may be recycled.

## Why

- `_spawn_in_workspace` (`mini_ork/dispatch/core.py:243-275`) runs
  `ws.up() → ws.spawn() → ws.down()` per call. With
  `dispatch_with_fallback` (`providers.py:1633`) and the `llm_dispatch` retry
  loop (`llm_dispatch.py:456-486`), a single node can boot several containers.
  Each one is named `mo-ws-<uuid>` (`runtime/backends/docker.py:71`), so
  nothing is shared. A remote boot costs seconds plus a network round trip, so
  this is the dominant overhead remotely.
- A remote node needs per-run state that survives between spawns: the synced
  target replica, the mirrored run dir, and claude transcripts for `--resume`.
  Per-spawn cattle throws all of that away.
- Teardown currently depends on `finally: ws.down()` running. `kill_run`
  (`mini_ork/web/control.py:171-219`) sends SIGTERM/SIGKILL to pids, and
  nothing installs a SIGTERM handler, so the `finally` never runs. The
  container keeps running `sleep infinity` until the 6 h `sandbox-gc` TTL
  (`runtime/sandbox_reaper.py`).

## Requirements

1. New module `mini_ork/runtime/workspace_session.py`:
   - `get_run_session(run_id, backend, *, env, roots) -> Workspace`. It keeps
     a process-wide registry keyed by `(run_id, backend)`, calls `up()` lazily
     on first use, and returns the same instance afterwards. The registry is
     thread-safe, because the parallel pool dispatches concurrently.
   - `close_run_session(run_id)` calls `down()` once. It is idempotent and
     never raises; errors are logged.
   - A session marker file `<run_dir>/.workspace-session.json` records
     `{backend, session_id/container_id, created_at}`, so an external
     `kill_run` and `sandbox-gc` can find and reap it.
2. `_spawn_in_workspace` uses `get_run_session` when a run id is known
   (`MINI_ORK_RUN_ID` in the dispatch env). With no run id, for example
   ad-hoc dispatch or tests, it keeps today's one-shot behaviour.
3. Teardown wiring:
   - The executor's top-level run function calls `close_run_session(run_id)`
     in a `finally`.
   - Install a SIGTERM/SIGINT handler in the executor entrypoint that closes
     sessions, then re-raises the default action.
   - `kill_run` reads `.workspace-session.json` and, for `docker`, runs
     `docker rm -f <cid>` after signalling pids, so a killed run never leaves
     a live container.
4. Backends must tolerate repeated `spawn()` on one `up()`. Verify this for
   `docker` (`docker exec` into the same container) and `local`. `microvm`
   gets the same contract test, gated on `MO_MICROVM_LIVE=1`.
5. Default parity: with `MO_SANDBOX_SCOPE`/`MO_SANDBOX_BACKEND` unset,
   `_select_workspace` returns `host` (`providers.py:941`) and none of this
   code is imported. Prove this with a test that asserts
   `mini_ork.runtime.workspace_session` is absent from `sys.modules` after a
   host dispatch.

## Files in scope (touch ONLY these)

- `mini_ork/runtime/workspace_session.py` (new)
- `mini_ork/dispatch/core.py` (`_spawn_in_workspace` only)
- `mini_ork/cli/execute.py` (run-level `finally` + signal handler)
- `mini_ork/web/control.py` (`kill_run` session reap)
- `mini_ork/runtime/sandbox_reaper.py` (read session markers)
- `tests/unit/test_workspace_session.py` (new)

## Out of scope

- The remote backend (epic 06). Use `docker` and a registered fake backend in
  tests.
- Changing image or drive config resolution (`agent_workspace.py` stays the
  single source).

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_workspace_session.py tests/unit/test_docker_spawn.py tests/unit/test_sandbox_reaper.py && make lint
```

## Acceptance

- With a fake backend counting `up()`/`down()`: 3 nodes × 2 retries × 2
  fallback lanes in one run gives exactly 1 `up` and 1 `down`.
- A SIGTERM delivered to the executor mid-spawn results in `down()` being
  called. Use the fake backend plus `os.kill(os.getpid(), SIGTERM)` in a
  subprocess test.
- `kill_run` on a run with a live docker session leaves no container with
  label `mo.sandbox=1` for that run. This test is daemon-gated, like
  `test_docker_workspace.py`.
- Host-path dispatch with the env unset is unchanged, and the module is not
  imported.
