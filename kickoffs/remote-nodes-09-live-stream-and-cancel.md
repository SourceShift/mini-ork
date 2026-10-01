# remote-nodes-09 — Live stream and cancel for remote procs

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
**D7** and the failure-modes table ("`kill_run` from UI/CLI"). Depends on epic 06.
This is the "I can control everything" epic for in-flight nodes.

## Goal

While a remote node runs, its output appears on the laptop as it is produced,
and the operator's controls work on it:
- **stop:** finish this node, then halt
- **kill:** terminate now, with no orphan anywhere
- **timeout:** the remote process is killed and rc=124 is recorded

## Why (traced 2026-10-01)

- `LiveWriter` (`mini_ork/dispatch/live_stream.py:67`) and the per-line tee in
  `spawn_local` (`dispatch/core.py:126`, `:150-169`) already exist. But
  `MO_LIVE_FILE` is never set by production code, and no web route reads a
  live sidecar (grep of `mini_ork/web` for `live` found nothing).
  `Workspace.spawn` backends return output only at exit
  (`runtime/backends/docker.py:219-228`). So a remote node is invisible until
  it ends.
- `stop_run` (`web/control.py:145-148`) only touches `.stop-requested`, which is
  checked before the next node (`execute_handlers.py:313`). That works
  unchanged remotely.
- `kill_run` (`web/control.py:171-219`) signals local pids. A remote process
  is outside its reach, and the in-container agent keeps running and spending.

## Requirements

1. **Live tee.**
   - When the executor dispatches a node with an isolated workspace, set the
     per-node live file to `<run_dir>/agent-<node>.live.jsonl`. Set it via
     `DispatchRequest.env`, not process env (P5).
   - `_spawn_in_workspace` (`dispatch/core.py:243`) opens
     `open_live_writer()` and passes an `on_chunk` callback into
     `RemoteWorkspace.spawn` (the hook from epic 06). It writes raw transport
     lines with stream tags, preserving `LiveWriter`'s append-only/byte-cap
     contract.
   - For `docker`/`local` backends, a best-effort tee is acceptable: docker
     can switch from `subprocess.run` to Popen line reads. Do not change the
     host path (`spawn_local`) behaviour beyond what already exists.
2. **Read surface.** Add `GET /api/v1/runs/{run_id}/nodes/{node}/live?offset=<n>`
   in `mini_ork/web/routes/`. It returns the bytes from `offset` plus a new
   `offset`, using the same bearer-token auth as the other `/api/v1` control
   routes (`web/auth.py`). Wire it into the existing SSE stream
   (`web/routes/stream.py`) as `node.live` events with `{node, offset,
   chunk}`, so the UI and any client can follow a remote node. This is
   UI-agnostic; rendering it is a later UI task.
3. **Kill propagation.**
   - `kill_run` reads `<run_dir>/.workspace-session.json` (epic 02). For
     backend `remote`, it calls the node-agent: kill every running proc of
     the session, then `DELETE` the session. It reuses `RemoteWorkspace` and
     the node registry, and it is bounded: a 10 s total budget, logged on
     failure.
   - It then signals local pids as today.
   - The executor's SIGTERM handler (epic 02) closes the session, which also
     kills remote procs.
4. **Timeout.** Two layers:
   - The node-agent enforces `timeout_s` server-side (epic 05).
   - The client also enforces `timeout + MO_REMOTE_KILL_GRACE_S` (default 30).
     On expiry it posts `kill` and records rc=124.

   The client never waits unbounded on a remote process.
5. **Events:** `remote.proc.start` `{node, proc_id, node_host}` and
   `remote.proc.exit` `{rc, ms, killed}`.

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/core.py` (`_spawn_in_workspace` live writer + on_chunk)
- `mini_ork/runtime/backends/remote.py` (on_chunk plumbing, client timeout, kill)
- `mini_ork/web/control.py` (`kill_run` remote branch)
- `mini_ork/web/routes/stream.py` + a new `mini_ork/web/routes/node_live.py`
- `tests/unit/test_remote_live_and_kill.py` (new)

## Out of scope

- Re-attaching after a disconnect or a control-plane restart (epic 10).
  This epic assumes the client stays up.
- UI rendering of live output.

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_remote_live_and_kill.py tests/unit -k "live or kill_run or stream" && make lint
```

## Acceptance

All cases run against the in-process node-agent (`--runtime host`).
- A spawn printing a line every 200 ms grows `agent-<node>.live.jsonl` while
  the spawn is still running. The test reads the file mid-run and sees at
  least 2 lines before exit.
- `GET …/live?offset=` returns only new bytes. The SSE stream emits
  `node.live` events.
- `kill_run` during a remote `sleep 60` spawn: the node-agent proc state
  becomes `killed` within 10 s, the session is deleted, and the executor
  records the node as failed/killed.
- A client-side timeout with an unresponsive server stream posts `kill` and
  returns rc=124.
- With isolation unset, the host dispatch path and live behaviour are
  byte-unchanged (parity test).
