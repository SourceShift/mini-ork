"""Hermetic tests for the stdio ACP agent (``mini_ork.acp.agent``).

No lane, network, or subprocess launch: the launcher, reader, stopper, and
killer seams are injected as stubs so ``session/prompt`` never spawns
``bin/mini-ork`` and never touches a real state.db. The editable install
resolves ``mini_ork`` to the main checkout, so the repo root is inserted on
``sys.path`` before importing (the worktree copy must win).

The seven contract assertions (mirrored by the verifier):
  1. new_session returns a safe token; a _meta.run_id override wins;
     field_meta records the bound run id
  2. new_session launches nothing
  3. prompt launches with run_id == session_id and the flattened kickoff text
  4. two node_start + one node_end → 2 ToolCallStart + 1 ToolCallProgress
  5. D1: projecting the same snapshot twice emits no duplicate transitions
  6. UsageUpdate cost == SUM(llm_calls.cost_usd), currency == USD
  7. terminal run → "end_turn"; cancel calls stop_run(session_id)
"""
from __future__ import annotations

import asyncio

import pytest
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from acp.schema import (  # noqa: E402
    AgentMessageChunk,
    FileEditToolCallContent,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)

from mini_ork.acp.agent import MiniOrkAcpAgent, mint_run_id  # noqa: E402


@pytest.fixture(autouse=True)
def _never_launch_a_real_run(monkeypatch):
    """A test that forgets to inject ``launcher=`` would spawn a real, detached
    ``mini-ork run`` (real model calls, editing the engine checkout). Fail it."""
    def _refuse(self, run_id, kickoff_text):
        raise AssertionError(f"unit test tried to launch a real mini-ork run ({run_id})")
    monkeypatch.setattr(MiniOrkAcpAgent, "_launch", _refuse)
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.web.control import _is_safe_token  # noqa: E402


def _text_block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


def _terminal_reader(_run_id: str) -> dict:
    return {"status": "published", "events": [], "llm_calls": []}


# ── contract assertion 1 ────────────────────────────────────────────────────


def test_new_session_mints_thread_session_when_no_run_id_added():
    # Z9c-1: ``new_session`` without a client-minted run_id mints a thread
    # session (orch-<epoch>-<6 hex>). The session id is NOT a run id; the
    # thread carries config (mode / model / recipe) and starts as a member
    # of ``_thread_sessions``. The previous ``run-`` prefix assertion is
    # now split across this test (thread sessions) and the next one
    # (run sessions, via the client _meta.run_id override).
    from unittest.mock import patch

    fake_lanes = [{"id": "opus", "name": "Opus"}, {"id": "sonnet", "name": "Sonnet"}]
    fake_recipes = ["bug-audit-cmgk", "code-fix", "framework-edit"]
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        agent = MiniOrkAcpAgent()
        resp = asyncio.run(agent.new_session(cwd="/tmp/proj"))
    sid = resp.session_id
    assert sid.startswith("orch-")
    assert _is_safe_token(sid)
    assert resp.field_meta == {"kind": "thread"}
    assert agent._sessions[sid] == "/tmp/proj"
    assert sid in agent._thread_sessions
    assert agent._thread_config[sid]["mode"] == "orchestrate"
    # The picker list carries three Zed-rendered options in the mandated order.
    assert resp.config_options is not None
    assert [opt.id for opt in resp.config_options] == ["mode", "model", "recipe"]
    assert resp.config_options[0].category == "mode"
    assert resp.config_options[1].category == "model"
    assert resp.modes is not None
    assert resp.modes.current_mode_id == "orchestrate"
    assert {m.id for m in resp.modes.available_modes} == {"orchestrate", "direct"}


def test_new_session_honours_client_meta_run_id_override():
    # Run-session path is unchanged: a client-minted _meta.run_id mints a
    # ``run-<id>`` session bound to the given cwd + recipe. The session id
    # IS the run id (slice 0 contract).
    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.new_session(cwd="/tmp/proj", run_id="run-client-abc123"))
    assert resp.session_id == "run-client-abc123"
    assert _is_safe_token(resp.session_id)
    assert resp.field_meta == {"run_id": "run-client-abc123"}
    assert agent._sessions["run-client-abc123"] == "/tmp/proj"
    assert "run-client-abc123" not in agent._thread_sessions
    # Run sessions do not advertise config pickers.
    assert resp.config_options is None
    assert resp.modes is None


def test_new_session_honours_client_meta_recipe():
    agent = MiniOrkAcpAgent()
    resp = asyncio.run(
        agent.new_session(cwd="/tmp/proj", run_id="run-client-abc123", recipe="bug-audit-cmgk")
    )
    assert resp.session_id == "run-client-abc123"
    assert agent._recipes["run-client-abc123"] == "bug-audit-cmgk"


def test_mint_run_id_is_safe_token():
    for _ in range(20):
        assert _is_safe_token(mint_run_id())


# ── contract assertion 2 ────────────────────────────────────────────────────


def test_new_session_launches_nothing():
    agent = MiniOrkAcpAgent()
    asyncio.run(agent.new_session(cwd="/tmp"))
    assert agent.launch_count == 0


# ── contract assertion 3 ────────────────────────────────────────────────────


def test_prompt_launches_with_run_id_equal_to_session_id_and_flattened_text():
    calls: list[tuple[str, str]] = []

    def fake_launcher(run_id: str, text: str) -> dict:
        calls.append((run_id, text))
        return {"ok": True, "run_id": run_id}

    agent = MiniOrkAcpAgent(launcher=fake_launcher, reader=_terminal_reader)
    resp = asyncio.run(
        agent.prompt("run-1-abc", [_text_block("fix the bug"), _text_block("add a test")])
    )
    assert resp.stop_reason == "end_turn"
    assert calls == [("run-1-abc", "fix the bug\nadd a test")]
    assert agent.launch_count == 1


# ── contract assertion 4 ────────────────────────────────────────────────────


def test_events_project_to_two_starts_and_one_progress():
    agent = MiniOrkAcpAgent()
    events = [
        {"event_type": "node_start", "payload_json": json.dumps({"node_id": "n1", "node_type": "planner"})},
        {"event_type": "node_start", "payload_json": json.dumps({"node_id": "n2", "node_type": "implementer"})},
        {"event_type": "node_end", "payload_json": json.dumps({"node_id": "n1", "node_type": "planner"})},
    ]
    updates = agent._build_event_updates(events)
    starts = [u for u in updates if isinstance(u, ToolCallStart)]
    progress = [u for u in updates if isinstance(u, ToolCallProgress)]
    assert len(starts) == 2
    assert len(progress) == 1
    assert progress[0].tool_call_id == "n1"
    assert progress[0].status == "completed"


# ── contract assertion 5 (D1 regression) ────────────────────────────────────


def test_projecting_same_snapshot_twice_emits_no_duplicate_transitions():
    agent = MiniOrkAcpAgent()
    emitted_updates: list = []

    class _FakeConn:
        async def session_update(self, session_id: str, update) -> None:
            emitted_updates.append(update)

    agent.on_connect(_FakeConn())
    events = [
        {"event_type": "node_start", "payload_json": json.dumps({"node_id": "n1", "node_type": "planner"})},
        {"event_type": "node_end", "payload_json": json.dumps({"node_id": "n1", "node_type": "planner"})},
    ]
    snapshot = {"status": None, "events": events, "llm_calls": []}
    asyncio.run(agent._project_snapshot("run-1", snapshot))
    asyncio.run(agent._project_snapshot("run-1", snapshot))
    starts = [u for u in emitted_updates if isinstance(u, ToolCallStart)]
    progress = [u for u in emitted_updates if isinstance(u, ToolCallProgress)]
    assert len(starts) == 1
    assert len(progress) == 1


# ── contract assertion 6 ────────────────────────────────────────────────────


def test_usage_update_cost_equals_sum_of_llm_calls():
    agent = MiniOrkAcpAgent()
    llm_calls = [
        {"cost_usd": 1.25, "total_tokens": 100},
        {"cost_usd": 0.75, "total_tokens": 50},
    ]
    update = agent._build_usage_update(llm_calls)
    assert update.cost is not None
    assert update.cost.amount == 2.0
    assert update.cost.currency == "USD"
    assert update.used == 150


# ── contract assertion 7 ────────────────────────────────────────────────────


def test_prompt_returns_end_turn_on_terminal_run():
    def fake_launcher(run_id: str, text: str) -> dict:
        del text
        return {"ok": True, "run_id": run_id}

    agent = MiniOrkAcpAgent(launcher=fake_launcher, reader=_terminal_reader)
    resp = asyncio.run(agent.prompt("run-1-abc", [_text_block("do it")]))
    assert resp.stop_reason == "end_turn"


def test_cancel_calls_stop_run_with_session_id():
    stopped: list[str] = []

    def fake_stopper(run_id: str) -> dict:
        stopped.append(run_id)
        return {"ok": True}

    agent = MiniOrkAcpAgent(stopper=fake_stopper, cancel_grace=0)
    asyncio.run(agent.cancel("run-1-abc"))
    assert stopped == ["run-1-abc"]


# ── auxiliary ───────────────────────────────────────────────────────────────


def test_initialize_returns_protocol_version():
    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.initialize(protocol_version=1))
    assert resp.protocol_version == 1
    assert resp.agent_info is not None
    assert resp.agent_info.name == "mini-ork-acp"


def test_prompt_refuses_unsafe_session_id():
    def should_not_launch(_run_id: str, _text: str) -> dict:
        raise AssertionError("launcher must not be called for an unsafe session id")

    agent = MiniOrkAcpAgent(launcher=should_not_launch)
    resp = asyncio.run(agent.prompt("../etc", [_text_block("x")]))
    assert resp.stop_reason == "refusal"
    assert agent.launch_count == 0


def test_prompt_polls_until_terminal():
    states = iter(["executing", "published"])

    def fake_launcher(run_id: str, text: str) -> dict:
        del text
        return {"ok": True, "run_id": run_id}

    def fake_reader(_run_id: str) -> dict:
        return {"status": next(states), "events": [], "llm_calls": []}

    agent = MiniOrkAcpAgent(launcher=fake_launcher, reader=fake_reader, poll_interval=0)
    resp = asyncio.run(agent.prompt("run-1-abc", [_text_block("do it")]))
    assert resp.stop_reason == "end_turn"


def test_acp_registered_in_subcommand_registry():
    from mini_ork.cli.main import SUBCOMMAND_REGISTRY

    assert "acp" in SUBCOMMAND_REGISTRY


def test_acp_cmd_import_prints_nothing_to_stdout():
    code = "import mini_ork.cli.acp_cmd; import mini_ork.cli.main"
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert out.stdout == ""


# ── start-detection (dead-launcher probe + start-timeout) ──────────────────


def _capturing_conn() -> tuple[list, Any]:
    """Build a fake ACP connection that records every session_update push."""

    class _FakeConn:
        async def session_update(self, _sid: str, update) -> None:
            captured.append(update)

    captured: list = []
    return captured, _FakeConn()


def test_prompt_refuses_when_launcher_exits_before_publishing(tmp_path):
    # A child that exits immediately so waitpid returns (pid, status).
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()

    log_path = tmp_path / "launch.log"
    log_path.write_text("alpha line\nbravo line\ncharlie line\n", encoding="utf-8")

    def fake_launcher(run_id: str, text: str) -> dict:
        del text
        return {"ok": True, "run_id": run_id, "pid": dead.pid, "log_path": str(log_path)}

    def always_none(_run_id: str) -> dict:
        return {"status": None, "events": [], "llm_calls": []}

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent(
        launcher=fake_launcher,
        reader=always_none,
        poll_interval=0,
    )
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt("run-dead-1", [_text_block("do it")]))

    assert resp.stop_reason == "refusal", resp
    chunks = [u for u in captured if isinstance(u, AgentMessageChunk)]
    assert chunks, f"expected an agent_message_chunk emission, got {captured!r}"
    text = chunks[0].content.text
    assert "failed to start" in text, text
    assert "alpha line" in text and "bravo line" in text and "charlie line" in text, text


def test_prompt_refuses_on_start_timeout_when_launcher_lingers():
    # No pid in the launcher result → dead-launcher probe is skipped; the
    # start-timeout is the only safety net.
    def fake_launcher(run_id: str, text: str) -> dict:
        del text
        return {"ok": True, "run_id": run_id}

    def always_none(_run_id: str) -> dict:
        return {"status": None, "events": [], "llm_calls": []}

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent(
        launcher=fake_launcher,
        reader=always_none,
        poll_interval=0.01,
        start_timeout=0.2,
    )
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt("run-slow-1", [_text_block("do it")]))

    assert resp.stop_reason == "refusal", resp
    chunks = [u for u in captured if isinstance(u, AgentMessageChunk)]
    assert chunks, f"expected an agent_message_chunk emission, got {captured!r}"
    assert "did not start within" in chunks[0].content.text, chunks[0].content.text


def test_prompt_end_turn_with_live_launcher_pid():
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        def fake_launcher(run_id: str, text: str) -> dict:
            del text
            return {"ok": True, "run_id": run_id, "pid": live.pid}

        states = iter(["executing", "published"])

        def fake_reader(_run_id: str) -> dict:
            try:
                return {"status": next(states), "events": [], "llm_calls": []}
            except StopIteration:
                return {"status": "published", "events": [], "llm_calls": []}

        agent = MiniOrkAcpAgent(
            launcher=fake_launcher,
            reader=fake_reader,
            poll_interval=0.01,
        )
        resp = asyncio.run(agent.prompt("run-live-1", [_text_block("do it")]))
        assert resp.stop_reason == "end_turn", resp
    finally:
        live.terminate()
        try:
            live.wait(timeout=2)
        except subprocess.TimeoutExpired:
            live.kill()
            live.wait()


# ── session/list + session/load (Zed Z1+Z2) ─────────────────────────────────


def _migrate_home(tmp_path, name: str = "proj") -> tuple[Path, Path]:
    """Build a project with ``.mini-ork/state.db`` migrated; return (proj, home)."""
    proj = tmp_path / name
    home = proj / ".mini-ork"
    home.mkdir(parents=True)
    rc, out, err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return proj, home


def _seed_run(
    home: Path,
    run_id: str,
    *,
    status: str = "published",
    created_at: int = 1000,
    kickoff_text: str | None = None,
) -> Path:
    inbox = home / "runs-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    kickoff_path = inbox / f"{run_id}.md"
    if kickoff_text is not None:
        kickoff_path.write_text(kickoff_text, encoding="utf-8")
    con = sqlite3.connect(str(home / "state.db"))
    try:
        con.execute(
            """
            INSERT INTO task_runs
              (id, task_class, recipe, kickoff_path, status, cost_usd, created_at, updated_at)
            VALUES (?, 'code_fix', 'code-fix', ?, ?, 0.5, ?, ?)
            """,
            (run_id, str(kickoff_path), status, created_at, created_at + 100),
        )
        con.commit()
    finally:
        con.close()
    return kickoff_path


def _seed_node_event(home: Path, run_id: str, node_id: str) -> None:
    con = sqlite3.connect(str(home / "state.db"))
    try:
        con.execute(
            """
            INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at)
            VALUES (?, ?, 'node_start', ?, 1500)
            """,
            (f"{run_id}-{node_id}", run_id, json.dumps({"node_id": node_id, "node_type": "planner"})),
        )
        con.commit()
    finally:
        con.close()


def test_initialize_advertises_load_session_and_session_list():
    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.initialize(protocol_version=1))
    caps = resp.agent_capabilities
    assert caps is not None
    assert caps.load_session is True
    assert caps.session_capabilities is not None
    assert caps.session_capabilities.list is not None


def test_list_sessions_resolves_project_home_and_maps_rows(tmp_path):
    proj, home = _migrate_home(tmp_path)
    _seed_run(home, "run-1", kickoff_text="# A past run\nbody")

    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.list_sessions(cwd=str(proj)))

    assert len(resp.sessions) == 1
    info = resp.sessions[0]
    assert info.session_id == "run-1"
    assert info.cwd == str(proj.resolve())
    assert info.title == "A past run"
    assert info.updated_at is not None
    assert info.field_meta == {"status": "published", "recipe": "code-fix", "cost_usd": 0.5}
    assert resp.next_cursor is None


def test_list_sessions_cursor_round_trips(tmp_path):
    proj, home = _migrate_home(tmp_path)
    con = sqlite3.connect(str(home / "state.db"))
    try:
        for i in range(51):
            con.execute(
                """
                INSERT INTO task_runs
                  (id, task_class, recipe, kickoff_path, status, cost_usd, created_at, updated_at)
                VALUES (?, 'code_fix', 'code-fix', ?, 'published', 0.5, ?, ?)
                """,
                (f"run-{i}", f"run-{i}.md", 1000 + i, 2000 + i),
            )
        con.commit()
    finally:
        con.close()

    agent = MiniOrkAcpAgent()
    page1 = asyncio.run(agent.list_sessions(cwd=str(proj)))
    assert len(page1.sessions) == 50
    assert page1.next_cursor == "50"
    page2 = asyncio.run(agent.list_sessions(cwd=str(proj), cursor="50"))
    assert len(page2.sessions) == 1
    assert page2.sessions[0].session_id == "run-0"
    assert page2.next_cursor is None


def test_list_sessions_invalid_cursor_treated_as_zero(tmp_path):
    proj, home = _migrate_home(tmp_path)
    _seed_run(home, "run-1")
    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.list_sessions(cwd=str(proj), cursor="not-a-number"))
    assert len(resp.sessions) == 1


def test_list_sessions_missing_home_returns_empty(tmp_path):
    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.list_sessions(cwd=str(tmp_path / "no-such-proj")))
    assert resp.sessions == []


def test_load_session_finished_run_emits_kickoff_events_and_terminal(tmp_path):
    proj, home = _migrate_home(tmp_path)
    _seed_run(home, "run-1", status="published", kickoff_text="# My kickoff\nbody")
    _seed_node_event(home, "run-1", "n1")

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent()
    agent.on_connect(conn)
    resp = asyncio.run(agent.load_session(cwd=str(proj), session_id="run-1"))

    assert resp is not None
    kinds = [u.session_update for u in captured]
    assert kinds[0] == "user_message_chunk"
    assert captured[0].content.text.startswith("# My kickoff")
    assert "tool_call" in kinds  # ToolCallStart for the node_start event
    assert "usage_update" in kinds
    assert "agent_message_chunk" in kinds  # terminal message
    terminal = [u for u in captured if isinstance(u, AgentMessageChunk)][0]
    assert "published" in terminal.content.text
    assert "run-1" not in agent._followers  # finished → no follower


def test_load_session_running_run_starts_follower_until_terminal(tmp_path):
    proj, home = _migrate_home(tmp_path)
    _seed_run(home, "run-1", status="executing", kickoff_text="# Running\n")
    _seed_node_event(home, "run-1", "n1")

    captured, conn = _capturing_conn()
    states = iter(["executing", "published"])

    def fake_reader(_run_id: str) -> dict:
        status = next(states, "published")
        events = (
            [
                {
                    "event_type": "node_end",
                    "payload_json": json.dumps({"node_id": "n1", "node_type": "planner"}),
                }
            ]
            if status == "published"
            else []
        )
        return {"status": status, "events": events, "llm_calls": []}

    agent = MiniOrkAcpAgent(reader=fake_reader, poll_interval=0)
    agent.on_connect(conn)

    async def _drive() -> None:
        resp = await agent.load_session(cwd=str(proj), session_id="run-1")
        assert resp is not None
        assert "run-1" in agent._followers
        await agent._followers["run-1"]

    asyncio.run(_drive())

    kinds = [u.session_update for u in captured]
    assert "tool_call_update" in kinds  # node_end projected by the follower
    terminal = [u for u in captured if isinstance(u, AgentMessageChunk)]
    assert any("published" in u.content.text for u in terminal)


def test_load_session_rejects_unsafe_session_id(tmp_path):
    proj, _ = _migrate_home(tmp_path)
    agent = MiniOrkAcpAgent()
    try:
        asyncio.run(agent.load_session(cwd=str(proj), session_id="../etc"))
    except Exception as exc:  # acp.RequestError is a plain Exception
        assert getattr(exc, "code", None) == -32602
    else:
        raise AssertionError("load_session must reject an unsafe session id")


def test_load_session_unknown_run_raises_invalid_params(tmp_path):
    proj, _ = _migrate_home(tmp_path)
    agent = MiniOrkAcpAgent()
    try:
        asyncio.run(agent.load_session(cwd=str(proj), session_id="run-nope"))
    except Exception as exc:
        assert getattr(exc, "code", None) == -32602
        assert "no mini-ork run" in str(getattr(exc, "data", None))
    else:
        raise AssertionError("load_session must reject a run with no task_runs row")


def test_prompt_on_loaded_session_never_launches(tmp_path):
    proj, home = _migrate_home(tmp_path)
    _seed_run(home, "run-1", status="published", kickoff_text="# Done\n")

    def should_not_launch(_run_id: str, _text: str) -> dict:
        raise AssertionError("launcher must not be called for a loaded session")

    agent = MiniOrkAcpAgent(launcher=should_not_launch)
    asyncio.run(agent.load_session(cwd=str(proj), session_id="run-1"))
    assert agent.launch_count == 0

    captured, conn = _capturing_conn()
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt("run-1", [_text_block("do it")]))
    assert resp.stop_reason == "end_turn"
    assert agent.launch_count == 0
    chunks = [u for u in captured if isinstance(u, AgentMessageChunk)]
    assert any("finished run" in u.content.text for u in chunks)


def test_home_for_session_cwd_wins(tmp_path):
    proj, home = _migrate_home(tmp_path)
    agent = MiniOrkAcpAgent(home=str(tmp_path / "other-home"))
    agent._sessions["run-1"] = str(proj)
    assert agent._home_for("run-1") == home


def test_home_for_falls_back_to_constructor_home(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    other = tmp_path / "other-home"
    agent = MiniOrkAcpAgent(home=str(other))
    # A session cwd with no .mini-ork dir falls through to the constructor home.
    agent._sessions["run-1"] = str(tmp_path / "no-home")
    assert agent._home_for("run-1") == Path(other)


def test_home_for_env_precedence(tmp_path, monkeypatch):
    env_home = tmp_path / "env-home"
    monkeypatch.setenv("MINI_ORK_HOME", str(env_home))
    agent = MiniOrkAcpAgent()
    assert agent._home_for(None) == Path(env_home)


# ── Z3 live sidecar projection ─────────────────────────────────────────────


class _FakeLiveConn:
    """Capturing ACP connection for live projection tests."""

    def __init__(self) -> None:
        self.captured: list = []

    async def session_update(self, session_id: str, update) -> None:
        del session_id
        self.captured.append(update)


def _write_live_lines(path: Path, lines: list[str]) -> None:
    """Append one ``LiveWriter``-shaped envelope per ``lines`` entry."""
    from mini_ork.dispatch.live_stream import LiveWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    with LiveWriter(str(path)) as writer:
        for i, line in enumerate(lines):
            writer.write_line(line, "stdout", partial=False)


def test_running_session_drains_new_live_lines_each_poll(tmp_path):
    """A running session whose run dir gains lines in
    ``agent-<node>.live.jsonl`` between polls emits ``ToolCallProgress`` with
    exactly the new text (and an ``AgentThoughtChunk`` for a thinking block);
    nothing is re-sent on the next poll.

    Drives the agent through the real ``_await_terminal`` loop with a
    counter-driven fake reader that writes fresh live content between calls
    (the writer runs INSIDE the reader, so ``_project_snapshot``'s drain
    sees the just-written bytes — the same timing a real dispatch produces).
    """
    home = tmp_path / ".mini-ork"
    run_id = "run-live-001"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    live_path = run_dir / "agent-implementer.live.jsonl"

    call = {"n": 0}

    def fake_reader(_run_id: str) -> dict:
        call["n"] += 1
        # Poll 1: emit node_start for implementer + write first live chunk.
        # Poll 2: write second batch (text + thinking) and stay running.
        # Poll 3: stay running, no new live content (D1 reuse assertion).
        # Poll 4: terminal — last drain still picks up nothing new.
        if call["n"] == 1:
            _write_live_lines(live_path, [json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "first chunk"}]},
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
            _write_live_lines(live_path, [json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "thinking", "thinking": "pondering"},
                    {"type": "text", "text": "second chunk"},
                ]},
            })])
            return {"status": "executing", "events": [], "llm_calls": []}
        if call["n"] == 3:
            # No new live lines; status still executing → drain emits nothing.
            return {"status": "executing", "events": [], "llm_calls": []}
        # Poll 4: terminal.
        return {"status": "published", "events": [], "llm_calls": []}

    conn = _FakeLiveConn()
    agent = MiniOrkAcpAgent(home=home, reader=fake_reader, poll_interval=0,
                            launcher=lambda _rid, _kick: {"ok": True})
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt(run_id, [_text_block("go")]))
    assert resp.stop_reason == "end_turn"

    # Lifecycle: one ToolCallStart + one ToolCallProgress(terminal) + usage +
    # terminal message.
    starts = [u for u in conn.captured if isinstance(u, ToolCallStart)]
    assert len(starts) == 1
    assert starts[0].tool_call_id == "implementer"

    # Live chunks: poll 1 emits 1 text chunk; poll 2 emits 1 thought + 1 text.
    thoughts = [u for u in conn.captured if isinstance(u, AgentMessageChunk)]
    text_progress = [
        u for u in conn.captured
        if isinstance(u, ToolCallProgress) and u.content is not None
    ]
    # The terminal ToolCallProgress has content=None — exclude it.
    progress_with_text = [u for u in text_progress if u.content]

    assert len(progress_with_text) == 2, [str(u) for u in conn.captured]
    texts: list[str] = []
    for u in progress_with_text:
        if u.content is None:
            continue
        block = u.content[0]
        texts.append(block.content.text)
    assert texts == ["first chunk", "second chunk"]

    # Thought: must be the AgentMessageChunk-shaped AgentThoughtChunk that
    # carries the "[implementer] pondering" prefix.
    assert len(thoughts) == 1, [str(u) for u in conn.captured]
    # The agent_message_chunk is the terminal; the thought is a separate update.
    # Filter AgentMessageChunk that carries the terminal text vs. the thought.
    terminal_chunks = [
        u for u in thoughts
        if isinstance(u, AgentMessageChunk)
        and "mini-ork run finished" in (u.content.text or "")
    ]
    assert len(terminal_chunks) == 1
    # The AgentThoughtChunk for the live thought chunk uses a different type.
    from acp.schema import AgentThoughtChunk
    live_thoughts = [u for u in conn.captured if isinstance(u, AgentThoughtChunk)]
    assert len(live_thoughts) == 1
    assert live_thoughts[0].content.text == "[implementer] pondering"

    # D1 for live events: tail.read_new() advances offset across polls, so a
    # poll with no new content emits zero new ToolCallProgress chunks beyond
    # the terminal one. Verify by counting the per-node live emissions:
    # exactly 2 text chunks + 1 thought, no duplicates.
    assert len(progress_with_text) == 2
    assert len(live_thoughts) == 1


def test_load_session_replays_at_most_50_live_events_per_node(tmp_path):
    """``session/load`` of a finished run replays at most the last 50
    normalized events per node (kickoff §3).

    Writes 80 records to a node's live sidecar, seeds a published task_run
    row + a ``node_start`` event, and asserts the load emits ≤50
    ``ToolCallProgress`` chunks for the node (text + tool_output + tool all
    count as normalized events).
    """
    proj, home = _migrate_home(tmp_path)
    run_id = "run-replay-001"
    _seed_run(home, run_id, status="published", kickoff_text="# Replay")
    _seed_node_event(home, run_id, "implementer")

    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    live_path = run_dir / "agent-implementer.live.jsonl"

    from mini_ork.acp.agent import _LIVE_REPLAY_LIMIT
    from mini_ork.dispatch.live_stream import LiveWriter

    with LiveWriter(str(live_path)) as writer:
        for i in range(80):
            writer.write_line(
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [
                        {"type": "text", "text": f"chunk-{i:03d}"},
                    ]},
                }),
                "stdout",
            )

    conn = _FakeLiveConn()
    agent = MiniOrkAcpAgent(home=home, poll_interval=0)
    agent.on_connect(conn)
    asyncio.run(agent.load_session(str(proj), run_id))

    # Count normalized-event TextProgress chunks for "implementer".
    text_chunks = [
        u for u in conn.captured
        if isinstance(u, ToolCallProgress)
        and u.content is not None
        and u.tool_call_id == "implementer"
    ]
    assert len(text_chunks) == _LIVE_REPLAY_LIMIT, (
        f"expected at most {_LIVE_REPLAY_LIMIT} replays, got {len(text_chunks)}"
    )
    # The cap is "last 50 normalized events" — the replayed texts are the
    # last 50 ("chunk-030" through "chunk-079"), oldest 30 dropped.
    texts: list[str] = []
    for u in text_chunks:
        if u.content is None:
            continue
        texts.append(u.content[0].content.text)
    assert texts[0] == "chunk-030"
    assert texts[-1] == "chunk-079"


# ── thread-session shape (Z9c-1) ──────────────────────────────────────────


class _CapturingConn:
    """Minimal ACP connection: capture every ``session_update`` push."""

    def __init__(self) -> None:
        self.captured: list = []

    async def session_update(self, _sid: str, update) -> None:
        self.captured.append(update)


def _make_thread_agent(tmp_path, *, monkeypatch=None, **ctor_kwargs):
    """Build an agent pre-populated with a thread session.

    Patches ``orchestrator_lanes`` + ``list_recipes`` so the picker is
    deterministic, then mints one thread session for ``tmp_path / proj``.
    Returns ``(agent, conn, thread_id)``.
    """
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    fake_lanes = [{"id": "opus", "name": "Opus"}, {"id": "sonnet", "name": "Sonnet"}]
    fake_recipes = ["code-fix", "framework-edit"]
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        agent = MiniOrkAcpAgent(**ctor_kwargs)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))
    conn = _CapturingConn()
    agent.on_connect(conn)
    return agent, conn, resp.session_id


def test_set_config_option_stores_and_returns_full_list(tmp_path):
    from unittest.mock import patch
    from acp.schema import ConfigOptionUpdate

    agent, conn, sid = _make_thread_agent(tmp_path)
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=[{"id": "opus", "name": "Opus"}, {"id": "sonnet", "name": "Sonnet"}],
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=["code-fix", "framework-edit"],
    ):
        resp = asyncio.run(agent.set_config_option("model", sid, "sonnet"))
    assert resp.config_options is not None
    assert [opt.id for opt in resp.config_options] == ["mode", "model", "recipe"]
    assert resp.config_options[1].current_value == "sonnet"
    assert agent._thread_config[sid]["model"] == "sonnet"
    # The emission lands on the wire.
    updates = [
        u for u in conn.captured
        if isinstance(u, ConfigOptionUpdate) and u.session_update == "config_option_update"
    ]
    assert len(updates) == 1
    assert updates[0].config_options[1].current_value == "sonnet"


def test_set_config_option_rejects_unknown_id(tmp_path):
    agent, _conn, sid = _make_thread_agent(tmp_path)
    try:
        asyncio.run(agent.set_config_option("bogus", sid, "x"))
    except Exception as exc:
        assert getattr(exc, "code", None) == -32602
    else:
        raise AssertionError("expected invalid_params")


def test_set_config_option_rejects_unknown_value(tmp_path):
    agent, _conn, sid = _make_thread_agent(tmp_path)
    try:
        asyncio.run(agent.set_config_option("mode", sid, "hyperdrive"))
    except Exception as exc:
        assert getattr(exc, "code", None) == -32602
        assert "unknown mode" in str(getattr(exc, "data", None))
    else:
        raise AssertionError("expected invalid_params")


def test_set_config_option_rejects_non_thread_session(tmp_path):
    # A run-id is not a thread session; set_config_option must refuse it.
    agent = MiniOrkAcpAgent()
    try:
        asyncio.run(agent.set_config_option("mode", "run-1-abc", "orchestrate"))
    except Exception as exc:
        assert getattr(exc, "code", None) == -32602
    else:
        raise AssertionError("expected invalid_params on non-thread session")


def test_set_session_mode_routes_to_set_config_option(tmp_path):
    from unittest.mock import patch

    agent, _conn, sid = _make_thread_agent(tmp_path)
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=[{"id": "opus", "name": "Opus"}],
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=["code-fix"],
    ):
        asyncio.run(agent.set_session_mode("direct", sid))
    assert agent._thread_config[sid]["mode"] == "direct"


def test_orchestrate_prompt_streams_events_and_stores_resume(tmp_path):
    from unittest.mock import patch
    from acp.schema import AgentMessageChunk, AgentThoughtChunk

    proj = tmp_path / "proj"
    proj.mkdir()
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    # The fake orchestrator_turn pushes two envelopes before returning.
    envelopes = [
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}},
    ]

    class _TurnResult:
        session_id = "claude-sess-001"
        rc = 0
        text = "answer"
        cost_usd = 0.0
        error = ""

    seen: dict[str, Any] = {"lane": None, "resume": None, "on_event": None}

    def fake_turn(lane, prompt, cwd, home, resume, on_event):
        seen["lane"] = lane
        seen["resume"] = resume
        seen["on_event"] = on_event

        async def _run() -> Any:
            for env in envelopes:
                await on_event(env)
            return _TurnResult()

        return _run()

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        agent = MiniOrkAcpAgent(orchestrator_turn=fake_turn)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))

    conn = _CapturingConn()
    agent.on_connect(conn)
    turn_resp = asyncio.run(agent.prompt(resp.session_id, [_text_block("hi")]))

    assert turn_resp.stop_reason == "end_turn"
    # The chosen lane + the resumed session id both made it to the seam.
    assert seen["lane"] == "opus"
    assert seen["resume"] is None  # first turn — no resume yet
    # The persisted resume id is now ready for the next turn.
    assert agent._thread_config[resp.session_id]["claude_session_id"] == "claude-sess-001"
    # The two envelopes produced one thought + one message.
    thoughts = [u for u in conn.captured if isinstance(u, AgentThoughtChunk)]
    messages = [u for u in conn.captured if isinstance(u, AgentMessageChunk)]
    assert len(thoughts) == 1 and thoughts[0].content.text == "hmm"
    assert any(m.content.text == "answer" for m in messages)


def test_orchestrate_prompt_resumes_with_persisted_session_id(tmp_path):
    """Second turn passes the persisted claude session id as ``resume``."""
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    class _TurnResult:
        def __init__(self, sess: str) -> None:
            self.session_id = sess
            self.rc = 0
            self.text = ""
            self.cost_usd = 0.0
            self.error = ""

    seen: list[Any] = []

    def fake_turn(lane, prompt, cwd, home, resume, on_event):
        async def _run() -> Any:
            seen.append(resume)
            return _TurnResult("claude-sess-002")

        return _run()

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        agent = MiniOrkAcpAgent(orchestrator_turn=fake_turn)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))

    asyncio.run(agent.prompt(resp.session_id, [_text_block("first")]))
    # First turn observed no resume; the agent now stores the returned id.
    assert seen[-1] is None
    asyncio.run(agent.prompt(resp.session_id, [_text_block("second")]))
    # Second turn passes the persisted id.
    assert seen[-1] == "claude-sess-002"


def test_orchestrate_prompt_uses_chosen_lane(tmp_path):
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    fake_lanes = [
        {"id": "opus", "name": "Opus"},
        {"id": "sonnet", "name": "Sonnet"},
        {"id": "minimax", "name": "MiniMax-M3"},
    ]
    fake_recipes = ["code-fix"]

    class _TurnResult:
        session_id = "sess-1"
        rc = 0
        text = ""
        cost_usd = 0.0
        error = ""

    seen: list[str] = []

    def fake_turn(lane, prompt, cwd, home, resume, on_event):
        async def _run() -> Any:
            seen.append(lane)
            return _TurnResult()

        return _run()

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        agent = MiniOrkAcpAgent(orchestrator_turn=fake_turn)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))

    # Switch the model via set_config_option and observe the seam picks it up.
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        asyncio.run(agent.set_config_option("model", resp.session_id, "sonnet"))
    asyncio.run(agent.prompt(resp.session_id, [_text_block("hi")]))
    assert seen == ["sonnet"]


def test_start_run_tool_result_makes_thread_follow_child_run(tmp_path):
    """An orchestrator turn whose tool_result answers ``start_run`` spawns a
    follower for the child run under prefixed tool ids."""
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    child_run_id = "run-child-001"

    # Two readers so the child follow sees terminal after a couple polls.
    call = {"n": 0}

    def fake_reader(rid: str) -> dict:
        if rid == child_run_id:
            call["n"] += 1
            if call["n"] >= 2:
                return {
                    "status": "published",
                    "events": [
                        {
                            "event_type": "node_start",
                            "payload_json": json.dumps(
                                {"node_id": "n1", "node_type": "planner"}
                            ),
                        }
                    ],
                    "llm_calls": [],
                }
            return {"status": "executing", "events": [], "llm_calls": []}
        return {"status": None, "events": [], "llm_calls": []}

    class _TurnResult:
        session_id = "sess-1"
        rc = 0
        text = ""
        cost_usd = 0.0
        error = ""

    def fake_turn(lane, prompt, cwd, home, resume, on_event):
        async def _run() -> Any:
            # Assistant tool_use for the start_run tool...
            await on_event({
                "type": "assistant",
                "message": {"content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "mcp__mini-ork__start_run",
                        "input": {"recipe": "code-fix"},
                    }
                ]},
            })
            # ...answered by a tool_result with a JSON run_id.
            await on_event({
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "is_error": False,
                    "content": [{"type": "text", "text": json.dumps(
                        {"run_id": child_run_id}
                    )}],
                }]},
            })
            return _TurnResult()

        return _run()

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        agent = MiniOrkAcpAgent(
            orchestrator_turn=fake_turn,
            reader=fake_reader,
            poll_interval=0,
        )
        resp = asyncio.run(agent.new_session(cwd=str(proj)))

    conn = _CapturingConn()
    agent.on_connect(conn)
    turn_resp = asyncio.run(agent.prompt(resp.session_id, [_text_block("launch a run")]))
    assert turn_resp.stop_reason == "end_turn"

    # The child follower's parent marker landed on the wire with a prefixed id.
    parent_markers = [
        u for u in conn.captured
        if isinstance(u, ToolCallStart) and u.tool_call_id == f"{child_run_id}:parent"
    ]
    assert len(parent_markers) == 1
    assert "code-fix" in parent_markers[0].title


def test_only_start_run_results_start_a_child_run(tmp_path):
    """run_status / wait_for_run / run_detail results also carry a run_id; they
    must not make the thread follow that run. A start_run result must, with the
    recipe the orchestrator chose in the marker title."""
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()

    class _TurnResult:
        session_id = "sess-1"
        rc = 0
        text = ""
        cost_usd = 0.0
        error = ""

    def _pair(tool_id, name, inp, run_id):
        return [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}},
            {"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": tool_id, "is_error": False,
                "content": [{"type": "text", "text": json.dumps({"run_id": run_id})}]}]}},
        ]

    def fake_turn(lane, prompt, cwd, home, resume, on_event):
        async def _run():
            events = (_pair("t1", "mcp__mini-ork__run_status", {"run_id": "run-old-001"}, "run-old-001")
                      + _pair("t2", "mcp__mini-ork__wait_for_run", {"run_id": "run-old-001"}, "run-old-001")
                      + _pair("t3", "mcp__mini-ork__start_run", {"recipe": "docs"}, "run-new-001"))
            for ev in events:
                await on_event(ev)
            return _TurnResult()
        return _run()

    def fake_reader(rid):
        return {"status": "published", "events": [], "llm_calls": []}

    with patch("mini_ork.acp_orchestrator.config.orchestrator_lanes",
               return_value=[{"id": "opus", "name": "Opus"}]), \
         patch("mini_ork.web.recipes.list_recipes", return_value=["code-fix", "docs"]):
        agent = MiniOrkAcpAgent(orchestrator_turn=fake_turn, reader=fake_reader, poll_interval=0)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))
    conn = _CapturingConn()
    agent.on_connect(conn)
    asyncio.run(agent.prompt(resp.session_id, [_text_block("go")]))
    markers = [u for u in conn.captured
               if isinstance(u, ToolCallStart) and str(u.tool_call_id).endswith(":parent")]
    assert [m.tool_call_id for m in markers] == ["run-new-001:parent"]
    assert "docs" in markers[0].title


def test_direct_mode_launches_with_selected_recipe(tmp_path):
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix", "framework-edit"]

    launched: list[tuple[str, str, str]] = []  # (run_id, kickoff, recipe)

    def fake_reader(rid: str) -> dict:
        # Only the fresh run sees a row; the thread session id is not in the db.
        if not rid.startswith("run-"):
            return {"status": None, "events": [], "llm_calls": []}
        return {"status": "published", "events": [], "llm_calls": []}

    def fake_turn(lane, prompt, cwd, home, resume, on_event):
        raise AssertionError("orchestrator_turn must not run in direct mode")

    agent = MiniOrkAcpAgent(
        orchestrator_turn=fake_turn,
        reader=fake_reader,
        poll_interval=0,
    )

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        resp = asyncio.run(agent.new_session(cwd=str(proj)))
        sid = resp.session_id
        # Switch to direct mode with the framework-edit recipe.
        asyncio.run(agent.set_config_option("mode", sid, "direct"))
        asyncio.run(agent.set_config_option("recipe", sid, "framework-edit"))

    # Patch the bound _launch to capture (run_id, kickoff, recipe) — _launch
    # reads from ``_sessions`` / ``_recipes`` so we can detect the right one.
    original_launch = MiniOrkAcpAgent._launch

    def captured_launch(self, run_id, kickoff_text):
        launched.append((run_id, kickoff_text, self._recipes.get(run_id) or ""))
        return {"ok": True, "run_id": run_id}

    MiniOrkAcpAgent._launch = captured_launch  # type: ignore[method-assign]
    try:
        turn_resp = asyncio.run(agent.prompt(sid, [_text_block("do it")]))
    finally:
        MiniOrkAcpAgent._launch = original_launch  # type: ignore[method-assign]

    assert turn_resp.stop_reason == "end_turn"
    assert len(launched) == 1
    assert launched[0][2] == "framework-edit"
    assert launched[0][0].startswith("run-")


def test_slash_run_in_orchestrate_mode_launches_directly(tmp_path):
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    seen_turn = {"called": False}

    def fake_turn(lane, prompt, cwd, home, resume, on_event):
        async def _run() -> Any:
            seen_turn["called"] = True
            return _TurnResult()  # noqa: F821 — defined below

        return _run()

    class _TurnResult:
        session_id = "sess-1"
        rc = 0
        text = ""
        cost_usd = 0.0
        error = ""

    launched: list[tuple[str, str]] = []

    agent = MiniOrkAcpAgent(
        orchestrator_turn=fake_turn,
        reader=lambda _: {"status": "published", "events": [], "llm_calls": []},
        poll_interval=0,
    )

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        resp = asyncio.run(agent.new_session(cwd=str(proj)))
        sid = resp.session_id

    original_launch = MiniOrkAcpAgent._launch

    def captured_launch(self, run_id, kickoff_text):
        launched.append((run_id, kickoff_text))
        return {"ok": True, "run_id": run_id}

    MiniOrkAcpAgent._launch = captured_launch  # type: ignore[method-assign]
    try:
        turn_resp = asyncio.run(agent.prompt(sid, [_text_block("/run fix x")]))
    finally:
        MiniOrkAcpAgent._launch = original_launch  # type: ignore[method-assign]

    assert turn_resp.stop_reason == "end_turn"
    # The orchestrator was NOT invoked (slash command short-circuits).
    assert seen_turn["called"] is False
    # Direct mode launched one run with the slash payload.
    assert launched == [(launched[0][0], "fix x")]


def test_cancel_thread_session_cancels_orchestrator_task(tmp_path):
    """A ``cancel`` on a thread session cancels the in-flight orchestrator turn."""
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    started = asyncio.Event()
    finished = {"ok": False}

    def fake_turn(lane, prompt, cwd, home, resume, on_event):
        async def _run() -> Any:
            started.set()
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                finished["ok"] = True
                raise
            return _TurnResult()  # noqa: F821

        return _run()

    class _TurnResult:
        session_id = "sess-1"
        rc = 0
        text = ""
        cost_usd = 0.0
        error = ""

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        agent = MiniOrkAcpAgent(orchestrator_turn=fake_turn)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))
        sid = resp.session_id

    conn = _CapturingConn()
    agent.on_connect(conn)

    async def _drive():
        prompt_task = asyncio.create_task(agent.prompt(sid, [_text_block("hi")]))
        await started.wait()
        await agent.cancel(sid)
        return await prompt_task

    turn_resp = asyncio.run(_drive())
    assert turn_resp.stop_reason == "cancelled"
    assert finished["ok"] is True


def test_thread_session_keeps_run_session_path_unchanged(tmp_path):
    """A run-id session id (not a thread id) still goes through the launcher."""
    from unittest.mock import patch

    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        # Pre-populate a session that is NOT a thread session.
        agent = MiniOrkAcpAgent(
            launcher=lambda rid, _t: {"ok": True, "run_id": rid},
            reader=lambda _: {"status": "published", "events": [], "llm_calls": []},
        )
        # Manually mark a session as a run session (NOT in _thread_sessions).
        agent._sessions["run-1-abc"] = "/tmp/proj"
        agent._recipes["run-1-abc"] = "code-fix"
        # A prompt on this run id should still hit the run-session path.
        resp = asyncio.run(agent.prompt("run-1-abc", [_text_block("hi")]))
    assert resp.stop_reason == "end_turn"
    assert "run-1-abc" not in agent._thread_sessions


def test_model_picker_reads_the_session_homes_registry(tmp_path, monkeypatch):
    # No mock on ``orchestrator_lanes``: the picker must resolve the real
    # registry. The project's home shadows the engine's providers.yaml, so the
    # picker lists exactly the home's claude lanes (regression: the home was
    # passed as the ENGINE root, the lookup missed, and only "opus" showed).
    engine = tmp_path / "engine"
    (engine / "config").mkdir(parents=True)
    (engine / "config" / "providers.yaml").write_text("providers: {}\n")
    home = tmp_path / "proj" / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "providers.yaml").write_text(
        "providers:\n"
        "  opus: {kind: anthropic-native, family: anthropic}\n"
        "  sonnet: {kind: anthropic-native, family: anthropic}\n"
    )
    monkeypatch.setenv("MINI_ORK_ROOT", str(engine))
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    rows = MiniOrkAcpAgent()._list_orchestrator_lanes(home)
    assert [r.value for r in rows] == ["opus", "sonnet"]
    assert rows[1].name == "Sonnet (Claude subscription)"


# ── runs followed inside a thread (routing) ─────────────────────────────────


class _SidConn:
    """ACP connection that records which session each update was sent to."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update) -> None:
        self.sent.append((session_id, update))


class _Turn:
    def __init__(self, cost_usd: float = 0.0) -> None:
        self.session_id = "sess-1"
        self.rc = 0
        self.text = ""
        self.cost_usd = cost_usd
        self.error = ""


def _start_run_events(run_id: str, recipe: str = "docs") -> list[dict]:
    return [
        {"type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": f"t-{run_id}", "name": "mcp__mini-ork__start_run",
            "input": {"recipe": recipe}}]}},
        {"type": "user", "message": {"content": [{
            "type": "tool_result", "tool_use_id": f"t-{run_id}", "is_error": False,
            "content": [{"type": "text", "text": json.dumps({"run_id": run_id})}]}]}},
    ]


def _thread_agent(proj: Path, **kwargs) -> tuple[MiniOrkAcpAgent, str]:
    from unittest.mock import patch

    with patch("mini_ork.acp_orchestrator.config.orchestrator_lanes",
               return_value=[{"id": "opus", "name": "Opus"}]), \
         patch("mini_ork.web.recipes.list_recipes", return_value=["code-fix", "docs"]):
        agent = MiniOrkAcpAgent(poll_interval=0, **kwargs)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))
    return agent, resp.session_id


def test_child_run_streams_agent_output_into_the_thread(tmp_path):
    """A run the orchestrator starts shows its agents' live output, closes its
    marker when it ends, names itself in the terminal message, and the
    thread's cost is orchestrator + run."""
    proj = tmp_path / "proj"
    child = "run-child-live"
    live_path = proj / ".mini-ork" / "runs" / child / "agent-doc_editor.live.jsonl"
    live_path.parent.mkdir(parents=True)
    polls = {"n": 0}

    def reader(rid: str) -> dict:
        assert rid == child, "only the child run is ever read"
        polls["n"] += 1
        start = {"event_type": "node_start",
                 "payload_json": json.dumps({"node_id": "doc_editor", "node_type": "implementer"})}
        if polls["n"] == 1:
            return {"status": "executing", "events": [start], "llm_calls": []}
        if polls["n"] == 2:
            _write_live_lines(live_path, [json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "editing CHANGELOG"}]}})])
            return {"status": "executing", "events": [start], "llm_calls": []}
        end = {"event_type": "node_end", "payload_json": json.dumps({"node_id": "doc_editor"})}
        return {"status": "published", "events": [start, end],
                "llm_calls": [{"cost_usd": 0.25, "total_tokens": 900}]}

    def turn(lane, prompt, cwd, home, resume, on_event):
        async def _run():
            for ev in _start_run_events(child):
                await on_event(ev)
            return _Turn(cost_usd=0.5)
        return _run()

    agent, thread = _thread_agent(proj, orchestrator_turn=turn, reader=reader)
    conn = _SidConn()
    agent.on_connect(conn)

    async def scenario():
        await agent.prompt(thread, [_text_block("add a changelog line")])
        follower = agent._followers.get(child)
        if follower is not None:
            await follower

    asyncio.run(scenario())
    assert {sid for sid, _ in conn.sent} == {thread}, "every update must address the thread"
    updates = [u for _, u in conn.sent]
    ids = [u.tool_call_id for u in updates if isinstance(u, (ToolCallStart, ToolCallProgress))]
    assert f"{child}:doc_editor" in ids and "doc_editor" not in ids
    texts = [c.content.text for u in updates if isinstance(u, ToolCallProgress)
             for c in (u.content or [])]
    assert "editing CHANGELOG" in texts
    closes = [u for u in updates if isinstance(u, ToolCallProgress)
              and u.tool_call_id == f"{child}:parent"]
    assert [u.status for u in closes] == ["completed"]
    messages = [u.content.text for u in updates if isinstance(u, AgentMessageChunk)]
    assert f"mini-ork run {child} finished: published" in messages
    costs = [u.cost.amount for u in updates if isinstance(u, UsageUpdate)]
    assert costs[-1] == pytest.approx(0.75)


def test_direct_mode_run_streams_into_the_thread_not_a_run_session(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    launched: list[str] = []

    def launcher(rid: str, kickoff: str) -> dict:
        launched.append(rid)
        return {"ok": True}

    def reader(rid: str) -> dict:
        return {"status": "rolled_back", "events": [
            {"event_type": "node_start", "payload_json": json.dumps({"node_id": "n1"})},
            {"event_type": "node_end", "payload_json": json.dumps({"node_id": "n1"})}],
            "llm_calls": []}

    agent, thread = _thread_agent(proj, launcher=launcher, reader=reader)
    conn = _SidConn()
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt(thread, [_text_block("/run fix the typo")]))
    assert resp.stop_reason == "end_turn"
    (rid,) = launched
    assert {sid for sid, _ in conn.sent} == {thread}
    ids = [u.tool_call_id for _, u in conn.sent if isinstance(u, (ToolCallStart, ToolCallProgress))]
    assert ids[0] == f"{rid}:parent" and f"{rid}:n1" in ids
    close = [u for _, u in conn.sent if isinstance(u, ToolCallProgress)
             and u.tool_call_id == f"{rid}:parent"]
    assert [u.status for u in close] == ["failed"]  # rolled_back is not a success


def test_direct_mode_launch_failure_explains_and_keeps_the_thread(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    agent, thread = _thread_agent(
        proj, launcher=lambda rid, kickoff: {"ok": False, "error": "recipe not found"})
    conn = _SidConn()
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt(thread, [_text_block("/run x")]))
    assert resp.stop_reason == "end_turn"
    texts = [u.content.text for _, u in conn.sent if isinstance(u, AgentMessageChunk)]
    assert any("recipe not found" in t for t in texts)


def test_cancel_stops_only_this_threads_followers_and_not_the_next_turn(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    runs = iter(["run-a-1", "run-b-1", "run-a-2"])

    def turn(lane, prompt, cwd, home, resume, on_event):
        async def _run():
            for ev in _start_run_events(next(runs)):
                await on_event(ev)
            return _Turn()
        return _run()

    def reader(rid: str) -> dict:
        return {"status": "executing", "events": [], "llm_calls": []}

    agent, thread_a = _thread_agent(proj, orchestrator_turn=turn, reader=reader)
    from unittest.mock import patch
    with patch("mini_ork.acp_orchestrator.config.orchestrator_lanes",
               return_value=[{"id": "opus", "name": "Opus"}]), \
         patch("mini_ork.web.recipes.list_recipes", return_value=["docs"]):
        thread_b = asyncio.run(agent.new_session(cwd=str(proj))).session_id
    agent.on_connect(_SidConn())

    async def scenario():
        await agent.prompt(thread_a, [_text_block("start a")])
        await agent.prompt(thread_b, [_text_block("start b")])
        await agent.cancel(thread_a)
        await asyncio.sleep(0)
        assert "run-a-1" not in agent._followers
        assert not agent._followers["run-b-1"].done(), "thread B's run must keep streaming"
        # The next turn in the cancelled thread follows its new run again.
        await agent.prompt(thread_a, [_text_block("start another")])
        await asyncio.sleep(0)
        assert not agent._followers["run-a-2"].done()
        for task in list(agent._followers.values()):
            task.cancel()

    asyncio.run(scenario())


def test_cancel_during_direct_mode_stops_the_run(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    stopped: list[str] = []

    def reader(rid: str) -> dict:
        return {"status": "executing", "events": [], "llm_calls": []}

    agent, thread = _thread_agent(
        proj, launcher=lambda rid, kickoff: {"ok": True}, reader=reader,
        stopper=lambda rid: stopped.append(rid) or {"ok": True}, cancel_grace=0)
    agent.on_connect(_SidConn())

    async def scenario():
        turn = asyncio.create_task(agent.prompt(thread, [_text_block("/run slow job")]))
        for _ in range(5):
            await asyncio.sleep(0)
        await agent.cancel(thread)
        return await turn

    resp = asyncio.run(scenario())
    assert resp.stop_reason == "cancelled"
    assert len(stopped) == 1 and stopped[0].startswith("run-")


def test_thread_usage_is_sent_only_when_the_total_changes(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    polls = {"n": 0}

    def reader(rid: str) -> dict:
        polls["n"] += 1
        calls = [{"cost_usd": 0.1}] if polls["n"] < 4 else [{"cost_usd": 0.1}, {"cost_usd": 0.2}]
        return {"status": "published" if polls["n"] >= 5 else "executing",
                "events": [], "llm_calls": calls}

    agent, thread = _thread_agent(proj, launcher=lambda rid, kickoff: {"ok": True}, reader=reader)
    conn = _SidConn()
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/run x")]))
    costs = [u.cost.amount for _, u in conn.sent if isinstance(u, UsageUpdate)]
    assert costs == [pytest.approx(0.1), pytest.approx(0.3)]


# ── thread-session persistence (Z9c-2) ───────────────────────────────────


def _thread_store_path(proj: Path, thread_id: str) -> Path:
    return proj / ".mini-ork" / "acp-threads" / f"{thread_id}.jsonl"


def _read_thread_records(proj: Path, thread_id: str) -> list[dict]:
    """Load the persisted JSONL for ``thread_id`` as a list of records."""
    import json as _json
    path = _thread_store_path(proj, thread_id)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(_json.loads(line))
        except _json.JSONDecodeError:
            continue
    return out


def test_thread_prompt_records_meta_config_user_claude_session_costs(tmp_path):
    """One orchestrate turn with a start_run child writes the full record
    sequence: meta, config (new_session), user, update, claude_session, costs.

    The child run's internal updates are NOT recorded against the thread —
    the routed ``_emit`` remap lands on a non-thread session id and the
    outer guard in ``_record_thread_update`` filters them out.
    """

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()
    child = "run-child-rec"

    class _TurnResult:
        session_id = "claude-rec-1"
        rc = 0
        text = "answer"
        cost_usd = 0.1
        error = ""

    def turn(lane, prompt, cwd, home, resume, on_event):
        async def _run():
            for ev in _start_run_events(child):
                await on_event(ev)
            return _TurnResult()
        return _run()

    def reader(rid: str) -> dict:
        if rid == child:
            return {"status": "executing", "events": [], "llm_calls": []}
        return {"status": None, "events": [], "llm_calls": []}

    agent, thread = _thread_agent(proj, orchestrator_turn=turn, reader=reader)
    agent.on_connect(_SidConn())
    asyncio.run(agent.prompt(thread, [_text_block("kick off")]))
    recs = _read_thread_records(proj, thread)
    types = [r.get("type") for r in recs]
    # meta + config written by new_session; user + update + claude_session + costs by the prompt.
    assert types.count("meta") == 1
    assert types.count("config") == 1
    assert types.count("user") == 1
    assert types.count("update") >= 1
    assert types.count("claude_session") == 1
    assert types.count("costs") >= 1
    # The first prompt pushes a SessionInfoUpdate; it is NOT recorded (filter).
    update_kinds = [
        r["update"].get("sessionUpdate") for r in recs if r.get("type") == "update"
    ]
    assert "session_info_update" not in update_kinds
    # ToolCallStart(:parent) IS recorded (it is emitted directly to the thread).
    assert "tool_call" in update_kinds


def test_first_prompt_emits_session_info_update_once(tmp_path):
    """The first prompt of a thread pushes a SessionInfoUpdate; the second does not."""
    from acp.schema import SessionInfoUpdate

    proj = tmp_path / "proj"

    class _TurnResult:
        session_id = "sess-x"
        rc = 0
        text = ""
        cost_usd = 0.0
        error = ""

    def turn(lane, prompt, cwd, home, resume, on_event):
        async def _run():
            return _TurnResult()
        return _run()

    agent, thread = _thread_agent(proj, orchestrator_turn=turn)
    conn = _SidConn()
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("first prompt")]))
    asyncio.run(agent.prompt(thread, [_text_block("second prompt")]))
    updates = [
        u for _, u in conn.sent if isinstance(u, SessionInfoUpdate)
    ]
    assert len(updates) == 1
    assert updates[0].title.startswith("first prompt")
    # Loaded threads arrive with the flag already set, so a reload must
    # not re-emit the title update.
    sid = thread
    agent._first_prompt_sent.add(sid)  # simulate load
    conn.sent.clear()
    asyncio.run(agent.prompt(thread, [_text_block("third")]))
    assert not [u for _, u in conn.sent if isinstance(u, SessionInfoUpdate)]


def test_set_config_option_records_full_config_on_change(tmp_path):
    """Every accepted ``set_config_option`` writes one config record."""
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()
    agent, thread = _thread_agent(proj)

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=[{"id": "opus", "name": "Opus"}],
    ), patch(
        "mini_ork.web.recipes.list_recipes", return_value=["code-fix"]
    ):
        asyncio.run(agent.set_config_option("model", thread, "opus"))
        asyncio.run(agent.set_config_option("recipe", thread, "code-fix"))

    recs = _read_thread_records(proj, thread)
    configs = [r for r in recs if r.get("type") == "config"]
    # new_session writes 1; two set_config_option calls add 2 more.
    assert len(configs) == 3
    # The most recent reflects both changes.
    last = configs[-1]
    assert last["mode"] == "orchestrate"
    assert last["model"] == "opus"
    assert last["recipe"] == "code-fix"


def test_set_config_option_does_not_record_on_rejection(tmp_path):
    """An invalid value never persists AND never writes a config record."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()
    agent, thread = _thread_agent(proj)
    before = len(_read_thread_records(proj, thread))
    try:
        asyncio.run(agent.set_config_option("mode", thread, "hyperdrive"))
    except Exception:
        pass
    after = len(_read_thread_records(proj, thread))
    assert after == before


def test_claude_session_record_only_written_when_id_changes(tmp_path):
    """Two turns returning the SAME claude session id do NOT double-write."""

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()

    class _TurnResult:
        session_id = "sess-stable"
        rc = 0
        text = ""
        cost_usd = 0.0
        error = ""

    def turn(lane, prompt, cwd, home, resume, on_event):
        async def _run():
            return _TurnResult()
        return _run()

    agent, thread = _thread_agent(proj, orchestrator_turn=turn)
    agent.on_connect(_SidConn())
    asyncio.run(agent.prompt(thread, [_text_block("a")]))
    asyncio.run(agent.prompt(thread, [_text_block("b")]))
    recs = _read_thread_records(proj, thread)
    cs = [r for r in recs if r.get("type") == "claude_session"]
    assert len(cs) == 1
    assert cs[0]["id"] == "sess-stable"


def test_list_sessions_first_page_includes_threads_and_runs_sorted(tmp_path):
    """First page = threads + runs sorted by ``updated_at`` desc; second
    page = runs only (cursor stays bound to the run offset)."""

    proj = tmp_path / "proj"
    proj.mkdir()
    # Pre-populate the thread store with one thread so list_sessions sees it.
    from mini_ork.acp.threads import ThreadStore
    ThreadStore(proj / ".mini-ork").append(
        "orch-1700000000-list",
        {"type": "meta", "thread_id": "orch-1700000000-list", "cwd": str(proj)},
    )
    ThreadStore(proj / ".mini-ork").append(
        "orch-1700000000-list",
        {"type": "user", "text": "thread title"},
    )

    # Set up a run row by standing up a minimal state.db with a fake task_run.
    from mini_ork.stores import migrate as mig
    db_path = proj / ".mini-ork" / "state.db"
    mig.init_db(str(db_path))
    import sqlite3
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO task_runs(id, task_class, recipe, status, kickoff_path, "
        "cost_usd, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run-list-1",
            "code_fix",
            "code-fix",
            "published",
            str(proj / ".mini-ork" / "runs-inbox" / "run-list-1.md"),
            0.0,
            1_700_000_100,
            1_700_000_110,
        ),
    )
    con.commit()
    con.close()

    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.list_sessions(cwd=str(proj)))
    sids = [s.session_id for s in resp.sessions]
    field_kinds = [s.field_meta.get("kind") for s in resp.sessions]
    assert "orch-1700000000-list" in sids
    assert "run-list-1" in sids
    # First page has both kinds.
    assert "thread" in field_kinds
    # Newest first: thread has a fresh file mtime > the run's updated_at.
    assert sids.index("orch-1700000000-list") < sids.index("run-list-1")
    # Second page (cursor) is runs-only — threads are NOT prepended again.
    resp2 = asyncio.run(agent.list_sessions(cwd=str(proj), cursor="50"))
    sids2 = [s.session_id for s in resp2.sessions]
    assert "orch-1700000000-list" not in sids2


def test_load_session_replays_and_resumes_with_stored_id(tmp_path):
    """A NEW agent instance can ``load_session`` the persisted thread:
    replays user + message + tool_call + run marker + child lifecycle,
    returns config options with the stored model, and the next prompt
    passes the persisted claude session id as ``resume``."""
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()
    child = "run-child-load"

    class _TurnResult:
        def __init__(self, session_id: str = "sess-load-1") -> None:
            self.session_id = session_id
            self.rc = 0
            self.text = ""
            self.cost_usd = 0.5
            self.error = ""

    seen: list[Any] = []

    def turn_first(lane, prompt, cwd, home, resume, on_event):
        async def _run():
            for ev in _start_run_events(child):
                await on_event(ev)
            return _TurnResult()
        return _run()

    def reader(rid: str) -> dict:
        if rid == child:
            return {"status": "published", "events": [], "llm_calls": []}
        return {"status": None, "events": [], "llm_calls": []}

    with patch("mini_ork.acp_orchestrator.config.orchestrator_lanes",
               return_value=[{"id": "opus", "name": "Opus"}]), \
         patch("mini_ork.web.recipes.list_recipes", return_value=["code-fix"]):
        # First agent: persist a turn.
        agent1, thread = _thread_agent(proj, orchestrator_turn=turn_first, reader=reader)
        agent1.on_connect(_SidConn())
        asyncio.run(agent1.prompt(thread, [_text_block("hello")]))

        # Force a terminal child snapshot so the replay can finish cleanly.
        def reader2(rid: str) -> dict:
            if rid == child:
                return {"status": "published", "events": [], "llm_calls": []}
            return {"status": None, "events": [], "llm_calls": []}

        # Second agent: same home, fresh state. ``load_session`` replays.
        agent2 = MiniOrkAcpAgent()
        agent2.on_connect(_SidConn())
        resp = asyncio.run(agent2.load_session(cwd=str(proj), session_id=thread))
        assert resp.config_options is not None
        model_opt = next(o for o in resp.config_options if o.id == "model")
        assert model_opt.current_value == "opus"
    # Re-run the load with a capturing conn to verify the replayed sequence.
    with patch("mini_ork.acp_orchestrator.config.orchestrator_lanes",
               return_value=[{"id": "opus", "name": "Opus"}]), \
         patch("mini_ork.web.recipes.list_recipes", return_value=["code-fix"]):
        agent3 = MiniOrkAcpAgent()
        conn3 = _SidConn()
        agent3.on_connect(conn3)
        asyncio.run(agent3.load_session(cwd=str(proj), session_id=thread))
        sent_sids = {sid for sid, _ in conn3.sent}
        assert sent_sids == {thread}
        types = [
            type(u).__name__ for _, u in conn3.sent
        ]
        # user → message → tool_call(marker) → child lifecycle → usage
        assert "UserMessageChunk" in types
        assert "ToolCallStart" in types
        assert "UsageUpdate" in types
        # No SessionInfoUpdate (loaded thread marks first prompt).
        assert "SessionInfoUpdate" not in types

        # The next prompt passes the stored claude session id as ``resume``.
        def turn_after(lane, prompt, cwd, home, resume, on_event):
            async def _run():
                seen.append(resume)
                # Return a new id so the prompt round-trips; the agent
                # should record a fresh claude_session because the id differs.
                return _TurnResult(session_id="sess-load-2")
            return _run()

        with patch("mini_ork.acp_orchestrator.config.orchestrator_lanes",
                   return_value=[{"id": "opus", "name": "Opus"}]), \
             patch("mini_ork.web.recipes.list_recipes", return_value=["code-fix"]):
            agent3._orchestrator_turn = turn_after
            asyncio.run(agent3.prompt(thread, [_text_block("again")]))
        assert seen[-1] == "sess-load-1"


def test_load_session_starts_follower_for_running_child(tmp_path):
    """Loading a thread whose child run is still running spawns a follower.

    The follower registration is observable: ``_routes[child]`` is set
    (the replay walk registers the route for ``<run_id>:parent`` updates)
    AND a follower task is created when the child snapshot is non-terminal.
    The task itself polls until the run ends or the test fixture cancels it.
    """
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()
    child = "run-still-going"

    class _TurnResult:
        session_id = "sess-running"
        rc = 0
        text = ""
        cost_usd = 0.0
        error = ""

    def turn(lane, prompt, cwd, home, resume, on_event):
        async def _run():
            for ev in _start_run_events(child):
                await on_event(ev)
            return _TurnResult()
        return _run()

    def reader(rid: str) -> dict:
        if rid == child:
            return {"status": "executing", "events": [], "llm_calls": []}
        return {"status": None, "events": [], "llm_calls": []}

    with patch("mini_ork.acp_orchestrator.config.orchestrator_lanes",
               return_value=[{"id": "opus", "name": "Opus"}]), \
         patch("mini_ork.web.recipes.list_recipes", return_value=["code-fix"]):
        agent, thread = _thread_agent(proj, orchestrator_turn=turn, reader=reader)
        agent.on_connect(_SidConn())
        asyncio.run(agent.prompt(thread, [_text_block("go")]))

        # Now load in a fresh agent — child is still executing.
        agent2 = MiniOrkAcpAgent()
        agent2.on_connect(_SidConn())

        # The follower is observable WHILE the load coroutine runs (the
        # task is created in load_session and polled by the event loop
        # alongside the load). Reach for ``_routes`` (which is set before
        # the task starts) AND observe the running tasks count from
        # inside the same ``asyncio.run`` boundary.
        observed = {"routes": {}, "followers": {}}

        async def _load_and_observe():
            await agent2.load_session(cwd=str(proj), session_id=thread)
            observed["routes"] = dict(agent2._routes)
            observed["followers"] = {
                rid: task for rid, task in agent2._followers.items()
                if not task.done()
            }

        asyncio.run(_load_and_observe())
        assert child in observed["routes"]
        assert observed["routes"][child][0] == thread
        assert child in observed["followers"]
        # Cleanup: cancel so the test exits cleanly.
        for task in list(observed["followers"].values()):
            task.cancel()


def test_load_session_unknown_orch_id_is_invalid_params(tmp_path):
    """An unknown ``orch-…`` id raises ``invalid_params`` (code -32602)."""
    agent = MiniOrkAcpAgent()
    try:
        asyncio.run(agent.load_session(cwd="/tmp", session_id="orch-1700000000-nope"))
    except Exception as exc:
        assert getattr(exc, "code", None) == -32602
        assert "unknown thread" in str(getattr(exc, "data", None) or "")
    else:
        raise AssertionError("expected invalid_params")


def test_load_session_run_session_still_works(tmp_path):
    """Run-session load path is unchanged: a non-orch session id hits the
    existing ``history.read_snapshot`` path and raises if the run is missing."""

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()
    agent = MiniOrkAcpAgent()
    # A run id with no task_runs row → invalid_params (existing behaviour).
    try:
        asyncio.run(agent.load_session(cwd=str(proj), session_id="run-nope-1"))
    except Exception as exc:
        assert getattr(exc, "code", None) == -32602
    else:
        raise AssertionError("expected invalid_params for missing run")


# ── thread persistence: review regressions ──────────────────────────────────


def _child_world(proj: Path, child: str, *, final_status: str = "published"):
    """A turn that starts ``child`` and a reader that runs it to ``final_status``
    with one node and one live line."""
    live = proj / ".mini-ork" / "runs" / child / "agent-n1.live.jsonl"
    live.parent.mkdir(parents=True, exist_ok=True)
    _write_live_lines(live, [json.dumps({
        "type": "assistant", "message": {"content": [{"type": "text", "text": "child says hi"}]}})])
    start = {"event_type": "node_start", "payload_json": json.dumps({"node_id": "n1", "node_type": "implementer"})}
    end = {"event_type": "node_end", "payload_json": json.dumps({"node_id": "n1"})}

    def reader(rid: str) -> dict:
        assert rid == child
        if final_status in TERMINAL:
            return {"status": final_status, "events": [start, end], "llm_calls": [{"cost_usd": 0.25}]}
        return {"status": final_status, "events": [start], "llm_calls": [{"cost_usd": 0.25}]}

    def turn(lane, prompt, cwd, home, resume, on_event):
        async def _run():
            for ev in _start_run_events(child):
                await on_event(ev)
            return _Turn(cost_usd=0.5)
        return _run()

    return reader, turn


TERMINAL = {"published", "rolled_back", "failed"}


def _recorded(proj: Path, thread: str) -> list[dict]:
    from mini_ork.acp.threads import ThreadStore
    return ThreadStore(proj / ".mini-ork").read(thread)


def test_child_run_internals_are_not_recorded_in_the_thread(tmp_path):
    proj = tmp_path / "proj"
    child = "run-child-rec"
    reader, turn = _child_world(proj, child)
    agent, thread = _thread_agent(proj, orchestrator_turn=turn, reader=reader)
    agent.on_connect(_SidConn())

    async def scenario():
        await agent.prompt(thread, [_text_block("go")])
        if agent._followers.get(child):
            await agent._followers[child]

    asyncio.run(scenario())
    ids = [str(r["update"].get("toolCallId", "")) for r in _recorded(proj, thread) if r["type"] == "update"]
    assert f"{child}:parent" in ids
    assert not [i for i in ids if i.startswith(f"{child}:") and i != f"{child}:parent"], ids
    texts = json.dumps(_recorded(proj, thread))
    assert "child says hi" not in texts


def test_reopened_thread_replays_marker_then_child_history_once(tmp_path):
    """Same process re-open (Zed does it): child history replays after its
    marker, live output once, and the recorded costs are refreshed, not stale."""
    proj = tmp_path / "proj"
    child = "run-child-reopen"
    reader, turn = _child_world(proj, child)
    agent, thread = _thread_agent(proj, orchestrator_turn=turn, reader=reader)
    agent.on_connect(_SidConn())

    async def live_then_reopen():
        await agent.prompt(thread, [_text_block("go")])
        if agent._followers.get(child):
            await agent._followers[child]
        conn = _SidConn()
        agent.on_connect(conn)
        await agent.load_session(cwd=str(proj), session_id=thread)
        return conn

    conn = asyncio.run(live_then_reopen())
    updates = [u for _, u in conn.sent]
    ids = [getattr(u, "tool_call_id", None) for u in updates]
    assert ids.index(f"{child}:parent") < ids.index(f"{child}:n1")
    live = [c.content.text for u in updates if isinstance(u, ToolCallProgress)
            for c in (u.content or []) if getattr(c.content, "text", None) == "child says hi"]
    assert live == ["child says hi"]
    costs = [u.cost.amount for u in updates if isinstance(u, UsageUpdate)]
    assert costs[-1] == pytest.approx(0.75)


def test_loading_a_thread_with_a_running_child_streams_its_backlog_once(tmp_path):
    proj = tmp_path / "proj"
    child = "run-child-running"
    reader, turn = _child_world(proj, child, final_status="executing")
    agent, thread = _thread_agent(proj, orchestrator_turn=turn, reader=reader)
    agent.on_connect(_SidConn())

    async def first_life():
        await agent.prompt(thread, [_text_block("go")])
        for task in list(agent._followers.values()):
            task.cancel()

    asyncio.run(first_life())
    fresh = MiniOrkAcpAgent(reader=reader, poll_interval=0)
    conn = _SidConn()
    fresh.on_connect(conn)

    async def reopen():
        await fresh.load_session(cwd=str(proj), session_id=thread)
        for _ in range(5):
            await asyncio.sleep(0)
        for task in list(fresh._followers.values()):
            task.cancel()

    asyncio.run(reopen())
    live = [u for _, u in conn.sent if isinstance(u, ToolCallProgress)
            and any(getattr(c.content, "text", None) == "child says hi" for c in (u.content or []))]
    assert len(live) == 1


def test_loading_an_unknown_thread_leaves_no_phantom_thread(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".mini-ork").mkdir(parents=True)
    agent = MiniOrkAcpAgent()
    with pytest.raises(Exception):
        asyncio.run(agent.load_session(cwd=str(proj), session_id="orch-1-nothere"))
    assert "orch-1-nothere" not in agent._thread_sessions
    assert "orch-1-nothere" not in agent._sessions


def test_fresh_process_load_puts_each_run_after_its_marker(tmp_path):
    proj = tmp_path / "proj"
    child = "run-child-order"
    reader, turn = _child_world(proj, child)
    agent, thread = _thread_agent(proj, orchestrator_turn=turn, reader=reader)
    agent.on_connect(_SidConn())

    async def first_life():
        await agent.prompt(thread, [_text_block("go")])
        if agent._followers.get(child):
            await agent._followers[child]

    asyncio.run(first_life())
    fresh = MiniOrkAcpAgent(reader=reader, poll_interval=0)
    conn = _SidConn()
    fresh.on_connect(conn)
    resp = asyncio.run(fresh.load_session(cwd=str(proj), session_id=thread))
    ids = [getattr(u, "tool_call_id", None) for _, u in conn.sent]
    assert ids.index(f"{child}:parent") < ids.index(f"{child}:n1")
    assert {sid for sid, _ in conn.sent} == {thread}
    assert [o.id for o in resp.config_options] == ["mode", "model", "recipe"]


# ── Z4 implementer diff surface ──────────────────────────────────────────────


def _seed_diff_artifacts(
    home: Path, run_id: str, worktree: Path, file_rel: str = "tracked.txt"
) -> None:
    """Write the artifacts ``diffs.run_diffs`` reads.

    ``worktree`` is a tmp dir the test controls; ``file_rel`` is the
    worktree-relative path the summary points at. The fixture is omitted —
    the diff hook reaches ``git show`` (which fails) and falls through to
    ``new_text`` from disk only.
    """
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # Synthetic tracked file in the worktree: new_text comes from disk.
    (worktree / file_rel).parent.mkdir(parents=True, exist_ok=True)
    (worktree / file_rel).write_text("after\n", encoding="utf-8")
    payload = {
        "status": "implemented",
        "worktree_path": str(worktree),
        "files_changed": [str(worktree / file_rel)],
        "implementation_log": "",
    }
    (run_dir / "implementer-summary.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    (run_dir / "pre-implementer-ref").write_text("\n", encoding="utf-8")


def test_implementer_node_end_emits_diff_update_with_expected_paths(
    tmp_path: Path,
) -> None:
    proj, home = _migrate_home(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_id = "run-z4-1"
    _seed_diff_artifacts(home, run_id, worktree)
    events = [
        {
            "event_type": "node_end",
            "payload_json": json.dumps(
                {"node_id": "implementer", "node_type": "implementer"}
            ),
        }
    ]

    def fake_reader(_rid: str) -> dict:
        return {"status": "published", "events": events, "llm_calls": []}

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent(home=home, reader=fake_reader, poll_interval=0)
    agent.on_connect(conn)
    asyncio.run(agent._project_snapshot(run_id, fake_reader(run_id)))

    diff_updates = [
        u for u in captured
        if isinstance(u, ToolCallProgress)
        and any(isinstance(c, FileEditToolCallContent) for c in (u.content or []))
    ]
    assert len(diff_updates) == 1
    assert diff_updates[0].tool_call_id == "implementer"
    files = [c for c in diff_updates[0].content if isinstance(c, FileEditToolCallContent)]
    assert len(files) == 1
    assert files[0].path == str(worktree / "tracked.txt")
    assert files[0].new_text == "after\n"
    assert files[0].old_text is None  # no baseline → treated as new


def test_second_poll_does_not_re_emit_diff(tmp_path: Path) -> None:
    proj, home = _migrate_home(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_id = "run-z4-2"
    _seed_diff_artifacts(home, run_id, worktree)
    events = [
        {
            "event_type": "node_end",
            "payload_json": json.dumps(
                {"node_id": "implementer", "node_type": "implementer"}
            ),
        }
    ]

    def fake_reader(_rid: str) -> dict:
        return {"status": "published", "events": events, "llm_calls": []}

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent(home=home, reader=fake_reader, poll_interval=0)
    agent.on_connect(conn)
    snapshot = fake_reader(run_id)
    asyncio.run(agent._project_snapshot(run_id, snapshot))
    asyncio.run(agent._project_snapshot(run_id, snapshot))

    diff_updates = [
        u for u in captured
        if isinstance(u, ToolCallProgress)
        and any(isinstance(c, FileEditToolCallContent) for c in (u.content or []))
    ]
    assert len(diff_updates) == 1


def test_child_run_diff_in_thread_lands_under_prefix(
    tmp_path: Path,
) -> None:
    """A child run followed inside a thread session gets the diff update
    under ``"<run_id>:<node>"`` — the same prefix the rest of the routed
    updates carry (matches the kickoff §"Tests" bullet 2)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    home = proj / ".mini-ork"
    home.mkdir()
    rc, out, err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    worktree = tmp_path / "wt"
    worktree.mkdir()

    launched: list[str] = []

    def fake_launcher(rid: str, _t: str) -> dict:
        # Seed the run_dir under whatever id the thread minted so the
        # implementer ``node_end`` projection can find the artifacts.
        launched.append(rid)
        _seed_diff_artifacts(home, rid, worktree)
        return {"ok": True, "run_id": rid}

    states = iter(["executing", "published"])
    events = [
        {
            "event_type": "node_end",
            "payload_json": json.dumps(
                {"node_id": "implementer", "node_type": "implementer"}
            ),
        }
    ]

    def fake_reader(rid: str) -> dict:
        if rid in launched:
            return {"status": next(states, "published"), "events": events, "llm_calls": []}
        return {"status": "published", "events": [], "llm_calls": []}

    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]
    from unittest.mock import patch
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.web.recipes.list_recipes",
        return_value=fake_recipes,
    ):
        agent = MiniOrkAcpAgent(
            home=home,
            launcher=fake_launcher,
            reader=fake_reader,
            poll_interval=0,
            default_mode="direct",
            default_recipe="code-fix",
        )
        new = asyncio.run(agent.new_session(cwd=str(proj)))
        thread = new.session_id
        conn = _SidConn()
        agent.on_connect(conn)

        async def run_then_follow() -> None:
            await agent.prompt(thread, [_text_block("/run go")])
            if agent._followers.get(launched[0]) if launched else False:
                await agent._followers[launched[0]]

        asyncio.run(run_then_follow())

    (child,) = launched
    diff_updates = [
        (sid, u) for sid, u in conn.sent
        if isinstance(u, ToolCallProgress)
        and any(isinstance(c, FileEditToolCallContent) for c in (u.content or []))
    ]
    assert diff_updates, "expected one diff update on the thread session"
    sid, u = diff_updates[0]
    assert sid == thread
    assert u.tool_call_id == f"{child}:implementer"


def test_load_of_published_run_replays_cache_without_may_have_changed_note(
    tmp_path: Path,
) -> None:
    proj, home = _migrate_home(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_id = "run-z4-pub"
    _seed_diff_artifacts(home, run_id, worktree)
    run_dir = home / "runs" / run_id
    # Pre-seed the cache so the load replays it (no "may have changed" note).
    (run_dir / "acp-diffs.json").write_text(
        json.dumps(
            [
                {
                    "path": str(worktree / "tracked.txt"),
                    "old_text": "before\n",
                    "new_text": "after\n",
                }
            ]
        ),
        encoding="utf-8",
    )
    _seed_run(home, run_id, status="published", kickoff_text="# Cached\n")

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent()
    agent.on_connect(conn)
    asyncio.run(agent.load_session(cwd=str(proj), session_id=run_id))

    notes = [
        u.content.text for u in captured
        if isinstance(u, AgentMessageChunk)
        and "may have changed" in u.content.text
    ]
    assert notes == []
    diffs = [
        u for u in captured
        if isinstance(u, ToolCallProgress)
        and any(isinstance(c, FileEditToolCallContent) for c in (u.content or []))
    ]
    assert diffs and diffs[0].tool_call_id == "implementer"


def test_load_without_cache_prefixes_may_have_changed_note(tmp_path: Path) -> None:
    proj, home = _migrate_home(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_id = "run-z4-fresh"
    _seed_diff_artifacts(home, run_id, worktree)
    _seed_run(home, run_id, status="published", kickoff_text="# Fresh\n")

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent()
    agent.on_connect(conn)
    asyncio.run(agent.load_session(cwd=str(proj), session_id=run_id))

    note_idx = next(
        i for i, u in enumerate(captured)
        if isinstance(u, AgentMessageChunk) and "may have changed" in u.content.text
    )
    diffs = [
        u for u in captured
        if isinstance(u, ToolCallProgress)
        and any(isinstance(c, FileEditToolCallContent) for c in (u.content or []))
    ]
    assert diffs
    diff_idx = captured.index(diffs[0])
    assert note_idx < diff_idx


def test_load_of_rolled_back_run_emits_message_and_no_diffs(tmp_path: Path) -> None:
    proj, home = _migrate_home(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_id = "run-z4-rb"
    _seed_diff_artifacts(home, run_id, worktree)
    _seed_run(home, run_id, status="rolled_back", kickoff_text="# RB\n")

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent()
    agent.on_connect(conn)
    asyncio.run(agent.load_session(cwd=str(proj), session_id=run_id))

    messages = [
        u.content.text for u in captured if isinstance(u, AgentMessageChunk)
    ]
    assert any("rolled back" in m for m in messages)
    diffs = [
        u for u in captured
        if isinstance(u, ToolCallProgress)
        and any(isinstance(c, FileEditToolCallContent) for c in (u.content or []))
    ]
    assert diffs == []


def _seed_impl_lifecycle(home: Path, run_id: str, node_id: str = "doc_editor") -> None:
    con = sqlite3.connect(str(home / "state.db"))
    try:
        for i, kind in enumerate(("node_start", "node_end")):
            con.execute(
                "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"{run_id}-{node_id}-{kind}", run_id, kind,
                 json.dumps({"node_id": node_id, "node_type": "implementer"}), 1500 + i),
            )
        con.commit()
    finally:
        con.close()


def test_load_shows_the_recorded_diff_not_todays_files_and_keeps_the_cache(tmp_path: Path) -> None:
    """Opening an old run replays what the run changed. The file has changed
    since (it now reads "after\\n"); the cache from the run must win, stay
    byte-identical, and no "may have changed" note is shown."""
    proj, home = _migrate_home(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_id = "run-z4-recorded"
    _seed_diff_artifacts(home, run_id, worktree)
    cache = home / "runs" / run_id / "acp-diffs.json"
    recorded = json.dumps([{"path": str(worktree / "tracked.txt"),
                            "old_text": "v1\n", "new_text": "v2 by the run\n"}])
    cache.write_text(recorded, encoding="utf-8")
    _seed_run(home, run_id, status="published", kickoff_text="# Recorded\n")
    _seed_impl_lifecycle(home, run_id)

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent()
    agent.on_connect(conn)
    asyncio.run(agent.load_session(cwd=str(proj), session_id=run_id))

    diffs = [c for u in captured if isinstance(u, ToolCallProgress)
             for c in (u.content or []) if isinstance(c, FileEditToolCallContent)]
    assert [(d.old_text, d.new_text) for d in diffs] == [("v1\n", "v2 by the run\n")]
    assert cache.read_text(encoding="utf-8") == recorded
    assert not [u for u in captured if isinstance(u, AgentMessageChunk)
                and "may have changed" in u.content.text]


def test_load_without_cache_does_not_create_one(tmp_path: Path) -> None:
    proj, home = _migrate_home(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_id = "run-z4-nocache"
    _seed_diff_artifacts(home, run_id, worktree)
    _seed_run(home, run_id, status="published", kickoff_text="# No cache\n")
    _seed_impl_lifecycle(home, run_id)
    agent = MiniOrkAcpAgent()
    agent.on_connect(_capturing_conn()[1])
    asyncio.run(agent.load_session(cwd=str(proj), session_id=run_id))
    assert not (home / "runs" / run_id / "acp-diffs.json").exists()
