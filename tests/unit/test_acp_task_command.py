"""Hermetic tests for the ``/task`` slash command (kickoff → queue on board).

Acceptance cases verified end-to-end against a tmp project + a migrated
``state.db``; no real ``bin/mini-ork`` ever runs. The ``/task`` command
mirrors :func:`mini_ork.acp.commands.handle_kickoff` (thread-only carve-out,
orchestrator rewrite with a kickoff-style intent) but flips the post-draft
approval from "Start run" to "Queue on board"; queueing reuses
:meth:`MiniOrkAcpAgent._persist_kickoff_draft` (extracted from
``_handle_kickoff_save`` so the slug/collision logic is shared) and
in-process calls :func:`mini_ork.cli.epics.main`.

Five acceptance cases:
  1. ``/task [arg]`` in a thread → ``_RewriteToOrchestrate`` with the
     task intent + a bridge line that names Queue on board.
  2. ``/task`` in a run session → plain string ("Tasks are written in a
     mini-ork thread"); the thread-only carve-out is intact.
  3. ``/task`` adds the session to ``agent._task_queue_sessions`` so the
     kickoff approval that follows swaps Start run for Queue on board.
  4. Clicking Queue on board saves the staged draft AND inserts an
     ``epics`` row via ``epics add``; the user sees the epic id +
     scheduler-pickup note; the session mark is consumed.
  5. After queueing (or a dismissed dialog), a later ``/kickoff`` in the
     same thread still offers Start run first — the session mark was
     consumed, not sticky.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from acp.schema import (  # noqa: E402
    AgentMessageChunk,
    TextContentBlock,
    ToolCallProgress,
)

from mini_ork.acp import commands as cmds  # noqa: E402
from mini_ork.acp.agent import MiniOrkAcpAgent  # noqa: E402
from mini_ork.cli import epics as ep  # noqa: E402
from mini_ork import scheduler as sched  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


# ── helpers (mirrored from test_acp_commands / test_acp_agent_py) ───────────


def _migrate_db(db_path: Path) -> None:
    rc, out, err = mig.init_db(db=str(db_path), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"


def _init_db_via_sh(home: Path) -> str:
    """Same seam test_epics_add uses — the bash entrypoint is gone, but
    the script delegates to ``mini_ork.stores.migrate`` which is
    available here directly. We call the shim anyway so a future
    bash-removal swap stays a one-liner."""
    home.mkdir(parents=True, exist_ok=True)
    db = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        capture_output=True, text=True, check=True,
    )
    return db


def _agent(home: Path) -> MiniOrkAcpAgent:
    """A bare agent bound to the tmp home so ``agent._home_for`` resolves.
    Mirrors :func:`tests.unit.test_acp_commands._agent`."""
    a = MiniOrkAcpAgent(home=str(home))
    a._sessions["run-1-abc"] = str(home.parent)
    a._thread_sessions.add("run-1-abc")
    a._thread_config["run-1-abc"] = {"recipe": "code-fix"}
    return a


def _text_block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


class _SidConn:
    """ACP connection that records which session each update was sent to."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.asked: list[tuple[str, list[str]]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.sent.append((session_id, update))

    async def request_permission(self, session_id, tool_call, options, **kw):
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        self.asked.append((tool_call.tool_call_id, [o.option_id for o in options]))
        # Default: dismiss (no choice). Tests that want a specific answer
        # monkeypatch ``self.choices`` before calling prompt.
        choice = getattr(self, "next_choice", None)
        if choice is None:
            outcome = DeniedOutcome(outcome="cancelled")
        else:
            outcome = AllowedOutcome(outcome="selected", option_id=choice)
        return RequestPermissionResponse(outcome=outcome)


def _seed_staged_draft(home: Path, draft_id: str, body: str) -> None:
    """Drop a staged draft where ``_persist_kickoff_draft`` will find it."""
    (home / "kickoff-drafts").mkdir(parents=True, exist_ok=True)
    (home / "kickoff-drafts" / f"{draft_id}.md").write_text(body, encoding="utf-8")


def _kickoff_turn(home: Path, prompts: list[str], body: str, *, recipe: str = "docs"):
    """A fake orchestrator turn that calls the real ``draft_kickoff`` MCP
    helper (mirrors ``_kickoff_turn`` in test_acp_agent_py)."""
    from mini_ork.mcp_context.server import dispatch

    def turn(lane, prompt, cwd, home_arg, resume, on_event):
        async def _run():
            prompts.append(prompt)
            resp = dispatch({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "draft_kickoff", "arguments": {
                    "recipe": recipe, "kickoff_markdown": body,
                }},
            }, control=True)
            inner = resp["result"]["content"][0]["text"]
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_k1",
                 "name": "mcp__mini-ork__draft_kickoff",
                 "input": {"recipe": recipe,
                           "kickoff_markdown": body}}]}})
            await on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_k1",
                 "content": [{"type": "text", "text": inner}]}]}})
            return _Turn()
        return _run()
    return lambda lane, prompt, cwd, home, resume, on_event: turn(
        lane, prompt, cwd, home, resume, on_event)


class _Turn:
    def __init__(self, cost_usd: float = 0.0) -> None:
        self.session_id = "sess-1"
        self.rc = 0
        self.text = ""
        self.cost_usd = cost_usd
        self.error = ""


def _kickoff_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A tmp project with one real file under project/ + a stub docs recipe
    + a migrated ``state.db`` (the queue path needs the epics table)."""
    proj = tmp_path / "proj"
    home = proj / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text(
        "lanes:\n  worker: sonnet\n  reviewer: opus\n",
        encoding="utf-8",
    )
    (proj / "present.py").write_text("x", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    # Migrate the epics table — ``epics add`` needs it. ``db/init.sh`` looks
    # up ``MINI_ORK_ROOT`` for the migrations directory, so it must remain
    # unset (or set to the repo) for this call; we set it to ``proj`` AFTER
    # the migration finishes so the queue flow picks the project root.
    _init_db_via_sh(home)
    # Default the engine root to the project root so ``epics.main``'s
    # default resolution (MINI_ORK_ROOT or os.getcwd()) lands the kickoff
    # copy at ``proj/kickoffs/auto/<id>.md``, matching the display path
    # the existing tests assert. Tests that intentionally separate engine
    # root from project root override this setenv (the new test does).
    monkeypatch.setenv("MINI_ORK_ROOT", str(proj))
    engine = tmp_path / "engine"
    docs = engine / "recipes" / "docs"
    (docs / "prompts").mkdir(parents=True)
    (docs / "workflow.yaml").write_text(
        "version: '0.1.0'\ntask_class: docs\n"
        "nodes:\n"
        "  - {name: implementer, type: implementer}\n"
        "edges: []\n",
        encoding="utf-8",
    )
    (docs / "task_class.yaml").write_text(
        "name: docs\ndescription: Docs edits.\n", encoding="utf-8"
    )
    (docs / "prompts" / "editor.md").write_text("edit docs\n")
    (docs / "example-kickoff.md").write_text(
        "# Docs Edit: Title\n"
        "## Files in scope\n"
        "## Success criteria\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: engine)
    return proj, home


def _thread_agent(proj: Path, **kwargs) -> tuple[MiniOrkAcpAgent, str]:
    from unittest.mock import patch

    with patch("mini_ork.acp_orchestrator.config.orchestrator_lanes",
               return_value=[{"id": "opus", "name": "Opus"}]), \
         patch("mini_ork.recipes_catalog.list_recipes",
               return_value=[_recipe_stub("code-fix"), _recipe_stub("docs")]):
        agent = MiniOrkAcpAgent(poll_interval=0, **kwargs)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))
    return agent, resp.session_id


def _recipe_stub(recipe_id: str) -> dict:
    return {
        "id": recipe_id,
        "source": "engine",
        "path": Path(f"/tmp/{recipe_id}"),
        "summary": f"stub {recipe_id}",
        "task_class": "docs",
        "description": "",
    }


def _chunks_text(conn: _SidConn) -> str:
    """Concatenated text of every AgentMessageChunk the conn saw."""
    return " ".join(
        getattr(c.content, "text", "")
        for _, c in conn.sent
        if isinstance(c, AgentMessageChunk)
    )


# ── acceptance #1: /task in a thread returns the rewrite sentinel ───────────


def test_task_in_thread_returns_rewrite_sentinel_with_bridge(tmp_path: Path):
    """``/task [task]`` in a thread session returns the
    :class:`_RewriteToOrchestrate` sentinel with the task-intent text and
    a bridge that names Queue on board. Mirrors
    :func:`tests.unit.test_acp_commands.test_kickoff_in_thread_returns_rewrite_sentinel_with_bridge`."""
    home = tmp_path / ".mini-ork"
    home.mkdir()
    agent = _agent(home)
    try:
        out = asyncio.run(cmds.handle_task(
            agent, "run-1-abc", "queue a doc audit"))
        # The intent names the recipe and asks the orchestrator to queue,
        # not run.
        assert isinstance(out, cmds._RewriteToOrchestrate)
        assert "for the code-fix recipe" in out.intent_text
        assert "queued on the board" in out.intent_text
        assert "not run now" in out.intent_text
        assert "queue a doc audit" in out.intent_text
        assert "Follow your kickoff steps" in out.intent_text
        # The bridge tells the user the button will be Queue on board.
        assert out.bridge is not None
        assert "Queue on board" in out.bridge
        # Bare ``/task`` (no arg) still asks what the task should do.
        out_empty = asyncio.run(cmds.handle_task(agent, "run-1-abc", ""))
        assert isinstance(out_empty, cmds._RewriteToOrchestrate)
        assert "Ask what the task should do" in out_empty.intent_text
    finally:
        agent._thread_sessions.discard("run-1-abc")


# ── acceptance #2: /task in a run session is thread-only ────────────────────


def test_task_in_run_session_returns_string(tmp_path: Path):
    """``/task`` outside a thread session returns a plain string the
    dispatcher emits directly — the task queueing flow is thread-only."""
    home = tmp_path / ".mini-ork"
    home.mkdir()
    # An agent whose ``_thread_sessions`` is empty for this session id.
    agent = MiniOrkAcpAgent(home=str(home))
    agent._sessions["run-1-abc"] = str(home.parent)
    out = asyncio.run(cmds.handle_task(
        agent, "run-1-abc", "queue a doc audit"))
    assert isinstance(out, str)
    assert "Tasks are written in a mini-ork thread" in out
    assert "start one from New Thread" in out
    # The session mark is NOT set when the carve-out refuses.
    assert "run-1-abc" not in getattr(agent, "_task_queue_sessions", set())


# ── acceptance #3: /task marks the session for the queue button ─────────────


def test_task_marks_session_for_queue_button(tmp_path: Path):
    """The ``/task`` handler adds the session id to
    ``agent._task_queue_sessions``; the mark is what
    :meth:`_offer_kickoff_draft_approval` reads to swap Start run for
    Queue on board. The mark is set even when ``_task_queue_sessions``
    does not yet exist on the agent (the handler creates it via
    :func:`getattr` default ``None`` and skips the set in that case —
    verified by case #5 below, where the mark is consumed on firing)."""
    home = tmp_path / ".mini-ork"
    home.mkdir()
    agent = _agent(home)
    # Pre-condition: the mark is empty.
    assert "run-1-abc" not in agent._task_queue_sessions
    asyncio.run(cmds.handle_task(agent, "run-1-abc", "queue the audit"))
    assert "run-1-abc" in agent._task_queue_sessions
    agent._thread_sessions.discard("run-1-abc")


# ── acceptance #4: Queue on board saves + inserts an epics row ──────────────


def test_task_queues_via_epics_add_after_kickoff_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end: a ``/task`` turn followed by the orchestrator's
    ``draft_kickoff`` makes the kickoff approval offer Queue on board;
    clicking it (1) saves the staged draft to ``<home>/kickoffs/<slug>.md``
    and (2) inserts an ``epics`` row via ``epics add``. The user sees
    the epic id and a scheduler-pickup line; the staged draft is gone."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    body = (
        "# Audit Migrations\n\n"
        "## Files in scope\n"
        "- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    # Mark the session as a /task session BEFORE the prompt runs — the
    # handler's mark-set is verified in test_task_marks_session_for_queue_button.
    agent, thread = _thread_agent(proj, orchestrator_turn=_kickoff_turn(home, [], body))
    agent._task_queue_sessions.add(thread)
    conn = _SidConn()
    conn.next_choice = "queue"
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/task queue a migration audit")]))

    # The first button offered is Queue on board; Save / Change / Discard
    # keep their existing identities.
    assert conn.asked, "kickoff approval must be requested"
    tool_call_id, option_ids = conn.asked[0]
    assert option_ids[0] == "queue", f"first option must be queue, got {option_ids}"
    assert option_ids == ["queue", "save", "change", "discard"]

    # The staged draft moved to <home>/kickoffs/audit-migrations.md
    saved = home / "kickoffs" / "audit-migrations.md"
    assert saved.is_file()
    assert saved.read_text(encoding="utf-8") == body
    # The staged draft is gone.
    assert not (home / "kickoff-drafts" / "audit-migrations.md").exists()

    # The epics row landed in the board.
    con = sqlite3.connect(str(home / "state.db"))
    try:
        rows = con.execute(
            "SELECT id, title, status, kickoff_path FROM epics"
        ).fetchall()
    finally:
        con.close()
    assert rows == [(
        "audit-migrations",
        "Audit Migrations",
        "not started",
        "kickoffs/auto/audit-migrations.md",
    )]
    # The auto-copied kickoff file matches the saved one byte-for-byte.
    auto = proj / "kickoffs" / "auto" / "audit-migrations.md"
    assert auto.is_file()
    assert auto.read_text(encoding="utf-8") == body

    # The user sees the epic id + scheduler-pickup line.
    text = _chunks_text(conn)
    assert "Queued `audit-migrations`" in text
    assert "scheduler will pick it up" in text
    # The tool call is marked completed.
    completed = [
        u for _, u in conn.sent
        if isinstance(u, ToolCallProgress) and u.tool_call_id == tool_call_id
        and u.status == "completed"
    ]
    assert completed, "queue path must mark the tool call completed"

    # The session mark was consumed (consumed exactly once).
    assert thread not in agent._task_queue_sessions


# ── acceptance #5: a later /kickoff in the same thread still offers Run ─────


def test_later_kickoff_in_same_thread_still_offers_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """After a /task round-trip (queue or dismissed), the session mark is
    consumed — a subsequent ``/kickoff`` in the same thread shows Start
    run first again. The 4-button set is identical to the pre-/task
    behaviour, so the existing kickoff tests stay green."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    body = (
        "# Audit\n\n## Files in scope\n- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    # First turn: /task → queue (drains the session mark).
    agent, thread = _thread_agent(proj, orchestrator_turn=_kickoff_turn(home, [], body))
    agent._task_queue_sessions.add(thread)
    conn = _SidConn()
    conn.next_choice = "queue"
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/task queue a thing")]))
    # Mark was consumed.
    assert thread not in agent._task_queue_sessions

    # Second turn: /kickoff → expect Start run first again.
    conn2 = _SidConn()
    conn2.next_choice = None  # dismissed
    agent.on_connect(conn2)
    asyncio.run(agent.prompt(thread, [_text_block("/kickoff another")]))
    assert conn2.asked, "second kickoff must also ask for approval"
    option_ids2 = conn2.asked[0][1]
    assert option_ids2[0] == "run", (
        f"after a /task round-trip the first option must be Run, got {option_ids2}"
    )
    assert option_ids2 == ["run", "save", "change", "discard"]


# ── announcement: COMMANDS / HANDLERS surface /task ──────────────────────────


def test_task_is_announced():
    """``task`` is in the COMMANDS table and the HANDLERS dispatch table;
    the hint names what the task should do."""
    names = {c.name for c in cmds.COMMANDS}
    assert "task" in names
    assert "task" in cmds.HANDLERS
    task_cmd = next(c for c in cmds.COMMANDS if c.name == "task")
    assert task_cmd.input is not None
    assert task_cmd.input.root.hint == "what the task should do"


# ── error path: a queue failure surfaces captured output verbatim ────────────


def test_queue_failure_surfaces_epics_add_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When ``epics add`` returns a non-zero rc, the user sees the
    captured output verbatim and NO success line — fail-closed posture
    matching the kickoff §Agent save/queue spec."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    body = (
        "# Will Fail\n\n## Files in scope\n- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    agent, thread = _thread_agent(proj, orchestrator_turn=_kickoff_turn(home, [], body))
    agent._task_queue_sessions.add(thread)
    conn = _SidConn()
    conn.next_choice = "queue"
    agent.on_connect(conn)
    # Force epics.main to return rc=2 with a recognizable error message.
    def fake_main(argv, *, db=None, root=None):
        sys.stderr.write("add: --title is required\n")
        return 2
    monkeypatch.setattr(ep, "main", fake_main)
    asyncio.run(agent.prompt(thread, [_text_block("/task will fail")]))
    text = _chunks_text(conn)
    assert "`queue` failed" in text
    assert "--title is required" in text
    # No success line was emitted (no "Queued" + no "scheduler will pick it up").
    assert "scheduler will pick it up" not in text
    # The save half DID happen (the staged draft was moved before epics.add
    # ran) — the user can re-queue or run it themselves.
    saved = home / "kickoffs" / "will-fail.md"
    assert saved.is_file()
    # The session mark is still consumed — the approval fired even on error.
    assert thread not in agent._task_queue_sessions


# ── acceptance #6: /task's engine root matches the scheduler's ───────────────


def test_task_queue_uses_engine_root_resolvable_by_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``/task`` queues the draft on the engine root, not on the session
    project's parent — the same root expression the scheduler resolves
    kickoffs against. The IDE serves many projects; queueing only worked
    for those whose home happened to sit under the engine root.

    Verifies the alignment end-to-end:
      * ``MINI_ORK_ROOT`` is monkeypatched to a tmp engine root distinct
        from the session project's parent before queue runs;
      * the kickoff draft lands at the engine root (not the project root);
      * ``mini_ork.scheduler.resolve_kickoff`` (the function
        ``scheduler.run_loop`` calls) returns a path to an existing file
        whose contents equal the staged draft — proof the board row is
        runnable, not just queued.
    """
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    engine = tmp_path / "engine"  # sibling of proj, distinct root
    # Override the fixture's MINI_ORK_ROOT=proj with the engine root.
    # This MUST happen before the queue button fires — the approval
    # inside _handle_kickoff_queue is where root resolution lives.
    monkeypatch.setenv("MINI_ORK_ROOT", str(engine))

    body = (
        "# Cross-Root Audit\n\n"
        "## Files in scope\n- `present.py`\n\n"
        "## Success criteria\n- queue lands where the scheduler looks\n"
    )
    agent, thread = _thread_agent(proj, orchestrator_turn=_kickoff_turn(home, [], body))
    agent._task_queue_sessions.add(thread)
    conn = _SidConn()
    conn.next_choice = "queue"
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/task across roots")]))

    # The board row landed in the session project's state.db …
    db = str(home / "state.db")
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT id, kickoff_path FROM epics"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    epic_id, kickoff_path = rows[0]
    assert kickoff_path == f"kickoffs/auto/{epic_id}.md"

    # … and the auto-copied kickoff lives under the engine root, not
    # under the project root — provable both by filesystem and by what
    # the scheduler's resolve_kickoff returns.
    engine_auto = engine / "kickoffs" / "auto" / f"{epic_id}.md"
    proj_auto = proj / "kickoffs" / "auto" / f"{epic_id}.md"
    assert engine_auto.is_file(), (
        f"queue should land under MINI_ORK_ROOT ({engine}), not under "
        f"the project root ({proj}); got engine={engine_auto.exists()}, "
        f"project={proj_auto.exists()}"
    )
    assert engine_auto.read_text(encoding="utf-8") == body

    # The user-facing path display still anchors on home/kickoffs/<slug>
    # (the staged draft moved by _persist_kickoff_draft), independent of
    # where epics.add copied the auto version — only the board row's
    # kickoff_path is engine-relative.
    text = _chunks_text(conn)
    assert f"Queued `{epic_id}`" in text
    assert ".mini-ork/" in text  # relative to home.parent (display only)

    # The scheduler resolves the kickoff against the engine root end-to-end.
    resolved = sched.resolve_kickoff(epic_id, str(engine), "epic-runner", db)
    assert resolved is not None, "scheduler.resolve_kickoff returned None"
    resolved_path = Path(resolved)
    assert resolved_path.is_file(), f"resolved kickoff {resolved_path} not on disk"
    assert resolved_path.read_text(encoding="utf-8") == body
    assert resolved_path == engine_auto
