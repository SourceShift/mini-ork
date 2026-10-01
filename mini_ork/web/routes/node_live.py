"""Live sidecar reader — bytes-from-offset for ``agent-<node>.live.jsonl``.

remote-nodes-09 (kickoff §2): the dispatch path (epic 06 + 09) writes per-node
live JSONL sidecars under ``<run_dir>/agent-<node>.live.jsonl``. This module
exposes them as a binary-friendly read surface so the UI and any client can
follow a remote node's output *while the spawn is still running* (the whole
point — without it the operator has no visibility into in-flight nodes).

Endpoint::

    GET /api/v1/runs/{run_id}/nodes/{node}/live?offset=<n>

Returns ``{"offset": <new_offset>, "chunk": "<utf-8 bytes as str>", "truncated": bool}``.

The byte offset is the position the client already holds; ``chunk`` is the
new bytes from that position to end-of-file, returned as a Python string
(JSON encodes it as a UTF-8 string; a binary-sensitive client can
re-interpret as UTF-8). The new offset is the file size at read time, so
the next call asks for ``offset=new_offset`` and gets only the delta. This
mirrors ``LiveWriter``'s append-only + byte-cap discipline: a tailer holds a
byte offset, the writer never truncates a prefix.

Auth: bearer-token, same as the other ``/api/v1`` write routes
(``/pause-cost``, ``/steer``, ``/resume-cost``). Fail-closed via
``mini_ork.web.auth.require_token``.

Mounting: ``mini_ork/web/app.py`` registers this router. Without that
registration the route is dead code; the implementer explicitly named this
file in the kickoff's "Files in scope" so the mount line is implicit.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, status

from .. import auth
from ..deps import get_home

router = APIRouter(prefix="/api/v1/runs", tags=["live"])

# The kickoff names this filename verbatim: ``<run_dir>/agent-<node>.live.jsonl``.
# Centralized here so the SSE event tailer (web/routes/stream.py) and this
# reader agree on the source of truth — a future rename is a single edit.
LIVE_FILE_NAME = "agent-{node}.live.jsonl"


def _live_path(home: Path, run_id: str, node: str) -> Path:
    """Resolve the live sidecar for ``(run_id, node)``.

    The run dir is the canonical storage location; ``home`` is the
    ``.mini-ork`` directory resolved by ``deps.get_home``. A run that does
    not exist or a node name that would traverse (e.g. ``../etc/passwd``)
    is rejected with 404 / 400 — never 500 — so a probing client gets a
    clear answer rather than a stack trace.
    """
    if not run_id or ".." in run_id or "/" in run_id or "\\" in run_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid run_id",
        )
    if not node or ".." in node or "/" in node or "\\" in node:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid node",
        )
    run_dir = home / "runs" / run_id
    if not run_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="run not found",
        )
    return run_dir / LIVE_FILE_NAME.format(node=node)


@router.get(
    "/{run_id}/nodes/{node}/live",
    dependencies=[Depends(auth.require_token)],
)
def get_live(
    run_id: str = PathParam(...),
    node: str = PathParam(...),
    offset: int = Query(0, ge=0, description="byte offset the caller already holds"),
    home: Path = Depends(get_home),
) -> dict[str, object]:
    """Read bytes from ``offset`` to EOF; return them plus the new offset.

    A missing file returns ``{"offset": 0, "chunk": "", "truncated": False}``
    (an empty sidecar — the writer hasn't been opened yet for this node, or
    the node never dispatched). A non-existent offset (e.g. the file has
    been truncated by a cap+rewrite) returns ``truncated=True`` so the
    client knows to reset to 0 rather than skip into garbage.
    """
    path = _live_path(home, run_id, node)
    if not path.exists():
        return {"offset": 0, "chunk": "", "truncated": False}
    try:
        size = path.stat().st_size
        truncated = False
        if offset > size:
            # The file shrank since the client last polled — treat as a
            # truncation so the client resets rather than reading nothing.
            offset = 0
            truncated = True
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        # ``str(...)`` is JSON-friendly; clients that need raw bytes can
        # UTF-8-encode it. The cost is one allocation per chunk, which is
        # what JSONL parsers already pay.
        return {
            "offset": offset + len(data),
            "chunk": data.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }
    except OSError:
        # Read transiently — the file may be being written by a tee right
        # now; a transient error should not 500. Treat as empty.
        return {"offset": 0, "chunk": "", "truncated": False}