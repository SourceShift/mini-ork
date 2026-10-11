"""The IDE's read surface: the same `mini-ork board` payloads over loopback.

The desktop IDE polls the board and opens pages by running `mini-ork board
… --json` as a subprocess. That is a whole Python interpreter per read — the
launcher shell, the dispatcher, and a second `python -m` for the module — which
costs ~0.25 s of process churn before any real work, on every poll and every
click. Over a `mini-ork serve` that is already running, the identical work is a
function call.

Nothing here is new logic. Each route parses the argv it would have been
invoked with and hands it to `board_cmd.payload_for_verb`, so a page read over
loopback and one over a subprocess run the same code — `tests/unit/…` pins that
by comparing both paths' payloads. Writes (`merge`, `gate`, `retry`, …) are
deliberately NOT here: they are user-initiated, one at a time, and privileged,
so they keep going through the subprocess path.

Home scoping is the shared `get_home` dependency, so the IDE names its project
with the `X-Mini-Ork-Home` header and one server can serve several projects.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..deps import get_home
from ...cli import board_cmd

router = APIRouter(prefix="/ide", tags=["ide"])


def _json(payload: dict) -> Response:
    """Serialise like the CLI does (`default=str`, so a Path/datetime in the
    payload does not 500), as a raw Response since FastAPI's JSONResponse
    would use the stricter stdlib encoder."""
    return Response(content=json.dumps(payload, default=str), media_type="application/json")


def _read(home: Path, argv: list[str]) -> Response:
    try:
        return _json(board_cmd.payload_for_verb(home, argv))
    except ValueError as exc:
        # An argv this surface would not have been asked for (a bad offset, a
        # verb missing its target) is the client's error, not ours.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/board")
def ide_board(shell: bool = Query(default=False), home: Path = Depends(get_home)) -> Response:
    """`board --json [--shell]`: the shell poll (header/runs/counts) or the
    full document."""
    argv = ["show", "--json"] + (["--shell"] if shell else [])
    return _read(home, argv)


@router.get("/page")
def ide_page(
    key: str = Query(...),
    tab: str | None = Query(default=None),
    arg: list[str] | None = Query(default=None),
    home: Path = Depends(get_home),
) -> Response:
    """`board page <key> [--tab T] [--arg k=v …]`: one IDE page. The key is a
    query parameter, not a path segment, because a run page's key carries a
    `run:<id>` colon and a path would have to be encoded to survive it."""
    argv = ["page", key]
    if tab:
        argv += ["--tab", tab]
    for pair in arg or []:
        argv += ["--arg", pair]
    return _read(home, argv)


@router.get("/node")
def ide_node(
    run: str = Query(...),
    node: str = Query(...),
    view: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    home: Path = Depends(get_home),
) -> Response:
    """`board node <run> <node> [--view V] [--offset N]`: one node's detail."""
    argv = ["node", run, node]
    if view:
        argv += ["--view", view]
    if offset:
        argv += ["--offset", str(offset)]
    return _read(home, argv)


@router.get("/runs")
def ide_runs(
    query: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1),
    home: Path = Depends(get_home),
) -> Response:
    """`board runs [--query Q] --offset N --limit M`: run search."""
    argv = ["runs", "--json", "--offset", str(offset)]
    if query:
        argv += ["--query", query]
    if limit is not None:
        argv += ["--limit", str(limit)]
    return _read(home, argv)
