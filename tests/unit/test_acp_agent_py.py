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
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from acp.schema import (  # noqa: E402
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)

from mini_ork.acp.agent import MiniOrkAcpAgent, mint_run_id  # noqa: E402
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
