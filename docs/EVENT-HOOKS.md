# Event Hooks — pushing mini-ork events out to external observers

Mini-ork writes lifecycle events to the `run_events` table on every node
start / end / heartbeat and every child-run state change. The native
read path is pull-based: dashboards SELECT from `run_events`, supervisors
`tail -F` log files. For real-time push to chat bots, agent supervisors,
incident systems, or live dashboards, point `MINI_ORK_ON_EVENT` at a
shell command and mini-ork will fire it best-effort after every event
write.

## The contract

```bash
export MINI_ORK_ON_EVENT=/abs/path/to/hook.sh
```

After every successful DB write of an event, mini-ork invokes:

```bash
"$MINI_ORK_ON_EVENT" "<event_type>" "<run_id>" "<payload_json>"
```

| Arg | Type | Examples |
|---|---|---|
| `event_type` | string | `node_start`, `node_end`, `node_heartbeat`, `child.started`, `child.completed`, `child.failed` |
| `run_id` | string | `run-1781509524-39638` |
| `payload_json` | JSON string | `{"node_id":"planner","node_type":"planner"}` |

Hard guarantees:

- **Non-blocking dispatch**: the hook is invoked with a 5-second timeout
  (`gtimeout 5` / `timeout 5`). A slow or dead hook cannot wedge mini-ork.
- **Errors swallowed**: non-zero exit, missing binary, syntax errors —
  all silently ignored. Observability must never break dispatch.
- **Best-effort fire-and-forget**: the hook IS NOT a transactional sink.
  If the hook crashes after the DB write, the event row still exists for
  pull-side recovery.

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
esac
```

```bash
chmod +x my-hook.sh
export MINI_ORK_ON_EVENT="$PWD/my-hook.sh"
```

## When to use SSE on `mini-ork serve` instead

If you have multiple browser clients or want auto-reconnect /
backpressure, run `mini-ork serve` and connect to its event stream
(planned — see issue tracker). For shell-side supervisors and
single-consumer pipelines, the `MINI_ORK_ON_EVENT` hook is simpler and
zero-dep.

## Event-type catalogue

Stable event types as of this writing — see `lib/mo_node_events.sh` and
`lib/recursive_orchestration.sh` for the canonical list.

| Event | Source | When |
|---|---|---|
| `node_start` | `mo_node_emit` | a workflow node begins execution |
| `node_end` | `mo_node_emit` | a workflow node finishes (success or fail) |
| `node_heartbeat` | `mo_node_emit` | long-running node periodic ping |
| `child.started` | `mo_recursive_emit_event` | a recursive child run begins |
| `child.completed` | `mo_recursive_emit_event` | a recursive child run exits 0 |
| `child.failed` | `mo_recursive_emit_event` | a recursive child run exits non-zero |

Additional event types are written via the same path; the hook receives
them transparently. Filter on `$1` in your handler for the events you
care about.
