# Event Hooks — pushing mini-ork events out to external observers

Mini-ork writes lifecycle events to the `run_events` table on every node
start / end / heartbeat and every child-run state change. The native read
path is pull-based: dashboards SELECT from `run_events`, supervisors
`tail -F` log files.

There are two push tiers:

- **Simple tier — `MINI_ORK_ON_EVENT`.** No server, no daemon, no network
  round-trip: a local command receives each event. This document.
- **Secure / served tier — SSE on `mini-ork serve`.** The HTTP event stream
  at `/api/v1/stream` (and the WebSocket at `/sockets/events/{id}`) is live
  and is the multi-client, authenticated option for shared deployments.
  See [Serving mini-ork](#serving) below.

## The contract

`MINI_ORK_ON_EVENT` is a command line: an executable plus its arguments.
It is split into argv with shell-style quoting and executed directly — **no
shell** — so the command may carry arguments without shell re-interpretation:

```bash
export MINI_ORK_ON_EVENT="/usr/local/bin/notify-hook --channel builds"
```

Keep secrets out of the command line: a hook's argv is visible to every user
in `ps` for as long as it runs. Let the hook read its token itself, e.g. a
wrapper that does `TOKEN="$(cat ~/.config/notify-hook/token)"` before calling
the real notifier.

At most 8 hooks run at once (`MINI_ORK_ON_EVENT_MAX_INFLIGHT`); an event that
arrives while all slots are busy is dropped, never queued. When `timeout` or
`gtimeout` is on `PATH`, the 5 s cap is enforced by it, so a hook started just
before mini-ork exits is still killed on time.

After every successful DB write of an event, mini-ork runs:

```
<command...> <event_type> <run_id> <payload_json>
```

The command's own arguments come first; the three event arguments are
appended:

| Arg | Type | Examples |
|---|---|---|
| `event_type` | string | `node_start`, `node_end`, `node_heartbeat`, `child.started`, `child.completed`, `child.failed`, `run_finished` |
| `run_id` | string | `run-1781509524-39638` |
| `payload_json` | JSON string | `{"node_id":"planner","node_type":"planner"}` |

Hard guarantees:

- **Non-blocking dispatch**: the hook is started and mini-ork returns at
  once. A daemon thread waits 5 seconds for it; a hook still running after
  that is killed as a process group and reaped. A slow or dead hook cannot
  wedge mini-ork.
- **No shell**: the value is split into argv and executed with
  `shell=False`. A pipeline (`a | b`), glob or `$(…)` is *not* interpreted;
  write a wrapper script for that.
- **Errors swallowed**: non-zero exit, missing binary, syntax errors —
  all silently ignored. Observability must never break dispatch.
- **Best-effort fire-and-forget**: the hook IS NOT a transactional sink.
  If the hook crashes after the DB write, the event row still exists for
  pull-side recovery.

### Heartbeats are opt-in

`node_heartbeat` rows are written constantly (tens of thousands per busy
machine), so they are **not** delivered by default. Export
`MINI_ORK_ON_EVENT_HEARTBEATS=1` to receive them as well. Every other
event type is delivered unconditionally.

### Invalid values

If the value does not split into a command, or its executable does not
exist or is not executable, the hook is disabled for the life of the
process and mini-ork writes exactly one line to stderr:

```
mini-ork: MINI_ORK_ON_EVENT is not an executable command; event hook disabled
```

The value itself is never printed — it may carry a token. Fix the path (or
the quoting) and start a new process; the warning is not repeated within
the same one.

## Reference handlers

Three example hooks ship under `examples/event-hooks/`. Drop one in
place and export `MINI_ORK_ON_EVENT` to get push delivery in under a
minute.

### 1. FIFO sink (one supervisor)

```bash
mkfifo /tmp/mini-ork.fifo
export MINI_ORK_EVENT_FIFO=/tmp/mini-ork.fifo
export MINI_ORK_ON_EVENT=$PWD/examples/event-hooks/fifo.sh

# In another shell:
tail -F /tmp/mini-ork.fifo | jq -c
```

Best for: a single supervisor process consuming events live. FIFO
semantics drop events when no reader is attached.

### 2. HTTP webhook (chat, dashboards, incidents)

```bash
export MINI_ORK_WEBHOOK_URL=https://your.dashboard/ingest
export MINI_ORK_ON_EVENT=$PWD/examples/event-hooks/webhook.sh
```

Each event becomes:

```http
POST /ingest HTTP/1.1
Content-Type: application/json

{"event":"node_end","run":"run-1781509524-39638","payload":{...}}
```

Uses `curl` with a 4s connect + 4s total timeout. A dead URL cannot
slow the dispatch loop.

### 3. JSONL log (multi-consumer / replay)

```bash
export MINI_ORK_EVENT_LOG=$HOME/.mini-ork-events.jsonl
export MINI_ORK_ON_EVENT=$PWD/examples/event-hooks/log.sh
```

Multiple readers can `tail -F` the same file concurrently. Line-atomic
append works as long as events fit in PIPE_BUF (≥512 bytes).

## Writing your own handler

The contract is intentionally tiny — any executable that accepts
`event_type run_id payload_json` as `$1 $2 $3` works.

```bash
#!/usr/bin/env bash
# my-hook.sh
case "$1" in
  child.failed)
    notify-send "mini-ork run $2 failed"
    ;;
  node_end)
    echo "$3" | jq -r '.node_id' | xargs -I{} echo "node {} done"
    ;;
  run_finished)
    echo "$3" | jq -r '"run \(.status) verdict=\(.verdict)"'
    ;;
esac
```

```bash
chmod +x my-hook.sh
export MINI_ORK_ON_EVENT="$PWD/my-hook.sh --origin mini-ork"
```

## Serving

The **secure tier** is the authenticated event stream of `mini-ork serve`:
SSE at `/api/v1/stream` (and the WebSocket at `/sockets/events/{id}`),
which supports multiple clients, auto-reconnect and backpressure. Use it
when mini-ork runs as a shared service. The `MINI_ORK_ON_EVENT` hook is
the **simple tier**: zero-dep, local, no server — right for laptop use and
shell-side supervisors.

## Event-type catalogue

Stable event types as of this writing — see
`mini_ork/observability/node_events.py` and
`mini_ork/orchestration/recursive.py` for the canonical list.

| Event | Source | When |
|---|---|---|
| `node_start` | `mo_node_emit` | a workflow node begins execution |
| `node_end` | `mo_node_emit` | a workflow node finishes (success or fail) |
| `node_heartbeat` | `mo_node_emit` | long-running node periodic ping (opt-in) |
| `run_finished` | `close_run_record` | a run reaches a terminal status (`published` / `failed`), with `status` and `verdict` |
| `child.started` | `mo_recursive_emit_event` | a recursive child run begins |
| `child.completed` | `mo_recursive_emit_event` | a recursive child run exits 0 |
| `child.failed` | `mo_recursive_emit_event` | a recursive child run exits non-zero |

Additional event types are written via the same path; the hook receives
them transparently. Filter on `$1` in your handler for the events you
care about.
