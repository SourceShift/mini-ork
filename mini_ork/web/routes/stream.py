"""SSE: tail mo_events + run_events so the UI updates live.

Concurrency notes (the part that previously stalled REST traffic):
  - SSE handlers are `async def` running on the main event loop. ANY
    synchronous sqlite call inside an async handler blocks the event
    loop, which freezes every other endpoint served by this worker.
  - All sqlite reads here go through `asyncio.to_thread(...)` so they
    execute on the threadpool. Combined with StateDB's per-thread
    connection pool, multiple SSE streams can run concurrently without
    blocking REST handlers like /summary or /health.

Poll cadence is 2s by default — fleet UI's TanStack Query already
refetches at 5s so 1s was over-fetching. KEEPALIVE_INTERVAL_S avoids
proxy timeouts when there's no traffic.

remote-nodes-09: in addition to the run_events / mo_events tail, the
SSE stream emits ``node.live`` events whenever a tracked node's
``<run_dir>/agent-<node>.live.jsonl`` sidecar grows. Subscribers pick
nodes via ``?nodes=n1,n2,n3`` (comma-separated); the matching file is
polled on the same ``POLL_INTERVAL_S`` cadence and only the delta is
sent. The byte offset is held in this handler's local state — SSE
clients keep one tail per stream, so a reconnect resets the offset to 0
and the client treats the next event as a full snapshot (same as
``node_live.get_live``).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..db import StateDB
from ..deps import get_db, get_home

router = APIRouter(prefix="/api/v1/stream", tags=["stream"])

POLL_INTERVAL_S = 2.0
KEEPALIVE_INTERVAL_S = 15.0

# remote-nodes-09: per-node live sidecar filename. Must match
# ``node_live.LIVE_FILE_NAME`` — a future rename is one edit + one
# runner rebuild (the centralization here is so the SSE and the GET
# route agree).
_LIVE_FILE_NAME = "agent-{node}.live.jsonl"
# max bytes per node.poll each SSE iteration. Bounded so a fast-producing
# node doesn't stuff a single poll into one giant event frame; the SSE
# client just keeps the offset and reads the rest on the next tick.
_MAX_CHUNK_BYTES = 64 * 1024


async def _event_loop(
    db: StateDB,
    request: Request,
    task_run_id: str | None,
    nodes: list[str] | None = None,
    home: Path | None = None,
) -> AsyncIterator[str]:
    cursor_mo = 0
    cursor_run_evt = 0
    last_keepalive = time.monotonic()

    # Initial cursor: skip historical; stream only new from now.
    has_mo = await asyncio.to_thread(db.has_table, "mo_events")
    has_re = await asyncio.to_thread(db.has_table, "run_events")
    has_tr = await asyncio.to_thread(db.has_table, "task_runs")

    if has_mo:
        row = await asyncio.to_thread(db.row, "SELECT COALESCE(MAX(id), 0) AS mx FROM mo_events")
        cursor_mo = int(row["mx"] if row else 0)
    if has_re:
        row = await asyncio.to_thread(
            db.row, "SELECT COALESCE(MAX(created_at), 0) AS mx FROM run_events"
        )
        cursor_run_evt = int(row["mx"] if row else 0)

    trace_id: str | None = None
    if task_run_id and has_tr:
        tr = await asyncio.to_thread(
            db.row, "SELECT trace_id FROM task_runs WHERE id = ?", (task_run_id,)
        )
        if tr:
            trace_id = tr.get("trace_id")

    # remote-nodes-09: per-node byte offsets, keyed by node name. Reset on
    # reconnect (each SSE client has its own tail). A node with no live
    # file yet never enters the dict — the missing-file branch returns
    # an empty delta without registering the offset, so the next poll
    # polls again.
    live_offsets: dict[str, int] = {}

    yield _format("hello", {"task_run_id": task_run_id, "trace_id": trace_id})

    while True:
        if await request.is_disconnected():
            break

        batch: list[dict[str, Any]] = []

        if has_mo:
            if trace_id:
                rows = await asyncio.to_thread(
                    db.rows,
                    """
                    SELECT id, ts, event_type, actor, status, duration_ms, cost_usd,
                           artifact_path, payload_json
                    FROM mo_events WHERE id > ? AND trace_id = ?
                    ORDER BY id ASC LIMIT 200
                    """,
                    (cursor_mo, trace_id),
                )
            else:
                rows = await asyncio.to_thread(
                    db.rows,
                    """
                    SELECT id, ts, event_type, actor, status, duration_ms, cost_usd,
                           artifact_path, payload_json
                    FROM mo_events WHERE id > ?
                    ORDER BY id ASC LIMIT 200
                    """,
                    (cursor_mo,),
                )
            if rows:
                cursor_mo = max(int(r["id"]) for r in rows)
                for r in rows:
                    batch.append({"source": "mo_events", **r})

        if has_re and (task_run_id or trace_id is None):
            if task_run_id:
                rows = await asyncio.to_thread(
                    db.rows,
                    """
                    SELECT event_id AS id, created_at AS ts, event_type, payload_json
                    FROM run_events
                    WHERE created_at > ? AND run_id = ?
                    ORDER BY created_at ASC LIMIT 200
                    """,
                    (cursor_run_evt, task_run_id),
                )
            else:
                rows = await asyncio.to_thread(
                    db.rows,
                    """
                    SELECT event_id AS id, created_at AS ts, event_type, payload_json
                    FROM run_events WHERE created_at > ?
                    ORDER BY created_at ASC LIMIT 200
                    """,
                    (cursor_run_evt,),
                )
            if rows:
                cursor_run_evt = max(int(r["ts"]) for r in rows)
                for r in rows:
                    batch.append({"source": "run_events", **r})

        for evt in batch:
            yield _format("event", evt)

        # remote-nodes-09: tail the live sidecars on the same cadence as
        # the run_events poll. ``task_run_id`` is the run; ``nodes`` is
        # the subscriber's filter (None = no live tailing — the GET
        # route is the always-available surface).
        if task_run_id and nodes and home is not None:
            run_dir = home / "runs" / task_run_id
            for node in nodes:
                evt = await asyncio.to_thread(
                    _read_live_delta, run_dir, node, live_offsets
                )
                if evt is not None:
                    yield _format("node.live", evt)

        now = time.monotonic()
        if now - last_keepalive >= KEEPALIVE_INTERVAL_S:
            yield ": keepalive\n\n"
            last_keepalive = now

        await asyncio.sleep(POLL_INTERVAL_S)


def _read_live_delta(
    run_dir: Path, node: str, offsets: dict[str, int]
) -> dict[str, object] | None:
    """Synchronous helper: read the byte delta of one node's live sidecar.

    Returns None when there's nothing new (most polls) so the SSE loop
    doesn't emit empty events. Updates ``offsets[node]`` in-place so the
    next poll picks up where this one left off. A truncation (file shrank)
    emits a single ``truncated=True`` event and resets the offset to 0;
    the next tick catches up from the new start.
    """
    path = run_dir / _LIVE_FILE_NAME.format(node=node)
    if not path.exists():
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    offset = offsets.get(node, 0)
    truncated = False
    if offset > size:
        offset = 0
        truncated = True
    if offset == size and not truncated:
        return None  # nothing new
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(_MAX_CHUNK_BYTES)
    except OSError:
        return None
    if not data and not truncated:
        return None
    new_offset = offset + len(data)
    offsets[node] = new_offset
    return {
        "node": node,
        "offset": new_offset,
        "chunk": data.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }


def _format(name: str, data: Any) -> str:
    payload = json.dumps(data, default=str)
    return f"event: {name}\ndata: {payload}\n\n"


@router.get("")
async def stream(
    request: Request,
    db: StateDB = Depends(get_db),
    task_run: str | None = None,
    nodes: str | None = None,
    home: Path | None = Depends(get_home),
) -> StreamingResponse:
    # ``nodes`` is comma-separated; empty / None = no live tailing (the
    # default, so existing clients see no change in payload shape).
    node_list: list[str] | None = None
    if nodes:
        node_list = [n.strip() for n in nodes.split(",") if n.strip()]
    return StreamingResponse(
        _event_loop(db, request, task_run, node_list, home),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
