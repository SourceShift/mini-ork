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
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)

from mini_ork.acp.agent import MiniOrkAcpAgent, mint_run_id  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.web.control import _is_safe_token  # noqa: E402


def _text_block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


def _terminal_reader(_run_id: str) -> dict:
    return {"status": "published", "events": [], "llm_calls": []}


# ── contract assertion 1 ────────────────────────────────────────────────────


def test_new_session_mints_safe_run_id_and_binds_meta_and_cwd():
    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.new_session(cwd="/tmp/proj"))
    sid = resp.session_id
    assert sid.startswith("run-")
    assert _is_safe_token(sid)
    # The _meta.run_id override records the run id the session is bound to.
    assert resp.field_meta == {"run_id": sid}
    # session -> cwd binding.
    assert agent._sessions[sid] == "/tmp/proj"


def test_new_session_honours_client_meta_run_id_override():
    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.new_session(cwd="/tmp/proj", run_id="run-client-abc123"))
    assert resp.session_id == "run-client-abc123"
    assert _is_safe_token(resp.session_id)
    assert resp.field_meta == {"run_id": "run-client-abc123"}
    assert agent._sessions["run-client-abc123"] == "/tmp/proj"


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
