"""Hermetic tests for the ACP ``locations`` follow-the-agent feature.

The slice keeps the file location on every normalized live event so Zed's
"follow the agent" crosshair can jump straight to the buffer the chunk
describes. Two surfaces are under test:

1. ``mini_ork.acp.live.normalize`` — ``LiveEvent.path`` / ``LiveEvent.line``
   are populated for claude file-touching ``tool_use`` blocks (and the
   codex ``file_change`` item) and left empty otherwise. Existing
   ``kind``/``text`` behaviour is preserved.

2. ``mini_ork.acp.agent.MiniOrkAcpAgent._drain_live_for`` — every text /
   tool / tool_output chunk whose source ``LiveEvent`` has a non-empty
   ``path`` is projected as a ``ToolCallProgress`` that ALSO carries a
   single ``ToolCallLocation(path, line)``. Absolute paths go through
   unchanged, relative paths are joined to the session's cwd, a session
   without a known cwd sends no ``locations``, and a steady stream of
   tool calls on the same file only re-asserts the location once.

No network is involved — every test runs against an in-memory agent with
injected launcher / reader and a capturing fake connection.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from acp.schema import (  # noqa: E402
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
)
from mini_ork.acp.agent import MiniOrkAcpAgent  # noqa: E402
from mini_ork.acp.live import normalize  # noqa: E402


def _record(line: str, *, seq: int = 0, stream: str = "stdout") -> dict:
    """Build a ``LiveWriter`` envelope around ``line``."""
    return {"seq": seq, "stream": stream, "t": 0.0, "line": line}


def _text_block(text: str):
    from acp.schema import TextContentBlock

    return TextContentBlock(type="text", text=text)


# ── normalize: claude ``assistant`` envelope ────────────────────────────────


def test_normalize_claude_tool_use_edit_sets_absolute_path():
    """An ``Edit`` ``tool_use`` keeps the absolute ``file_path`` on the event,
    leaves ``line`` ``None`` (the ACP field is for Read offsets only), and
    preserves the existing ``text`` rendering verbatim."""
    rec = _record(json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "Edit",
            "input": {"file_path": "/abs/x.py", "new_text": "x"},
        }]},
    }))
    out = normalize(rec)
    assert len(out) == 1
    ev = out[0]
    assert ev.kind == "tool"
    assert ev.path == "/abs/x.py"
    assert ev.line is None
    assert ev.text == "Edit: " + json.dumps(
        {"file_path": "/abs/x.py", "new_text": "x"},
        ensure_ascii=False,
    )


def test_normalize_claude_tool_use_read_with_offset_sets_zero_based_line():
    """A ``Read`` with ``offset=10`` → ``line == 9`` (ACP/Zed is 0-based,
    claude's ``offset`` is 1-based). A ``Read`` without an ``offset`` →
    ``line is None`` so the editor opens at the first line."""
    with_offset = _record(json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "Read",
            "input": {"file_path": "/abs/x.py", "offset": 10},
        }]},
    }))
    no_offset = _record(json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "Read",
            "input": {"file_path": "/abs/x.py"},
        }]},
    }))
    out_with = normalize(with_offset)
    out_no = normalize(no_offset)
    assert len(out_with) == 1 and len(out_no) == 1
    assert out_with[0].path == "/abs/x.py"
    assert out_with[0].line == 9
    assert out_no[0].path == "/abs/x.py"
    assert out_no[0].line is None


def test_normalize_claude_tool_use_notebook_edit_uses_notebook_path():
    """``NotebookEdit`` surfaces ``notebook_path``; ``Bash`` is not a
    file-touching tool so its ``path`` stays empty."""
    nb = _record(json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "NotebookEdit",
            "input": {"notebook_path": "/abs/notes.ipynb", "new_source": []},
        }]},
    }))
    bash = _record(json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "Bash",
            "input": {"command": "ls -la"},
        }]},
    }))
    out_nb = normalize(nb)
    out_bash = normalize(bash)
    assert len(out_nb) == 1 and len(out_bash) == 1
    assert out_nb[0].path == "/abs/notes.ipynb"
    assert out_nb[0].line is None
    assert out_bash[0].path == ""
    assert out_bash[0].line is None


def test_normalize_claude_tool_use_write_and_multi_edit_path():
    """``Write`` and ``MultiEdit`` also set ``path`` from ``file_path``."""
    for name in ("Write", "MultiEdit"):
        rec = _record(json.dumps({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "name": name,
                "input": {"file_path": f"/abs/{name.lower()}.py"},
            }]},
        }))
        out = normalize(rec)
        assert len(out) == 1
        assert out[0].path == f"/abs/{name.lower()}.py", name
        assert out[0].line is None, name


# ── normalize: codex ``item.completed`` envelope ────────────────────────────


def test_normalize_codex_file_change_uses_first_non_empty_path():
    """A codex ``file_change`` with multiple paths picks the FIRST non-empty
    one for the ``ToolCallLocation`` candidate; ``text`` keeps listing
    every path so the operator still sees the full edit set."""
    rec = _record(json.dumps({
        "type": "item.completed",
        "item": {
            "type": "file_change",
            "changes": [
                {"path": "a.py"},
                {"path": "b.py"},
            ],
        },
    }))
    out = normalize(rec)
    assert len(out) == 1
    ev = out[0]
    assert ev.kind == "tool"
    assert ev.text == "edited a.py, b.py"
    assert ev.path == "a.py"
    assert ev.line is None


# ── agent emission (Z-following) ────────────────────────────────────────────


class _FakeLocationConn:
    """Capturing ACP connection for the live-projection tests."""

    def __init__(self) -> None:
        self.captured: list[Any] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        del session_id
        self.captured.append(update)


def _write_live_lines(path: Path, lines: list[str]) -> None:
    """Append one ``LiveWriter``-shaped envelope per ``lines`` entry."""
    from mini_ork.dispatch.live_stream import LiveWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    with LiveWriter(str(path)) as writer:
        for line in lines:
            writer.write_line(line, "stdout", partial=False)


def _locations_for_tool_progress(
    captured: list[Any], tool_call_id: str, *, with_text: bool = True
) -> list[tuple[list[ToolCallLocation] | None, str | None]]:
    """Extract ``(locations, text)`` for each ``ToolCallProgress`` chunk
    that targets ``tool_call_id``. ``None`` locations when the field is
    absent; ``None`` text when the chunk is the terminal ``ToolCallProgress``
    (content=None). Filters out non-text-bearing progress updates when
    ``with_text`` is True."""
    out: list[tuple[list[ToolCallLocation] | None, str | None]] = []
    for u in captured:
        if not isinstance(u, ToolCallProgress):
            continue
        if getattr(u, "tool_call_id", None) != tool_call_id:
            continue
        if u.content is None:
            if not with_text:
                out.append((getattr(u, "locations", None), None))
            continue
        # Carry-over from routed updates (thread children carry a prefix).
        text = u.content[0].content.text
        out.append((getattr(u, "locations", None), text))
    return out


def test_agent_emits_locations_on_first_chunk_and_skips_dedup_repeats(tmp_path):
    """Two ``Edit`` records on the same file followed by one on a different
    file produce three ``ToolCallProgress`` chunks where the first and third
    carry ``ToolCallLocation`` and the second carries ``locations=None``
    (Zed would otherwise reopen the same buffer on every chunk). The text
    chunk still lands on every emit.

    The test sets the session's cwd via the run id (the run-session path
    binds ``_sessions[run_id] = cwd`` in ``new_session``); the relative
    path in the first two records therefore resolves to
    ``<tmp>/alpha.py``.
    """
    home = tmp_path / ".mini-ork"
    run_id = "run-loc-001"
    cwd = tmp_path  # absolute path the relative ``alpha.py`` resolves against
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    live_path = run_dir / "agent-implementer.live.jsonl"

    call = {"n": 0}

    def fake_reader(_run_id: str) -> dict:
        call["n"] += 1
        if call["n"] == 1:
            _write_live_lines(live_path, [json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "alpha.py", "new_text": "v1"},
                }]},
            })])
            return {
                "status": "executing",
                "events": [
                    {"event_type": "node_start",
                     "payload_json": json.dumps(
                         {"node_id": "implementer", "node_type": "implementer"}
                     )},
                ],
                "llm_calls": [],
            }
        if call["n"] == 2:
            # Same file again — should be dedup'd (no locations re-asserted).
            _write_live_lines(live_path, [json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "alpha.py", "new_text": "v2"},
                }]},
            })])
            return {"status": "executing", "events": [], "llm_calls": []}
        if call["n"] == 3:
            # Different file — a fresh locations emit.
            _write_live_lines(live_path, [json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "beta.py", "new_text": "v3"},
                }]},
            })])
            return {"status": "executing", "events": [], "llm_calls": []}
        return {"status": "published", "events": [], "llm_calls": []}

    agent = MiniOrkAcpAgent(
        home=home,
        reader=fake_reader,
        poll_interval=0,
        launcher=lambda _rid, _kick: {"ok": True},
    )
    # The session_id == run_id on a run session; ``new_session`` is the
    # callable that binds cwd, so go through it directly.
    asyncio.run(agent.new_session(cwd=str(cwd), run_id=run_id))
    conn = _FakeLocationConn()
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt(run_id, [_text_block("go")]))
    assert resp.stop_reason == "end_turn"

    locations_progress = _locations_for_tool_progress(
        conn.captured, tool_call_id="implementer"
    )
    # Three text-bearing progress chunks: alpha (locations), alpha (no
    # locations), beta (locations). The terminal ToolCallProgress
    # (content=None) is filtered when ``with_text=True``.
    assert len(locations_progress) == 3, [(loc, t) for loc, t in locations_progress]
    first_locs, first_text = locations_progress[0]
    second_locs, second_text = locations_progress[1]
    third_locs, third_text = locations_progress[2]

    # First: location resolved to <cwd>/alpha.py, line is None (Edit).
    assert first_locs is not None and len(first_locs) == 1
    assert first_locs[0].path == str(Path(cwd) / "alpha.py")
    assert first_locs[0].line is None
    assert first_text is not None and first_text.startswith("Edit:")

    # Second: same (path, line) as the first — dedup'd to None.
    assert second_locs is None
    assert second_text is not None and second_text.startswith("Edit:")

    # Third: different path → fresh location.
    assert third_locs is not None and len(third_locs) == 1
    assert third_locs[0].path == str(Path(cwd) / "beta.py")
    assert third_locs[0].line is None
    assert third_text is not None and third_text.startswith("Edit:")


def test_agent_omits_locations_when_session_has_no_known_cwd(tmp_path):
    """A run session whose ``_sessions`` binding was cleared (or never set)
    must NOT guess: a relative ``LiveEvent.path`` with no known cwd produces
    a text chunk but no ``locations`` field. Drives the same
    ``_await_terminal`` loop the running-agent tests use; the test simply
    blanks out the cwd binding after ``new_session`` and asserts the
    dedup'd live chunk still emits with ``locations=None``.
    """
    home = tmp_path / ".mini-ork"
    run_id = "run-loc-nocwd"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    live_path = run_dir / "agent-implementer.live.jsonl"

    call = {"n": 0}

    def fake_reader(_run_id: str) -> dict:
        call["n"] += 1
        if call["n"] == 1:
            _write_live_lines(live_path, [json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "name": "Edit",
                    # Relative path — the resolved path depends on the
                    # session's cwd. We blank the cwd binding just below.
                    "input": {"file_path": "alpha.py", "new_text": "x"},
                }]},
            })])
            return {
                "status": "executing",
                "events": [
                    {"event_type": "node_start",
                     "payload_json": json.dumps(
                         {"node_id": "implementer", "node_type": "implementer"}
                     )},
                ],
                "llm_calls": [],
            }
        return {"status": "published", "events": [], "llm_calls": []}

    agent = MiniOrkAcpAgent(
        home=home,
        reader=fake_reader,
        poll_interval=0,
        launcher=lambda _rid, _kick: {"ok": True},
    )
    asyncio.run(agent.new_session(cwd=str(tmp_path), run_id=run_id))
    # Simulate "no cwd known" — the kickoff says NEVER guess.
    agent._sessions[run_id] = ""
    conn = _FakeLocationConn()
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt(run_id, [_text_block("go")]))
    assert resp.stop_reason == "end_turn"

    progress = _locations_for_tool_progress(
        conn.captured, tool_call_id="implementer"
    )
    # The text chunk still arrives (we don't suppress chatty chunks just
    # because the location is unknown); the ``locations`` field is None.
    text_chunks = [(loc, t) for loc, t in progress if t is not None]
    assert text_chunks, "no live text chunks reached the projection"
    live_locs, _live_text = text_chunks[0]
    assert live_locs is None


# ── ordering: a live chunk never precedes its tool-call card ────────────────


def test_first_live_chunk_arrives_after_its_tool_call_card(tmp_path):
    """A node's first live chunk must follow the ``ToolCallStart`` that names
    its ``tool_call_id``.

    ``_project_snapshot`` records a node's ``node_start`` and drains its live
    tail in the same pass. The drain follows nodes whose start was *recorded*,
    so a chunk emitted before the card is a progress update for a tool call
    Zed has never been told about: it inserts a failed "Tool call not found"
    entry and returns before resolving the chunk's ``locations``. The live
    dedup would then mark that path as sent, suppressing the location on
    every later chunk for the same file — the crosshair never follows it.
    """
    home = tmp_path / "home"
    run_id = "run-order"
    node = "implementer"
    live = home / "runs" / run_id / f"agent-{node}.live.jsonl"
    _write_live_lines(live, [
        json.dumps({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/tmp/ordered.py", "new_text": "x"},
            }]},
        }),
    ])

    agent = MiniOrkAcpAgent(
        home=str(home),
        reader=lambda _rid: {"status": "executing", "events": [], "llm_calls": []},
        poll_interval=0.01,
        start_timeout=2.0,
    )
    conn = _FakeLocationConn()
    agent._conn = conn
    agent._sessions[run_id] = str(tmp_path)

    snapshot = {
        "status": "executing",
        "events": [
            {"event_type": "node_start",
             "payload_json": json.dumps({"node_id": node, "node_type": node})},
        ],
        "llm_calls": [],
    }
    asyncio.run(agent._project_snapshot(run_id, snapshot))

    card_at = next(
        (i for i, u in enumerate(conn.captured)
         if isinstance(u, ToolCallStart) and u.tool_call_id == node),
        None,
    )
    chunk_at = next(
        (i for i, u in enumerate(conn.captured)
         if isinstance(u, ToolCallProgress) and u.tool_call_id == node
         and getattr(u, "locations", None)),
        None,
    )
    shapes = [type(u).__name__ for u in conn.captured]
    assert card_at is not None, shapes
    assert chunk_at is not None, shapes
    assert card_at < chunk_at, (
        "the first chunk with a location must follow its tool-call card"
    )
