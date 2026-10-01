# remote-nodes-10 — Disconnect tolerance, reattach and resume

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
**D7** and failure-modes rows "network drop", "control plane dies" and "VM
lost". Depends on epic 09 (stream + kill) and epic 07 (tree sync, needed to
harvest a finished proc).

## Goal

A remote node survives the laptop:
- **Network drop or laptop sleep:** the remote process keeps running and the
  client reconnects from its byte offsets.
- **Control-plane process death:** `mini-ork recover <run>` re-attaches to the
  still-running remote process, or harvests the finished one, instead of
  paying for the node again.
- **Retries within the same run:** claude `--resume` can continue a
  transcript, because the transcript lives on the run volume.

## Why

- `RemoteWorkspace.spawn` (epic 06) is one HTTP stream. Any drop today
  surfaces as a failed node, and a retry re-dispatches. That pays twice and
  may run two agents on one replica.
- Durable-DAG resume exists:
  - `node_attempts` in `db/migrations/0050_node_dag_checkpoints.sql`
  - `mini-ork recover <run> --strategy resume|retry|repair|pause`
    (`mini_ork/recovery/planner.py:126-143`)
  - leases in `mini_ork/recovery/admin.py`
  - transcript restore in `mini_ork/recovery/resume_prep.py:76`

  None of these know that a node's work may still be running on another
  machine.
- Session transcripts are persisted and restored from the host `~/.claude`,
  keyed by a per-cwd slug (`stores/session_store.py:49-53`,
  `recovery/resume_prep.py:90-116`). Remotely the cwd is `/workspace/target`,
  so the slug differs and the transcript lives on the VM.

## Requirements

1. **Idempotent remote procs.**
   - The client computes `idempotency_key = sha256(run_id, node_id, attempt,
     input_hash)`, reusing the durable-DAG input hash.
   - It sends the key in `POST …/procs`. The node-agent
     (`mini_ork/remote/node_agent/procs.py`) returns the existing proc when
     the key is already known, in any state.
   - A re-dispatch of the same attempt therefore re-attaches by construction.
2. **Persisted proc identity.**
   - Migration `db/migrations/0061_remote_procs.sql` creates `remote_procs(run_id, node_id,
     attempt, node_host, session_id, proc_id, idempotency_key, state, rc,
     out_offset, err_offset, started_at, ended_at)`. It is additive, and
     legacy runs are unaffected.
   - The row is written on `remote.proc.start` and updated on exit.
     Offsets are checkpointed at least every 5 s while streaming.
3. **Reconnect loop.**
   - On a transport error mid-stream, `RemoteWorkspace.spawn` backs off
     (capped jittered exponential), checks `GET …/procs/{pid}`, and resumes
     the stream from the persisted offsets.
   - The total reconnect window is `MO_REMOTE_RECONNECT_MAX_S` (default
     21600, i.e. 6 h, long enough for a laptop sleep).
   - During the window the node shows `state=detached` via a
     `remote.proc.detached` event, followed by `remote.proc.reattached`.
   - After the window, the node fails `failure_class=infra_interruption` and
     the remote proc is killed.
4. **Recovery strategy `reattach`**, added to `RECOVERY_STRATEGIES` and made
   the default when an unconsumed `remote_procs` row exists for the
   first-incomplete node:
   - `running`: re-enter the node handler with the dispatch short-circuited to
     the existing proc via the idempotency key. Stream from offsets, then
     continue normally.
   - `exited`: fetch the remaining output, run sync-down (tree, epic 07) and
     pull (run dir, epic 08), then hand the result to the handler as if the
     spawn had just returned.
   - `unknown`/`lost` (node-agent restarted and cannot find it, or the VM is
     gone): fall back to `retry` with a `remote.proc.lost` event.
5. **Transcripts.**
   - Set `CLAUDE_CONFIG_DIR=/workspace/home/.claude` per run in the sandbox
     env, so transcripts persist on the run volume across spawns and retries.
     The `--resume <id>` continuation in `providers.py:1413-1415` then works
     remotely.
   - After each spawn, pull `home/.claude/projects/**/<session_id>.jsonl` into
     the local session store. Make the store key by **provider session id**
     (keeping the cwd-slug lookup as a fallback), so `resume_prep` finds
     remote transcripts.

## Files in scope (touch ONLY these)

- `db/migrations/0061_remote_procs.sql` (new) + `docs/SCHEMA.md` entry
- `mini_ork/runtime/backends/remote.py` (idempotency key, reconnect loop, offset checkpoints)
- `mini_ork/remote/node_agent/procs.py` (idempotency dedup)
- `mini_ork/recovery/planner.py` (+ the module that executes a strategy) — `reattach`
- `mini_ork/stores/session_store.py`, `mini_ork/recovery/resume_prep.py` (session-id keying)
- `tests/unit/test_remote_reattach.py` (new)

## Out of scope

- Moving a run to a different node host mid-run. The session is pinned to its
  node.
- Starting the *next* node while the laptop is away. The control plane is
  local by design; the doc states this explicitly.

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_remote_reattach.py tests/test_recovery_closure.py tests/test_lease_fencing.py tests/test_recover_lease_wiring.py && make lint
```

## Acceptance

All cases run against the in-process node-agent (`--runtime host`).
- Kill the client's HTTP connection mid-stream (a fault-injecting transport
  in the test). The spawn reconnects and returns the complete output exactly
  once, with no duplicated bytes, and the proc started once.
- Simulated control-plane death: start a remote spawn, discard the client,
  and run recovery. The `reattach` strategy yields the node's result.
  node-agent proc count is 1 and `llm_calls` holds a single dispatch for that
  attempt.
- An exited-while-away proc is harvested: its tree edits appear locally and
  its run-dir outputs are pulled.
- A lost proc falls back to `retry` with the event emitted.
- A claude transcript written remotely is found by
  `resume_prep.restore(session_id)` locally.

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
