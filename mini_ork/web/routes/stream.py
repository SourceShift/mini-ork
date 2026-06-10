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
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..db import StateDB
from ..deps import get_db

router = APIRouter(prefix="/api/v1/stream", tags=["stream"])

POLL_INTERVAL_S = 2.0
KEEPALIVE_INTERVAL_S = 15.0


async def _event_loop(db: StateDB, request: Request, task_run_id: str | None) -> AsyncIterator[str]:
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

        now = time.monotonic()
        if now - last_keepalive >= KEEPALIVE_INTERVAL_S:
            yield ": keepalive\n\n"
            last_keepalive = now

        await asyncio.sleep(POLL_INTERVAL_S)


def _format(name: str, data: Any) -> str:
    payload = json.dumps(data, default=str)
    return f"event: {name}\ndata: {payload}\n\n"


@router.get("")
async def stream(
    request: Request,
    db: StateDB = Depends(get_db),
    task_run: str | None = None,
) -> StreamingResponse:
    return StreamingResponse(
        _event_loop(db, request, task_run),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
