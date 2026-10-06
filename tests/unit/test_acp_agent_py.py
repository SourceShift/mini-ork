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
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from acp.schema import (  # noqa: E402
    AgentMessageChunk,
    AgentPlanUpdate,
    ContentToolCallContent,
    FileEditToolCallContent,
    ResourceContentBlock,
    SessionInfoUpdate,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    UserMessageChunk,
)
from acp import RequestError  # noqa: E402

from mini_ork.acp.agent import MiniOrkAcpAgent, mint_run_id  # noqa: E402
from mini_ork.recipes_catalog import RecipeInfo  # noqa: E402


def _recipe(name: str) -> RecipeInfo:
    """Build a minimal engine ``RecipeInfo`` for picker mocks.

    The picker / ``_list_recipes`` only reads ``.id``; the other fields
    are populated with safe defaults so a test that ignores them still
    gets a valid frozen dataclass instance.
    """
    return RecipeInfo(
        id=name,
        source="engine",
        path=Path("/_recipe_catalog_test"),
        description="",
        task_class="",
        node_count=0,
        shadows_engine=False,
    )


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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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
    # The picker list carries four Zed-rendered options in the mandated order.
    # (Z4 added workspace; the option list ends with it.)
    assert resp.config_options is not None
    assert [opt.id for opt in resp.config_options] == ["mode", "model", "recipe", "workspace"]
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


def test_initialize_stores_capabilities_and_logs_one_stderr_line(
    monkeypatch, tmp_path, capsys
):
    """S0 (zed-integration): ``initialize`` keeps the client capabilities
    object so ``client_supports`` can answer later, and writes ONE
    stderr line summarising them. The exact line format is the kickoff
    contract — a future reader relies on it for diagnostics.
    """

    from acp.schema import (
        ClientCapabilities,
        ElicitationCapabilities,
        ElicitationFormCapabilities,
        FileSystemCapabilities,
    )

    caps = ClientCapabilities(
        fs=FileSystemCapabilities(read_text_file=True, write_text_file=False),
        terminal=True,
        elicitation=ElicitationCapabilities(
            form=ElicitationFormCapabilities(), url=None
        ),
    )
    # Point the agent at an empty engine root so a stray recipe on the
    # test machine never lands in the stderr line.
    engine = tmp_path / "engine"
    engine.mkdir()
    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: engine)

    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.initialize(protocol_version=1, client_capabilities=caps))
    assert resp.protocol_version == 1
    # The capabilities object is stored verbatim on the agent.
    assert agent._client_capabilities is caps
    # ``client_supports`` reads it back.
    assert agent.client_supports("fs.read") is True
    assert agent.client_supports("fs.write") is False
    assert agent.client_supports("terminal") is True
    assert agent.client_supports("elicitation.form") is True
    assert agent.client_supports("unknown.feature") is False
    # ONE stderr line, exact format from the kickoff.
    captured = capsys.readouterr()
    out = captured.err.splitlines()
    summary_lines = [ln for ln in out if ln.startswith("[mini-ork acp] client capabilities:")]
    assert len(summary_lines) == 1, f"expected exactly one summary line, got: {out}"
    line = summary_lines[0]
    assert line == (
        "[mini-ork acp] client capabilities: "
        "fs.read=True fs.write=False terminal=True elicitation=form"
    )


def test_initialize_with_no_capabilities_yields_all_false():
    """When the client does not send ``client_capabilities`` (or sends
    ``None``), every feature is False and nothing raises."""
    agent = MiniOrkAcpAgent()
    asyncio.run(agent.initialize(protocol_version=1))
    assert agent._client_capabilities is None
    for feature in ("fs.read", "fs.write", "terminal", "elicitation.form", "nope"):
        assert agent.client_supports(feature) is False


def test_recipe_picker_includes_project_recipe(monkeypatch, tmp_path):
    """A thread session whose project has ``.mini-ork/recipes/my-audit/``
    lists ``my-audit`` in the recipe picker. The engine root is patched
    to an empty dir so the test never depends on the real engine
    checkout.
    """

    # Project home = ``<tmp>/proj/.mini-ork/recipes/my-audit/...``.
    proj = tmp_path / "proj"
    proj.mkdir()
    proj_home = proj / ".mini-ork"
    proj_recipes = proj_home / "recipes" / "my-audit"
    proj_recipes.mkdir(parents=True)
    (proj_recipes / "task_class.yaml").write_text(
        "name: my_audit\ndescription: My audit recipe\n",
        encoding="utf-8",
    )
    (proj_recipes / "workflow.yaml").write_text(
        "name: my-audit\nnodes:\n  - id: a\n    type: verifier\n",
        encoding="utf-8",
    )
    # Engine root is empty so only the project recipe surfaces.
    engine = tmp_path / "engine"
    engine.mkdir()
    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: engine)
    # Orchestrator lane list — keep the lane picker functional.
    monkeypatch.setattr(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        lambda: [{"id": "opus", "name": "Opus"}],
    )

    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.new_session(cwd=str(proj)))
    assert resp.config_options is not None
    recipe_opt = next(o for o in resp.config_options if o.id == "recipe")
    assert recipe_opt is not None
    recipe_ids = {o.value for o in recipe_opt.options}
    assert "my-audit" in recipe_ids
    # And the description for the project entry follows the kickoff rule.
    proj_entry = next(o for o in recipe_opt.options if o.value == "my-audit")
    assert proj_entry.description == "project recipe"


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
    # Zed S1: published run rows carry the done-mark prefix.
    assert info.title == "✓ A past run"
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe("code-fix"), _recipe("framework-edit")],
    ):
        resp = asyncio.run(agent.set_config_option("model", sid, "sonnet"))
    assert resp.config_options is not None
    assert [opt.id for opt in resp.config_options] == ["mode", "model", "recipe", "workspace"]
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe("code-fix")],
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
    ):
        agent = MiniOrkAcpAgent(orchestrator_turn=fake_turn)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))

    # Switch the model via set_config_option and observe the seam picks it up.
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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
         patch("mini_ork.recipes_catalog.list_recipes", return_value=[_recipe("code-fix"), _recipe("docs")]):
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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
         patch("mini_ork.recipes_catalog.list_recipes", return_value=[_recipe("code-fix"), _recipe("docs")]):
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
         patch("mini_ork.recipes_catalog.list_recipes", return_value=[_recipe("docs")]):
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
        "mini_ork.recipes_catalog.list_recipes", return_value=[_recipe("code-fix")]
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
         patch("mini_ork.recipes_catalog.list_recipes", return_value=[_recipe("code-fix")]):
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
         patch("mini_ork.recipes_catalog.list_recipes", return_value=[_recipe("code-fix")]):
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
        # Zed S1: a load replays the task-state title as a single
        # ``SessionInfoUpdate`` (the thread's latest persisted title).
        # The first-prompt title push is suppressed by
        # ``_first_prompt_sent``; the task-state path emits exactly
        # once per load (the title records already carry every prior
        # state, so the projection dedups further cycles).
        siu = [u for _, u in conn3.sent if isinstance(u, SessionInfoUpdate)]
        assert len(siu) == 1

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
             patch("mini_ork.recipes_catalog.list_recipes", return_value=[_recipe("code-fix")]):
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
         patch("mini_ork.recipes_catalog.list_recipes", return_value=[_recipe("code-fix")]):
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
    assert [o.id for o in resp.config_options] == ["mode", "model", "recipe", "workspace"]


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
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
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


# ── Z5: announcement + slash-command routing ────────────────────────────────


from acp.schema import AvailableCommandsUpdate  # noqa: E402


def _commands_updates(captured: list) -> list:
    return [u for u in captured if isinstance(u, AvailableCommandsUpdate)]


def test_z5_new_session_run_emits_commands_update(tmp_path: Path) -> None:
    """``session/new`` for a run session pushes ``available_commands_update``."""
    proj, _home = _migrate_home(tmp_path)
    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent()
    agent.on_connect(conn)
    asyncio.run(
        agent.new_session(cwd=str(proj), run_id="run-z5-001", recipe="code-fix")
    )
    updates = _commands_updates(captured)
    assert updates, f"expected an AvailableCommandsUpdate, got {captured!r}"
    announced = {c.name for c in updates[0].available_commands}
    assert {"help", "status", "runs", "stop", "kill", "serve"} <= announced


def test_z5_new_session_thread_emits_commands_update(tmp_path: Path) -> None:
    """``session/new`` for a thread session also pushes ``available_commands_update``."""
    proj, _home = _migrate_home(tmp_path)
    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent()
    agent.on_connect(conn)
    resp = asyncio.run(agent.new_session(cwd=str(proj)))
    assert resp.session_id.startswith("orch-")
    assert _commands_updates(captured), (
        f"expected an AvailableCommandsUpdate for a thread session, got {captured!r}"
    )


def test_z5_load_session_run_emits_commands_update(tmp_path: Path) -> None:
    """``session/load`` re-announces the command list (run session)."""
    proj, home = _migrate_home(tmp_path)
    run_id = "run-z5-load-001"
    _seed_run(home, run_id, status="published", kickoff_text="# Z5 load\n")
    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent()
    agent.on_connect(conn)
    asyncio.run(agent.load_session(cwd=str(proj), session_id=run_id))
    assert _commands_updates(captured), "load_session did not emit commands"


def test_z5_prompt_slash_status_run_returns_end_turn(tmp_path: Path) -> None:
    """A leading ``/status`` in a run session returns ``end_turn`` and never launches."""
    proj, home = _migrate_home(tmp_path)
    run_id = "run-z5-status-001"
    _seed_run(home, run_id, status="published", kickoff_text="# Z5 status\n")
    captured, conn = _capturing_conn()
    launches: list[str] = []

    def fake_launcher(rid: str, text: str) -> dict:
        launches.append(rid)
        return {"ok": True, "run_id": rid}

    agent = MiniOrkAcpAgent(launcher=fake_launcher)
    agent.on_connect(conn)
    resp = asyncio.run(
        agent.prompt(run_id, [_text_block("/status")])
    )
    assert resp.stop_reason == "end_turn"
    assert launches == [], f"launcher should NOT run for a slash command, got {launches}"
    # The handler emitted one ``agent_message_chunk`` carrying the status body.
    chunks = [u for u in captured if isinstance(u, AgentMessageChunk)]
    assert chunks, f"expected a chunk, got {captured!r}"


def test_z5_prompt_unknown_slash_answers_without_launch(tmp_path: Path) -> None:
    """An unknown ``/foo`` returns the fixed one-liner and never launches."""
    proj, _home = _migrate_home(tmp_path)
    captured, conn = _capturing_conn()
    launches: list[str] = []

    def fake_launcher(rid: str, text: str) -> dict:
        launches.append(rid)
        return {"ok": True, "run_id": rid}

    agent = MiniOrkAcpAgent(launcher=fake_launcher)
    agent.on_connect(conn)
    resp = asyncio.run(
        agent.new_session(cwd=str(proj), run_id="run-z5-foo")
    )
    sid = resp.session_id
    captured.clear()
    resp2 = asyncio.run(agent.prompt(sid, [_text_block("/foo")]))
    assert resp2.stop_reason == "end_turn"
    assert launches == [], "unknown /foo must not launch"
    chunks = [u for u in captured if isinstance(u, AgentMessageChunk)]
    assert chunks
    assert "/help lists commands" in chunks[0].content.text


def test_z5_prompt_normal_text_still_launches(tmp_path: Path) -> None:
    """A normal (non-slash) prompt in a run session still goes to the launcher."""
    proj, _home = _migrate_home(tmp_path)
    launches: list[tuple[str, str]] = []

    def fake_launcher(rid: str, text: str) -> dict:
        launches.append((rid, text))
        return {"ok": True, "run_id": rid}

    def fake_reader(rid: str) -> dict:
        return {"status": "published", "events": [], "llm_calls": []}

    agent = MiniOrkAcpAgent(launcher=fake_launcher, reader=fake_reader)
    resp = asyncio.run(
        agent.prompt("run-z5-norm", [_text_block("fix the bug")])
    )
    assert resp.stop_reason == "end_turn"
    assert launches == [("run-z5-norm", "fix the bug")]


def test_z5_prompt_slash_run_still_launches(tmp_path: Path) -> None:
    """``/run fix x`` in a THREAD session falls through ``_strip_slash_run``.

    The carve-out is thread-only (run sessions launch any leading ``/`` text as
    the kickoff body). Verify the thread branch still strips ``/run `` and
    passes the remainder to the launcher.
    """
    proj, _home = _migrate_home(tmp_path)
    launches: list[tuple[str, str]] = []

    def fake_launcher(rid: str, text: str) -> dict:
        launches.append((rid, text))
        return {"ok": True, "run_id": rid}

    def fake_reader(rid: str) -> dict:
        return {"status": "published", "events": [], "llm_calls": []}

    agent = MiniOrkAcpAgent(launcher=fake_launcher, reader=fake_reader)
    new = asyncio.run(agent.new_session(cwd=str(proj)))
    thread_id = new.session_id
    resp = asyncio.run(
        agent.prompt(thread_id, [_text_block("/run fix x")])
    )
    assert resp.stop_reason == "end_turn"
    assert launches, "launcher must run for /run in a thread"
    assert launches[0][1] == "fix x", (
        f"expected kickoff body 'fix x', got {launches[0][1]!r}"
    )


def test_z5_loaded_session_slash_status_returns_end_turn(tmp_path: Path) -> None:
    """A loaded (attached) run session must respond to ``/status`` (kickoff §3)."""
    proj, home = _migrate_home(tmp_path)
    run_id = "run-z5-loaded"
    _seed_run(home, run_id, status="executing", kickoff_text="# Z5 loaded\n")
    agent = MiniOrkAcpAgent()
    agent.on_connect(_capturing_conn()[1])
    asyncio.run(agent.load_session(cwd=str(proj), session_id=run_id))
    captured, conn = _capturing_conn()
    agent2 = MiniOrkAcpAgent()
    agent2.on_connect(conn)
    # The second agent shares the same dispatch flow; ``load_session`` already
    # marked ``run_id`` as loaded — a follow-up prompt must short-circuit to
    # the slash handler before the refusal text.
    agent2._loaded.add(run_id)
    agent2._sessions[run_id] = str(proj)
    resp = asyncio.run(
        agent2.prompt(run_id, [_text_block("/status")])
    )
    assert resp.stop_reason == "end_turn"
    assert any(
        "Unknown command" not in (getattr(u, "content", None).text if getattr(u, "content", None) else "")
        for u in captured
        if isinstance(u, AgentMessageChunk)
    )


def test_z5_thread_slash_status_uses_thread_runs(tmp_path: Path) -> None:
    """A ``/status`` in a thread with a recent run reports that run."""
    proj, home = _migrate_home(tmp_path)
    run_id = "run-z5-thr-001"
    _seed_run(home, run_id, status="published", kickoff_text="# Z5 thread\n")
    agent = MiniOrkAcpAgent()
    agent.on_connect(_capturing_conn()[1])
    new = asyncio.run(agent.new_session(cwd=str(proj)))
    thread_id = new.session_id
    agent._thread_runs.setdefault(thread_id, []).append(run_id)
    captured, conn = _capturing_conn()
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt(thread_id, [_text_block("/status")]))
    assert resp.stop_reason == "end_turn"
    chunks = [u for u in captured if isinstance(u, AgentMessageChunk)]
    assert chunks
    assert run_id in chunks[0].content.text


def test_z5_thread_no_run_says_so(tmp_path: Path) -> None:
    """A thread with no run yet says so on ``/status``."""
    proj, _home = _migrate_home(tmp_path)
    agent = MiniOrkAcpAgent()
    agent.on_connect(_capturing_conn()[1])
    new = asyncio.run(agent.new_session(cwd=str(proj)))
    thread_id = new.session_id
    captured, conn = _capturing_conn()
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt(thread_id, [_text_block("/status")]))
    assert resp.stop_reason == "end_turn"
    chunks = [u for u in captured if isinstance(u, AgentMessageChunk)]
    assert chunks
    assert "No run in this thread yet" in chunks[0].content.text


def test_z5_thread_slash_does_not_record_prompt(tmp_path: Path) -> None:
    """A slash command must NOT land in the thread's user-prompt replay."""
    proj, _home = _migrate_home(tmp_path)
    agent = MiniOrkAcpAgent()
    agent.on_connect(_capturing_conn()[1])
    new = asyncio.run(agent.new_session(cwd=str(proj)))
    thread_id = new.session_id
    captured, conn = _capturing_conn()
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread_id, [_text_block("/status")]))
    # No UserMessageChunk should have been emitted for the slash prompt —
    # commands are read-only by contract and don't pollute replay.
    user_chunks = [
        u for u in captured
        if isinstance(u, UserMessageChunk)
        and "user_message_chunk" == getattr(u, "session_update", "")
    ]
    # The new_session path itself may emit one; the slash prompt must NOT add
    # another user_message_chunk with ``/status`` text.
    status_text = [u for u in user_chunks if "/status" in (u.content.text or "")]
    assert status_text == [], (
        f"slash prompt leaked into user replay: {status_text}"
    )


def test_a_thread_command_is_recorded_with_its_reply(tmp_path):
    """Reopening a thread replays '/runs' and its answer together, not an
    answer to nothing."""
    from mini_ork.acp.threads import ThreadStore

    proj = tmp_path / "proj"
    (proj / ".mini-ork").mkdir(parents=True)
    agent, thread = _thread_agent(proj)
    agent.on_connect(_SidConn())
    asyncio.run(agent.prompt(thread, [_text_block("/lanes")]))
    records = ThreadStore(proj / ".mini-ork").read(thread)
    kinds = [(r["type"], r.get("text") or r.get("update", {}).get("sessionUpdate")) for r in records
             if r["type"] in ("user", "update")]
    i = kinds.index(("user", "/lanes"))
    assert ("update", "agent_message_chunk") in kinds[i + 1:]


# ── Z6 plan emit (AgentPlanUpdate) ──────────────────────────────────────────


def _seed_plan(
    home: Path,
    run_id: str,
    decomposition: list[dict[str, Any]],
    *,
    recipe: str = "docs",
    created_at: int = 1000,
) -> None:
    """Seed the run row, a node_start event, AND a ``plan.json`` so
    ``_project_snapshot`` can render a non-empty plan. The decomposition
    matches the docs shape the lens uses as a canonical example."""
    _seed_run(home, run_id, status="executing", created_at=created_at)
    _seed_node_event(home, run_id, "planner")
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.json").write_text(
        json.dumps({"decomposition": decomposition}),
        encoding="utf-8",
    )


def _plan_updates(captured: list) -> list:
    """Filter ``captured`` to just the ``AgentPlanUpdate`` pushes."""
    return [u for u in captured if isinstance(u, AgentPlanUpdate)]


def test_run_session_emits_plan_on_each_status_change_and_none_on_unchanged_poll(
    tmp_path: Path,
) -> None:
    """D1-style dedup: the same snapshot projected twice emits one plan."""
    proj, home = _migrate_home(tmp_path)
    decomposition = [
        {"id": "planner", "description": "p", "node_type": "planner", "depends_on": []},
        {"id": "doc_editor", "description": "d", "node_type": "implementer", "depends_on": ["planner"]},
    ]
    run_id = "run-z6-1"
    _seed_plan(home, run_id, decomposition)

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent(home=home, poll_interval=0)
    agent.on_connect(conn)
    # Use a stable snapshot for the unchanged poll: the third projection
    # re-projectes the SAME snapshot the first projection saw → no new emit.
    snapshot_start_only = {
        "status": "executing",
        "events": [
            {
                "event_type": "node_start",
                "payload_json": {"node_id": "doc_editor", "node_type": "implementer"},
            }
        ],
        "llm_calls": [],
    }
    snapshot_with_end = {
        "status": "executing",
        "events": snapshot_start_only["events"]
        + [
            {
                "event_type": "node_end",
                "payload_json": {"node_id": "doc_editor", "node_type": "implementer"},
            }
        ],
        "llm_calls": [],
    }

    asyncio.run(agent._project_snapshot(run_id, snapshot_start_only))
    asyncio.run(agent._project_snapshot(run_id, snapshot_start_only))  # unchanged → no new emit
    asyncio.run(agent._project_snapshot(run_id, snapshot_with_end))  # status flipped

    plans = _plan_updates(captured)
    assert len(plans) == 2, [u.entries for u in plans]
    # First emit: planner completed (skipped because dependent fired),
    # doc_editor in_progress.
    first = plans[0].entries
    by_id = {e.content: e for e in first}
    assert by_id["planner (planner)"].status == "completed"
    assert by_id["doc_editor (implementer)"].status == "in_progress"
    # Second emit: doc_editor completed.
    second = plans[1].entries
    assert next(e for e in second if e.content == "doc_editor (implementer)").status == "completed"


def test_load_session_finished_run_replays_final_plan(tmp_path: Path) -> None:
    """Loading a finished run must show its final plan; the load-reset on
    ``_plan_emitted`` ensures the plan actually emits rather than deduping."""
    proj, home = _migrate_home(tmp_path)
    decomposition = [
        {"id": "planner", "description": "p", "node_type": "planner", "depends_on": []},
        {"id": "implementer", "description": "i", "node_type": "implementer", "depends_on": ["planner"]},
    ]
    run_id = "run-z6-2"
    _seed_run(home, run_id, status="published")
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text(
        json.dumps({"decomposition": decomposition}),
        encoding="utf-8",
    )

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent(home=home)
    agent.on_connect(conn)
    resp = asyncio.run(agent.load_session(cwd=str(proj), session_id=run_id))
    assert resp is not None

    plans = [u for u in captured if isinstance(u, AgentPlanUpdate)]
    assert plans, "load_session did not replay the run's final plan"
    statuses = [e.status for e in plans[-1].entries]
    assert statuses == ["pending", "pending"], plans[-1].entries


def test_routed_child_run_plan_addressed_to_thread_session(
    tmp_path: Path,
) -> None:
    """A child run followed inside a thread sees its plan re-addressed by
    ``_emit`` (the destination session is the thread, not the child run id)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    home = proj / ".mini-ork"
    home.mkdir()
    rc, out, err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"

    child = "run-z6-thread-1"
    decomposition = [
        {"id": "n1", "description": "n", "node_type": "implementer", "depends_on": []},
    ]
    # Pre-create the run_dir + plan.json so the projection pass can read it.
    run_dir = home / "runs" / child
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text(
        json.dumps({"decomposition": decomposition}),
        encoding="utf-8",
    )

    def fake_launcher(rid: str, _t: str) -> dict:
        return {"ok": True, "run_id": rid}

    def fake_reader(rid: str) -> dict:
        del rid
        return {
            "status": "executing",
            "events": [
                {
                    "event_type": "node_start",
                    "payload_json": {"node_id": "n1", "node_type": "implementer"},
                }
            ],
            "llm_calls": [],
        }

    agent, thread = _thread_agent(
        proj, launcher=fake_launcher, reader=fake_reader
    )
    conn = _SidConn()
    agent.on_connect(conn)

    # Wire a routed child directly (mirrors _start_child_follow's contract).
    agent._routes[child] = (thread, f"{child}:")
    agent._thread_runs.setdefault(thread, []).append(child)
    agent._sessions[child] = str(proj)

    asyncio.run(agent._project_snapshot(child, fake_reader(child)))

    plans = [(sid, update) for sid, update in conn.sent if isinstance(update, AgentPlanUpdate)]
    assert plans, "routed run produced no plan update"
    sid, update = plans[0]
    assert sid == thread, f"plan update addressed to {sid!r}, expected thread {thread!r}"
    # The projection's session_id argument IS the run; only the wire
    # destination is the thread.
    assert update.entries[0].content == "n1 (implementer)"


def test_thread_with_two_runs_only_latest_run_plan_emits(tmp_path: Path) -> None:
    """Older routed runs being followed must NOT overwrite the thread's plan.

    The gate is ``_thread_runs[thread][-1] != session_id`` — the older run's
    projection computes the same entries (it has the same decomposition +
    lifecycle), but the gate blocks the emit before the dedup set is touched.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    home = proj / ".mini-ork"
    home.mkdir()
    rc, out, err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"

    older = "run-z6-old"
    newer = "run-z6-new"
    decomposition = [
        {"id": "n1", "description": "n", "node_type": "implementer", "depends_on": []},
    ]
    for rid in (older, newer):
        run_dir = home / "runs" / rid
        run_dir.mkdir(parents=True)
        (run_dir / "plan.json").write_text(
            json.dumps({"decomposition": decomposition}),
            encoding="utf-8",
        )

    agent, thread = _thread_agent(proj)
    conn = _SidConn()
    agent.on_connect(conn)

    agent._routes[older] = (thread, f"{older}:")
    agent._routes[newer] = (thread, f"{newer}:")
    agent._thread_runs[thread] = [older, newer]
    agent._sessions[older] = str(proj)
    agent._sessions[newer] = str(proj)

    snapshot = {
        "status": "executing",
        "events": [
            {
                "event_type": "node_start",
                "payload_json": {"node_id": "n1", "node_type": "implementer"},
            }
        ],
        "llm_calls": [],
    }

    # Older run is NOT the latest → no plan push from this projection.
    asyncio.run(agent._project_snapshot(older, snapshot))
    plans_so_far = [
        (sid, u) for sid, u in conn.sent if isinstance(u, AgentPlanUpdate)
    ]
    assert plans_so_far == [], plans_so_far

    # Newer run IS the latest → plan pushes to the thread.
    asyncio.run(agent._project_snapshot(newer, snapshot))
    plans_now = [
        (sid, u) for sid, u in conn.sent if isinstance(u, AgentPlanUpdate)
    ]
    assert len(plans_now) == 1
    sid, update = plans_now[0]
    assert sid == thread
    assert update.entries[0].status == "in_progress"


def test_missing_plan_json_in_run_emits_no_plan_update(tmp_path: Path) -> None:
    """A run without a ``plan.json`` produces no ``AgentPlanUpdate``.

    ``plan_entries`` returns ``[]`` for the miss, the snapshot projection
    short-circuits — kickoff §``agent.py``: "Empty entries → nothing"."""
    proj, home = _migrate_home(tmp_path)
    run_id = "run-z6-empty"
    _seed_run(home, run_id, status="executing")
    # Intentionally NO plan.json under home/runs/run-z6-empty/.
    (home / "runs" / run_id).mkdir(parents=True)

    def fake_reader(_rid: str) -> dict:
        return {
            "status": "executing",
            "events": [
                {
                    "event_type": "node_start",
                    "payload_json": {"node_id": "n1", "node_type": "implementer"},
                }
            ],
            "llm_calls": [],
        }

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent(home=home, poll_interval=0)
    agent.on_connect(conn)
    snapshot = {
        "status": "executing",
        "events": [
            {
                "event_type": "node_start",
                "payload_json": {"node_id": "n1", "node_type": "implementer"},
            }
        ],
        "llm_calls": [],
    }
    asyncio.run(agent._project_snapshot(run_id, snapshot))

    assert _plan_updates(captured) == [], [u for u in captured]


# ── Z10: terminal-auth setup (kickoff §agent.py) ─────────────────────────────


@pytest.fixture(autouse=True)
def _skip_setup_check(monkeypatch):
    """Existing tests would otherwise hit the real ``claude auth status``
    call from the new ``new_session`` thread branch; the kickoff calls this
    out as opt-out via the env var.
    """
    monkeypatch.setenv("MO_ACP_SKIP_SETUP_CHECK", "1")
    yield


def test_initialize_advertises_terminal_auth_method():
    """``initialize`` advertises one TerminalAuthMethod (args=['acp','--setup'])."""
    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.initialize(protocol_version=1))
    methods = list(getattr(resp, "auth_methods", []) or [])
    assert len(methods) == 1
    method = methods[0]
    # Pydantic models expose ``type`` discriminator; the canonical id is
    # ``mini-ork-setup`` and the args literally route to ``acp --setup``.
    assert getattr(method, "id", None) == "mini-ork-setup"
    assert list(getattr(method, "args", []) or []) == ["acp", "--setup"]


def test_authenticate_succeeds_when_readiness_passes(tmp_path, monkeypatch):
    """``authenticate('mini-ork-setup')`` returns AuthenticateResponse on pass."""
    from acp.schema import AuthenticateResponse as _AuthResp

    from mini_ork.acp import agent as agent_mod
    from mini_ork.acp import setup as acp_setup

    monkeypatch.setattr(agent_mod, "_SETUP_PASS_CACHE", {})
    monkeypatch.setattr(acp_setup, "_run", lambda *a, **k: _FakeProc(0, stdout='{"loggedIn": true}'))
    monkeypatch.setattr(acp_setup.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setenv("MO_ACP_SKIP_SETUP_CHECK", "1")

    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.authenticate("mini-ork-setup"))
    assert isinstance(resp, _AuthResp)


def test_authenticate_raises_auth_required_when_readiness_fails(tmp_path, monkeypatch):
    """Failing orchestrator check raises ``RequestError.auth_required``."""
    from mini_ork.acp import agent as agent_mod
    from mini_ork.acp import setup as acp_setup

    monkeypatch.setattr(agent_mod, "_SETUP_PASS_CACHE", {})
    monkeypatch.setattr(acp_setup.shutil, "which", lambda name: None)
    monkeypatch.setenv("MO_ACP_SKIP_SETUP_CHECK", "1")

    agent = MiniOrkAcpAgent()
    with pytest.raises(RequestError) as excinfo:
        asyncio.run(agent.authenticate("mini-ork-setup"))
    # The kickoff contract: failure surfaces as ``auth_required`` so the
    # editor offers the terminal-auth setup.
    assert excinfo.value.code == RequestError.auth_required({}).code


def test_authenticate_rejects_unknown_method_id():
    agent = MiniOrkAcpAgent()
    with pytest.raises(RequestError):
        asyncio.run(agent.authenticate("not-a-real-method"))


def test_thread_new_session_raises_when_orchestrator_check_fails(monkeypatch):
    """Gate fires when ``MO_ACP_SKIP_SETUP_CHECK`` is unset and orchestrator fails."""
    from mini_ork.acp import agent as agent_mod
    from mini_ork.acp import setup as acp_setup

    monkeypatch.delenv("MO_ACP_SKIP_SETUP_CHECK", raising=False)
    monkeypatch.setattr(agent_mod, "_SETUP_PASS_CACHE", {})
    monkeypatch.setattr(acp_setup.shutil, "which", lambda name: None)

    agent = MiniOrkAcpAgent()
    with pytest.raises(RequestError):
        # Thread session (no client-minted run_id) — must trip the gate.
        asyncio.run(agent.new_session(cwd="/tmp/proj"))


def test_thread_new_session_skips_gate_when_env_opt_out(monkeypatch):
    """``MO_ACP_SKIP_SETUP_CHECK=1`` disables the gate; thread session proceeds."""
    monkeypatch.setenv("MO_ACP_SKIP_SETUP_CHECK", "1")
    from unittest.mock import patch

    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
    ):
        agent = MiniOrkAcpAgent()
        resp = asyncio.run(agent.new_session(cwd="/tmp/proj"))
    assert resp.session_id.startswith("orch-")


# ── helpers for the new tests ───────────────────────────────────────────────


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_thread_new_session_passes_and_caches_the_setup_check(tmp_path, monkeypatch):
    """A passing orchestrator check opens the thread and is reused: the second
    thread in the same project does not run `claude auth status` again."""
    from unittest.mock import patch

    from mini_ork.acp import agent as agent_mod
    from mini_ork.acp.setup import Check

    monkeypatch.delenv("MO_ACP_SKIP_SETUP_CHECK", raising=False)
    monkeypatch.setattr(agent_mod, "_SETUP_PASS_CACHE", {})
    calls: list[str] = []

    def readiness(cwd):
        calls.append(cwd)
        return [Check("project", True, "ok"), Check("orchestrator", True, "ok"), Check("lanes", True, "ok")]

    monkeypatch.setattr(agent_mod, "_setup_readiness_for_thread", readiness)
    proj = tmp_path / "proj"
    proj.mkdir()
    with patch("mini_ork.acp_orchestrator.config.orchestrator_lanes", return_value=[{"id": "opus", "name": "Opus"}]), \
         patch("mini_ork.recipes_catalog.list_recipes", return_value=[_recipe("docs")]):
        agent = MiniOrkAcpAgent()
        first = asyncio.run(agent.new_session(cwd=str(proj)))
        second = asyncio.run(agent.new_session(cwd=str(proj)))
    assert first.session_id.startswith("orch-") and second.session_id.startswith("orch-")
    assert len(calls) == 1


# ── task-state title push (Zed S1) ──────────────────────────────────────────


def _stage_diff_cache(home: Path, run_id: str, diffs: list[dict]) -> None:
    """Stage ``<home>/runs/<run_id>/acp-diffs.json`` so ``cached_or_computed`` hits the cache.

    Mirrors ``mini_ork.acp.task_state._diff_counts``'s read path —
    the cache is a JSON list of ``{path, old_text, new_text}`` triples.
    """
    import json as _json
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "acp-diffs.json").write_text(_json.dumps(diffs), encoding="utf-8")


def test_thread_run_executing_then_published_emits_two_titles_once_each(tmp_path):
    """A child run that flips executing → published emits ``● base`` then
    ``✓ base +a −b`` (each exactly once) and records both as ``title`` rows.

    The run dir hosts an empty diff cache so the ``+1 −1`` count comes
    from the cache (the test only verifies the rule fires; the diff
    count itself is covered by ``test_acp_task_state``).
    """
    from acp.schema import SessionInfoUpdate

    from mini_ork.acp import task_state as _ts

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()
    child = "run-child-ts"
    home = proj / ".mini-ork"

    def reader(rid: str) -> dict:
        assert rid == child
        return {
            "status": "executing",
            "events": [
                {
                    "event_type": "node_start",
                    "payload_json": json.dumps({"node_id": "implementer"}),
                }
            ],
            "llm_calls": [],
        }

    agent, thread = _thread_agent(proj, reader=reader)
    agent._routes[child] = (thread, f"{child}:")
    agent._thread_runs[thread] = [child]
    # Bind the child to the project cwd so ``_home_for`` resolves to
    # the staged run dir (where the diff cache lives).
    agent._sessions[child] = str(proj)
    # Seed the thread's base title (normally set on first prompt).
    agent._thread_titles[thread] = "Fix login"
    # Stage a 1-line diff cache so the published title shows +1 −0.
    _stage_diff_cache(
        home,
        child,
        [{"path": "f.py", "old_text": "a\n", "new_text": "a\nb\n"}],
    )

    conn = _SidConn()
    agent.on_connect(conn)

    async def _drive() -> None:
        # 1st projection: executing → "● Fix login"
        snap_exec = await asyncio.to_thread(reader, child)
        await agent._project_snapshot(child, snap_exec)
        # 2nd projection: published → "✓ Fix login +1 −0"
        def _published(_: str) -> dict:
            return {
                "status": "published",
                "events": [
                    {
                        "event_type": "node_start",
                        "payload_json": json.dumps({"node_id": "implementer"}),
                    },
                    {
                        "event_type": "node_end",
                        "payload_json": json.dumps(
                            {"node_id": "implementer", "finish_reason": "done"}
                        ),
                    },
                ],
                "llm_calls": [],
            }
        snap_done = await asyncio.to_thread(_published, child)
        await agent._project_snapshot(child, snap_done)

    asyncio.run(_drive())

    titles = [
        u.title for _, u in conn.sent if isinstance(u, SessionInfoUpdate)
    ]
    # Two distinct titles, in order, no duplicates.
    assert titles == [
        f"{_ts.MARKS['working']} Fix login",
        f"{_ts.MARKS['done']} Fix login +1 −0",
    ]
    # The title was persisted as a record on the thread JSONL.
    recs = _read_thread_records(proj, thread)
    title_records = [r for r in recs if r.get("type") == "title"]
    assert [r["title"] for r in title_records] == titles


def test_thread_newer_run_drives_title_and_older_does_not(tmp_path):
    """An older routed run that is no longer the thread's latest does NOT
    overwrite the latest's title; the latest's projection still drives it.
    """
    from acp.schema import SessionInfoUpdate

    from mini_ork.acp import task_state as _ts

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()
    older = "run-older"
    newer = "run-newer"
    home = proj / ".mini-ork"
    _stage_diff_cache(home, older, [])
    _stage_diff_cache(
        home,
        newer,
        [{"path": "f.py", "old_text": "a\n", "new_text": "a\nb\n"}],
    )

    agent, thread = _thread_agent(proj, reader=lambda _: {"status": None, "events": [], "llm_calls": []})
    agent._thread_titles[thread] = "Fix login"
    agent._thread_runs[thread] = [older, newer]
    agent._routes[older] = (thread, f"{older}:")
    agent._routes[newer] = (thread, f"{newer}:")
    # Bind both children to the project cwd so ``_home_for`` resolves
    # to the staged run dirs.
    agent._sessions[older] = str(proj)
    agent._sessions[newer] = str(proj)

    conn = _SidConn()
    agent.on_connect(conn)

    async def _drive() -> None:
        # Project the NEWER first (as the live cycle would).
        def _newer_snap(_: str) -> dict:
            return {
                "status": "published",
                "events": [
                    {
                        "event_type": "node_start",
                        "payload_json": json.dumps({"node_id": "implementer"}),
                    },
                    {
                        "event_type": "node_end",
                        "payload_json": json.dumps(
                            {"node_id": "implementer", "finish_reason": "done"}
                        ),
                    },
                ],
                "llm_calls": [],
            }
        await agent._project_snapshot(newer, _newer_snap(newer))
        # Now project the OLDER — its state would otherwise be working,
        # but the latest-run gate suppresses the title emit.
        def _older_snap(_: str) -> dict:
            return {
                "status": "executing",
                "events": [
                    {
                        "event_type": "node_start",
                        "payload_json": json.dumps({"node_id": "planner"}),
                    }
                ],
                "llm_calls": [],
            }
        await agent._project_snapshot(older, _older_snap(older))

    asyncio.run(_drive())
    titles = [
        u.title for _, u in conn.sent if isinstance(u, SessionInfoUpdate)
    ]
    # Only the newer run drove a title; the older run is gated out.
    assert titles == [f"{_ts.MARKS['done']} Fix login +1 −0"]


def test_thread_run_cost_paused_emits_needs_you_explanation_once(tmp_path):
    """A ``.cost-pause`` sentinel emits ``✋ <base>`` plus ONE agent
    message with the detail. A second projection of the same state is
    a no-op (the dedup key holds).
    """
    from acp.schema import AgentMessageChunk, SessionInfoUpdate

    from mini_ork.acp import task_state as _ts
    from mini_ork.acp.task_state import COST_PAUSE_DETAIL

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()
    child = "run-paused"
    home = proj / ".mini-ork"
    (home / "runs" / child).mkdir(parents=True)
    (home / "runs" / child / ".cost-pause").write_text("", encoding="utf-8")

    agent, thread = _thread_agent(proj, reader=lambda _: {"status": None, "events": [], "llm_calls": []})
    agent._thread_titles[thread] = "Fix login"
    agent._thread_runs[thread] = [child]
    agent._routes[child] = (thread, f"{child}:")
    # Bind the child to the project cwd so ``_home_for`` resolves to
    # the staged run dir (where the ``.cost-pause`` sentinel lives).
    agent._sessions[child] = str(proj)

    conn = _SidConn()
    agent.on_connect(conn)

    def _snap(_: str) -> dict:
        return {
            "status": "executing",
            "events": [
                {
                    "event_type": "node_start",
                    "payload_json": json.dumps({"node_id": "implementer"}),
                }
            ],
            "llm_calls": [],
        }

    async def _drive() -> None:
        await agent._project_snapshot(child, _snap(child))
        # Second projection of the same state: dedup means no extra emits.
        await agent._project_snapshot(child, _snap(child))

    asyncio.run(_drive())
    title_emits = [
        u.title for _, u in conn.sent if isinstance(u, SessionInfoUpdate)
    ]
    detail_emits = [
        blk.text
        for _, u in conn.sent
        if isinstance(u, AgentMessageChunk)
        for blk in (u.content,)
        if isinstance(blk, TextContentBlock) and blk.text == COST_PAUSE_DETAIL
    ]
    assert title_emits == [f"{_ts.MARKS['needs_you']} Fix login"]
    # One detail message total (the 2nd projection is deduped).
    assert len(detail_emits) == 1


def test_list_sessions_marks_run_rows_with_run_mark(tmp_path):
    """Run rows in ``list_sessions`` carry the ``run_mark`` prefix.

    A published run gets the done mark; a thread row carries the
    latest ``title`` record's text (when present) so a load replays
    the same string the live session last sent.
    """
    from mini_ork.acp.threads import ThreadStore

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mini-ork").mkdir()

    # Thread row with a ``title`` record (Zed S1: latest wins).
    ThreadStore(proj / ".mini-ork").append(
        "orch-1700000050-list",
        {"type": "meta", "thread_id": "orch-1700000050-list", "cwd": str(proj)},
    )
    ThreadStore(proj / ".mini-ork").append(
        "orch-1700000050-list",
        {"type": "user", "text": "thread title"},
    )
    ThreadStore(proj / ".mini-ork").append(
        "orch-1700000050-list",
        {"type": "title", "title": "✓ thread title +3 −1"},
    )

    # Run row — published, with a diff cache that adds 3, removes 1.
    from mini_ork.stores import migrate as mig
    db_path = proj / ".mini-ork" / "state.db"
    mig.init_db(str(db_path))
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO task_runs(id, task_class, recipe, status, kickoff_path, "
        "cost_usd, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run-list-2",
            "code_fix",
            "code-fix",
            "published",
            str(proj / ".mini-ork" / "runs-inbox" / "run-list-2.md"),
            0.0,
            1_700_000_200,
            1_700_000_210,
        ),
    )
    con.commit()
    con.close()
    _stage_diff_cache(
        proj / ".mini-ork",
        "run-list-2",
        [
            {"path": "a.py", "old_text": "x\ny\nz\nw\n", "new_text": "x\ny\nZ\nw\nN\nM\n"},
        ],
    )

    agent = MiniOrkAcpAgent()
    resp = asyncio.run(agent.list_sessions(cwd=str(proj)))
    by_id = {s.session_id: s for s in resp.sessions}
    # Thread row carries the latest ``title`` record, not the first prompt.
    assert by_id["orch-1700000050-list"].title == "✓ thread title +3 −1"
    # Run row is prefixed with the done mark (published → ✓).
    assert by_id["run-list-2"].title.startswith("✓ ")
    assert "run-list-2" in by_id["run-list-2"].title or "code-fix" in by_id["run-list-2"].title
    # The run row's title is NOT just the bare kickoff line — the mark
    # glyph must be present.
    assert "✓" in by_id["run-list-2"].title


# ── /recipe reply emits ResourceContentBlock chunks (Zed S3a) ────────────


def test_z5_recipe_reply_emits_resource_link_chunks(tmp_path, monkeypatch):
    """A ``/recipe <id>`` reply emits ONE text chunk first, then one
    ``ResourceContentBlock(type="resource_link")`` chunk per linked path.
    The order matters — Zed renders the resource links under the markdown."""
    proj = tmp_path / "proj"
    proj.mkdir()
    # Engine recipes catalog: a single recipe with one workflow.yaml +
    # one task_class.yaml + one artifact_contract.yaml. Two prompt files
    # + one verifier, so the resource-link chunk count is deterministic.
    engine = tmp_path / "engine"
    engine.mkdir()
    rd = engine / "recipes" / "docs"
    rd.mkdir(parents=True)
    (rd / "workflow.yaml").write_text("name: docs\n", encoding="utf-8")
    (rd / "task_class.yaml").write_text(
        "name: docs\ndescription: Project docs\n", encoding="utf-8"
    )
    (rd / "artifact_contract.yaml").write_text(
        "expected_artifact: diff\n", encoding="utf-8"
    )
    prompts = rd / "prompts"
    prompts.mkdir()
    (prompts / "planner.md").write_text("# planner\n", encoding="utf-8")
    (prompts / "implementer.md").write_text("# impl\n", encoding="utf-8")
    verifiers = rd / "verifiers"
    verifiers.mkdir()
    (verifiers / "test.py").write_text("pass\n", encoding="utf-8")
    (rd / "example-kickoff.md").write_text("# Example\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: engine)

    captured, conn = _capturing_conn()
    agent = MiniOrkAcpAgent()
    agent.on_connect(conn)
    resp = asyncio.run(agent.new_session(cwd=str(proj)))
    sid = resp.session_id
    captured.clear()
    turn = asyncio.run(agent.prompt(sid, [_text_block("/recipe docs")]))
    assert turn.stop_reason == "end_turn"

    chunks = [u for u in captured if isinstance(u, AgentMessageChunk)]
    # Exactly one text chunk first, then one ResourceContentBlock per file.
    assert len(chunks) >= 2, f"expected text + resource chunks, got {len(chunks)}"
    first = chunks[0]
    assert isinstance(first.content, TextContentBlock)
    assert "**docs**" in first.content.text
    # The remaining chunks are ``ResourceContentBlock`` with the file links.
    link_chunks = [
        c for c in chunks[1:]
        if isinstance(c.content, ResourceContentBlock)
        and c.content.type == "resource_link"
    ]
    assert len(link_chunks) >= 1
    # Each link chunk carries a ``file://`` URI and a name.
    for c in link_chunks:
        assert c.content.uri.startswith("file://")
        assert c.content.name



class _StubResult:
    """Minimal stand-in for the orchestrator turn result tuple."""

    def __init__(self, *, rc=0, session_id=None, cost_usd=0.0,
                 error="", text=""):
        self.rc = rc
        self.session_id = session_id
        self.cost_usd = cost_usd
        self.error = error
        self.text = text


class _StubConn:
    """Stub ACP client connection: accepts session_update + request_permission."""

    def __init__(self, request_permission) -> None:
        self._rp = request_permission

    async def session_update(self, _sid, _update):
        return None

    async def request_permission(self, *, session_id, tool_call, options):
        return await self._rp(session_id, tool_call, options)





# ── /recipe new|edit sentinel routing + draft approval (S3b-2) ──────────────


class _FakeRequestPermission:
    """Stub for ``conn.request_permission``; records the tool_call + options.

    The next response is consumed FIFO; tests push the answers they want back.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.responses: list[Any] = []

    async def __call__(self, session_id: str, tool_call, options):
        ids = [o.option_id for o in options]
        self.calls.append((tool_call.tool_call_id, ids))
        if not self.responses:
            return _StubOutcome(None)
        return self.responses.pop(0)


class _StubOutcome:
    """Mimics ACP ``RequestPermissionResponse.outcome`` shape."""

    def __init__(self, option_id: str | None) -> None:
        self.outcome = (
            type("_O", (), {"option_id": option_id}) if option_id is not None else None
        )






















# ── S3b-2: guided recipe creation (review rewrite) ──────────────────────────


class _PermConn(_SidConn):
    """Records updates and answers request_permission with scripted choices
    (an option id, or None for a dismissed dialog)."""

    def __init__(self, choices: list[str | None]) -> None:
        super().__init__()
        self.choices = list(choices)
        self.asked: list[tuple[str, list[str]]] = []

    async def request_permission(self, session_id, tool_call, options, **kw):
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        self.asked.append((tool_call.tool_call_id, [o.name for o in options]))
        choice = self.choices.pop(0) if self.choices else None
        outcome = (DeniedOutcome(outcome="cancelled") if choice is None
                   else AllowedOutcome(outcome="selected", option_id=choice))
        return RequestPermissionResponse(outcome=outcome)


_AUDIT_SPEC = {
    "id": "migration-audit", "description": "Audit a SQL migration.",
    "keywords": ["audit migration"], "input": "A migration file path.",
    "steps": [
        {"id": "editor", "type": "implementer", "role": "worker", "instructions": "Fix the migration."},
        {"id": "check", "type": "verifier", "check": "true", "after": ["editor"]},
    ],
    "example_kickoff": "# Audit migration 0042\n\n## Files in scope\n\n- db/0042.sql\n",
}


def _authoring_project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    home = proj / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text("lanes:\n  worker: sonnet\n  reviewer: opus\n")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    engine = tmp_path / "engine"
    docs = engine / "recipes" / "docs"
    (docs / "prompts").mkdir(parents=True)
    (docs / "workflow.yaml").write_text("version: '0.1.0'\ntask_class: docs\nnodes: []\nedges: []\n")
    (docs / "task_class.yaml").write_text("name: docs\ndescription: Docs edits.\n")
    (docs / "prompts" / "editor.md").write_text("edit docs\n")
    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: engine)
    return proj, home


def _drafting_turn(home, seen_prompts, *, draft=True):
    """A fake orchestrator turn: optionally calls the real draft_recipe (as the
    MCP server would) and streams its tool_use + tool_result."""
    from mini_ork import recipe_author

    def turn(lane, prompt, cwd, home_arg, resume, on_event):
        async def _run():
            seen_prompts.append(prompt)
            if draft:
                result = recipe_author.draft(home, _AUDIT_SPEC)
                await on_event({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "toolu_d1", "name": "mcp__mini-ork__draft_recipe",
                     "input": {"spec": _AUDIT_SPEC}}]}})
                await on_event({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "toolu_d1",
                     "content": [{"type": "text", "text": json.dumps(result)}]}]}})
            else:
                await on_event({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "What should a run receive?"}]}})
            return _Turn()
        return _run()
    return lambda lane, prompt, cwd, home, resume, on_event: turn(lane, prompt, cwd, home, resume, on_event)


def test_a_drafted_recipe_shows_as_diffs_and_create_commits_it(tmp_path, monkeypatch):
    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_drafting_turn(home, prompts))
    conn = _PermConn(["create", "later"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("I want a recipe that audits migrations")]))
    updates = [u for _, u in conn.sent]
    preview = [u for u in updates if isinstance(u, ToolCallProgress) and u.tool_call_id == "toolu_d1" and u.content]
    diffs = [c for u in preview for c in u.content if isinstance(c, FileEditToolCallContent)]
    assert any(d.path.endswith("recipes/migration-audit/workflow.yaml") for d in diffs)
    assert all(d.old_text is None for d in diffs)  # a new recipe
    assert any("Grade A" in getattr(getattr(c, "content", None), "text", "")
               for u in preview for c in u.content)
    assert conn.asked[0] == ("toolu_d1", ["Create recipe", "Change something", "Discard draft"])
    assert conn.asked[1][1] == ["Test it now", "Not now"]
    assert (home / "recipes" / "migration-audit" / "workflow.yaml").is_file()
    assert not (home / "recipe-drafts" / "migration-audit").exists()
    texts = [getattr(u.content, "text", "") for u in updates if isinstance(u, AgentMessageChunk)]
    assert any("Created `.mini-ork/recipes/migration-audit`" in t for t in texts)
    links = [u.content.name for u in updates if isinstance(u, AgentMessageChunk)
             and isinstance(u.content, ResourceContentBlock)]
    assert "workflow.yaml" in links and "prompts/editor.md" in links


def test_no_draft_means_no_buttons_even_after_recipe_new(tmp_path, monkeypatch):
    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_drafting_turn(home, prompts, draft=False))
    conn = _PermConn([])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/recipe new audit migrations")]))
    assert conn.asked == []
    assert "create a new recipe" in prompts[0] and "audit migrations" in prompts[0]
    titles = [u.title for _, u in conn.sent if isinstance(u, SessionInfoUpdate)]
    assert titles and titles[0] == "New recipe: audit migrations"
    from mini_ork.acp.threads import ThreadStore
    users = [r["text"] for r in ThreadStore(home).read(thread) if r["type"] == "user"]
    assert users == ["/recipe new audit migrations"]


def test_change_keeps_the_draft_and_the_users_next_message(tmp_path, monkeypatch):
    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_drafting_turn(home, prompts))
    conn = _PermConn(["change", "discard"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("make me a migration audit recipe")]))
    assert (home / "recipe-drafts" / "migration-audit").is_dir()
    asyncio.run(agent.prompt(thread, [_text_block("make the check run alembic check")]))
    assert prompts[-1] == "make the check run alembic check"
    assert not (home / "recipe-drafts" / "migration-audit").exists()  # discarded on the 2nd draft


def test_dismissing_the_dialog_keeps_the_draft(tmp_path, monkeypatch):
    proj, home = _authoring_project(tmp_path, monkeypatch)
    agent, thread = _thread_agent(proj, orchestrator_turn=_drafting_turn(home, []))
    conn = _PermConn([None])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("draft it")]))
    assert (home / "recipe-drafts" / "migration-audit").is_dir()
    texts = [getattr(u.content, "text", "") for _, u in conn.sent if isinstance(u, AgentMessageChunk)]
    assert any("Draft kept" in t for t in texts)


def test_test_it_now_runs_the_example_kickoff(tmp_path, monkeypatch):
    proj, home = _authoring_project(tmp_path, monkeypatch)
    launched: list[tuple[str, str]] = []

    def launcher(run_id, kickoff):
        launched.append((run_id, kickoff))
        return {"ok": True}

    agent, thread = _thread_agent(
        proj, orchestrator_turn=_drafting_turn(home, []), launcher=launcher,
        reader=lambda rid: {"status": "published", "events": [], "llm_calls": []})
    agent.on_connect(_PermConn(["create", "test"]))
    asyncio.run(agent.prompt(thread, [_text_block("draft it")]))
    (run_id, kickoff), = launched
    assert agent._recipes[run_id] == "migration-audit"
    assert kickoff.startswith("# Audit migration 0042")


def test_recipe_edit_of_an_engine_recipe_offers_a_project_copy(tmp_path, monkeypatch):
    proj, home = _authoring_project(tmp_path, monkeypatch)
    agent, thread = _thread_agent(proj)
    conn = _PermConn(["copy"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/recipe edit docs")]))
    assert conn.asked[0][1] == ["Copy into this project", "Cancel"]
    assert (home / "recipes" / "docs" / "workflow.yaml").is_file()
    links = [u.content.name for _, u in conn.sent if isinstance(u, AgentMessageChunk)
             and isinstance(u.content, ResourceContentBlock)]
    assert "prompts/editor.md" in links


def test_recipe_edit_of_a_spec_recipe_goes_to_the_orchestrator(tmp_path, monkeypatch):
    from mini_ork import recipe_author

    proj, home = _authoring_project(tmp_path, monkeypatch)
    recipe_author.draft(home, _AUDIT_SPEC)
    recipe_author.commit_draft(home, "migration-audit")
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_drafting_turn(home, prompts, draft=False))
    agent.on_connect(_PermConn([]))
    asyncio.run(agent.prompt(thread, [_text_block("/recipe edit migration-audit")]))
    assert "get_recipe_spec" in prompts[0] and 'base="migration-audit"' in prompts[0]


# ── workspace config + direct-mode worktree (Zed S4) ───────────────────────


def test_workspace_option_default_is_worktree(tmp_path):
    """A fresh thread session carries ``workspace="worktree"`` by default."""
    agent, _conn, sid = _make_thread_agent(tmp_path)
    assert agent._thread_config[sid]["workspace"] == "worktree"


def test_workspace_option_appears_in_picker_order(tmp_path):
    """The picker list ends with the workspace option (after mode/model/recipe)."""
    from unittest.mock import patch

    agent, _conn, sid = _make_thread_agent(tmp_path)
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=[{"id": "opus", "name": "Opus"}],
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe("code-fix")],
    ):
        resp = asyncio.run(agent.set_config_option("workspace", sid, "in-place"))
    assert resp.config_options is not None
    ids = [opt.id for opt in resp.config_options]
    assert ids == ["mode", "model", "recipe", "workspace"]
    ws = next(opt for opt in resp.config_options if opt.id == "workspace")
    assert ws.current_value == "in-place"
    assert {opt.value for opt in ws.options} == {"worktree", "in-place"}
    assert agent._thread_config[sid]["workspace"] == "in-place"


def test_set_config_option_rejects_unknown_workspace(tmp_path):
    agent, _conn, sid = _make_thread_agent(tmp_path)
    try:
        asyncio.run(agent.set_config_option("workspace", sid, "ssh-tunnel"))
    except Exception as exc:
        assert getattr(exc, "code", None) == -32602
        assert "unknown workspace" in str(getattr(exc, "data", None))
    else:
        raise AssertionError("expected invalid_params")


def test_workspace_default_honors_mo_workspace_mode_env(tmp_path, monkeypatch):
    """``MO_WORKSPACE_MODE=in-place`` is the default until the user changes it."""
    from unittest.mock import patch

    monkeypatch.setenv("MO_WORKSPACE_MODE", "in-place")
    proj = tmp_path / "proj"
    proj.mkdir()
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=[{"id": "opus", "name": "Opus"}],
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe("code-fix")],
    ):
        agent = MiniOrkAcpAgent()
        resp = asyncio.run(agent.new_session(cwd=str(proj)))
    sid = resp.session_id
    assert agent._thread_config[sid]["workspace"] == "in-place"


def test_direct_mode_worktree_creates_and_routes_to_worktree_path(tmp_path, monkeypatch):
    """``workspace=worktree`` mints a worktree (create monkeypatched) and the
    default launcher reads its path through ``self._sessions[new_run_id]``."""
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    # ``workspaces.create`` is monkeypatched to a stub that records the
    # args and returns a fake workspace with a sentinel path.
    captured: dict = {}

    class _FakeWS:
        def __init__(self, path, branch):
            self.path = path
            self.branch = branch

    def fake_create(project, home, run_id):
        captured["project"] = str(project)
        captured["home"] = str(home)
        captured["run_id"] = run_id
        return _FakeWS(path=tmp_path / "ws", branch=f"mini-ork/{run_id}")

    launched: dict = {}

    def fake_launch(self, run_id, kickoff_text):
        launched["run_id"] = run_id
        launched["cwd"] = self._sessions.get(run_id)
        launched["recipe"] = self._recipes.get(run_id)
        return {"ok": True, "run_id": run_id}

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
    ), patch(
        "mini_ork.workspaces.create", fake_create
    ), patch.object(MiniOrkAcpAgent, "_launch", fake_launch):
        agent = MiniOrkAcpAgent(reader=lambda _r: {"status": "published", "events": [], "llm_calls": []}, poll_interval=0)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))
        sid = resp.session_id
        asyncio.run(agent.set_config_option("mode", sid, "direct"))
        turn_resp = asyncio.run(agent.prompt(sid, [_text_block("do it")]))

    assert turn_resp.stop_reason == "end_turn"
    # workspaces.create was called with the project root and the thread's home.
    assert captured["project"] == str(proj)
    assert captured["run_id"] == launched["run_id"]
    # The launcher received the worktree path, NOT the project root.
    assert launched["cwd"] == str(tmp_path / "ws")
    assert launched["cwd"] != str(proj)


def test_direct_mode_in_place_skips_workspaces_create(tmp_path, monkeypatch):
    """``workspace=in-place`` MUST NOT call ``workspaces.create``."""
    from unittest.mock import patch

    proj = tmp_path / "proj"
    proj.mkdir()
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    create_calls: list = []

    def fake_create(project, home, run_id):
        create_calls.append(run_id)
        raise AssertionError("workspaces.create must not run for in-place")

    launched: dict = {}

    def fake_launch(self, run_id, kickoff_text):
        launched["cwd"] = self._sessions.get(run_id)
        return {"ok": True, "run_id": run_id}

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
    ), patch(
        "mini_ork.workspaces.create", fake_create
    ), patch.object(MiniOrkAcpAgent, "_launch", fake_launch):
        agent = MiniOrkAcpAgent(reader=lambda _r: {"status": "published", "events": [], "llm_calls": []}, poll_interval=0)
        resp = asyncio.run(agent.new_session(cwd=str(proj)))
        sid = resp.session_id
        asyncio.run(agent.set_config_option("mode", sid, "direct"))
        asyncio.run(agent.set_config_option("workspace", sid, "in-place"))
        turn_resp = asyncio.run(agent.prompt(sid, [_text_block("do it")]))

    assert turn_resp.stop_reason == "end_turn"
    assert create_calls == []
    assert launched["cwd"] == str(proj)


def test_workspace_round_trips_through_load_session(tmp_path):
    """A persisted ``workspace`` value is replayed on ``load_session``."""
    from unittest.mock import patch

    agent, _conn, sid = _make_thread_agent(tmp_path)
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=[{"id": "opus", "name": "Opus"}],
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe("code-fix")],
    ):
        asyncio.run(agent.set_config_option("workspace", sid, "in-place"))

    # Build a second agent that re-loads the same store; expect the same value.
    agent2 = MiniOrkAcpAgent()
    cwd = list(agent._sessions.values())[0]
    asyncio.run(agent2.load_session(cwd, sid))
    assert agent2._thread_config[sid]["workspace"] == "in-place"


# ── Zed S5 — review buttons (Merge / Discard / Keep) ──────────────────────────


def _init_repo_with_worktree(tmp_path: Path, *, run_id: str, home: Path | None = None):
    """A real temp git repo with one workspace commit on a worktree branch.

    Returns ``(project, home, ws)``. Mirrors the helper in
    ``tests/unit/test_workspaces.py`` but inlines the imports to keep
    the test file standalone. If ``home`` is provided it is used as the
    workspace home (so the agent's ``_home_for(session_id)`` resolves to
    the right place); otherwise we default to ``<proj>/.mini-ork``.
    """
    import subprocess
    from mini_ork import workspaces as ws_mod

    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=project, check=True,
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=project, check=True,
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=project, check=True, capture_output=True, text=True,
    )
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True,
                    capture_output=True, text=True)
    if home is None:
        home = project / ".mini-ork"
        home.mkdir(parents=True, exist_ok=True)
    ws = ws_mod.create(project, home, run_id)
    (ws.path / "new.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "new.txt"], cwd=ws.path, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "feat"], cwd=ws.path, check=True,
                    capture_output=True, text=True)
    return project, home, ws


class _ReviewConn(_SidConn):
    """S5 review-button fake: records request_permission calls and returns
    a single scripted outcome option id. Distinct from ``_PermConn`` above
    because the S5 surface uses raw ACP option objects with
    ``.option_id`` / ``.kind`` rather than the legacy ``.name`` shape."""

    def __init__(self, outcome_id: str | None = "merge") -> None:
        super().__init__()
        self.outcome_id = outcome_id
        self.calls: list[dict[str, Any]] = []

    async def request_permission(
        self,
        *,
        session_id: str,
        tool_call: Any,
        options: list[Any],
    ) -> Any:
        from acp.schema import AllowedOutcome, RequestPermissionResponse

        self.calls.append(
            {
                "session_id": session_id,
                "tool_call_id": tool_call.tool_call_id,
                "title": tool_call.title,
                "option_ids": [o.option_id for o in options],
                "kinds": [o.kind for o in options],
            }
        )
        if self.outcome_id is None:
            return None
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=self.outcome_id)
        )


async def _drive_prompt_to_terminal(agent: MiniOrkAcpAgent, run_id: str, sid: str) -> None:
    """Run a prompt that ends up terminal (status=published); returns when done."""
    await agent.prompt(run_id, [_text_block("do it")])


def test_direct_mode_ready_to_review_offers_buttons_on_run_id_parent(
    tmp_path, monkeypatch
):
    """A direct-mode ``/run`` that lands ready-to-review gets Merge / Discard /
    Keep buttons on the run's own ``<run_id>:parent`` tool card."""
    from unittest.mock import patch

    run_id = "run-s5-001"
    project, home, _ = _init_repo_with_worktree(tmp_path, run_id=run_id)
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    # ``mint_run_id`` mints fresh ids on each call; pin to the run id we
    # pre-created the workspace for so the path matches.
    monkeypatch.setattr(
        "mini_ork.acp.agent.mint_run_id", lambda: run_id
    )
    # ``_prompt_thread_direct`` requires ``<home>/runs/<run_id>`` to exist
    # for ``task_state`` to resolve the run_dir.
    (home / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    # Reader returns published immediately so the first poll terminates.
    def reader(rid: str) -> dict:
        return {"status": "published", "events": [], "llm_calls": []}

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
    ):
        agent = MiniOrkAcpAgent(
            reader=reader,
            poll_interval=0,
            launcher=lambda rid, _kick: {"ok": True, "run_id": rid},
        )
        resp = asyncio.run(agent.new_session(cwd=str(project)))
    sid = resp.session_id

    conn = _ReviewConn(outcome_id="keep")
    agent.on_connect(conn)
    asyncio.run(agent.prompt(sid, [_text_block("/run do it")]))

    # Exactly one permission call, on the run's marker, with three options.
    assert len(conn.calls) == 1
    call = conn.calls[0]
    assert call["tool_call_id"] == f"{run_id}:parent"
    assert call["session_id"] == sid
    assert call["option_ids"] == ["merge", "discard", "keep"]
    assert call["kinds"] == ["allow_once", "reject_always", "reject_once"]
    # Keep → worktree stays.
    from mini_ork import workspaces as ws_mod
    assert ws_mod.load(home, run_id) is not None


def test_direct_mode_discard_choice_removes_worktree(tmp_path, monkeypatch):
    from unittest.mock import patch

    run_id = "run-s5-002"
    project, home, _ = _init_repo_with_worktree(tmp_path, run_id=run_id)
    monkeypatch.setattr("mini_ork.acp.agent.mint_run_id", lambda: run_id)
    (home / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
    ):
        agent = MiniOrkAcpAgent(
            reader=lambda _r: {"status": "published", "events": [], "llm_calls": []},
            poll_interval=0,
            launcher=lambda rid, _kick: {"ok": True, "run_id": rid},
        )
        resp = asyncio.run(agent.new_session(cwd=str(project)))

    conn = _ReviewConn(outcome_id="discard")
    agent.on_connect(conn)
    asyncio.run(agent.prompt(resp.session_id, [_text_block("/run do it")]))

    from mini_ork import workspaces as ws_mod
    assert ws_mod.load(home, run_id) is None


def test_direct_mode_merge_choice_fast_forwards_into_project(tmp_path, monkeypatch):
    from unittest.mock import patch

    run_id = "run-s5-003"
    project, home, _ = _init_repo_with_worktree(tmp_path, run_id=run_id)
    monkeypatch.setattr("mini_ork.acp.agent.mint_run_id", lambda: run_id)
    (home / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
    ):
        agent = MiniOrkAcpAgent(
            reader=lambda _r: {"status": "published", "events": [], "llm_calls": []},
            poll_interval=0,
            launcher=lambda rid, _kick: {"ok": True, "run_id": rid},
        )
        resp = asyncio.run(agent.new_session(cwd=str(project)))

    conn = _ReviewConn(outcome_id="merge")
    agent.on_connect(conn)
    asyncio.run(agent.prompt(resp.session_id, [_text_block("/run do it")]))

    # The file from the worktree is now on the project branch.
    assert (project / "new.txt").is_file()
    from mini_ork import workspaces as ws_mod
    assert ws_mod.load(home, run_id) is None


def test_direct_mode_merge_refused_reports_and_keeps_worktree(
    tmp_path, monkeypatch
):
    """A merge refused because the project has a dirty overlapping path is
    surfaced to the user; the worktree stays."""
    import subprocess
    from unittest.mock import patch

    run_id = "run-s5-004"
    project, home, _ = _init_repo_with_worktree(tmp_path, run_id=run_id)
    # Make the project dirty on the SAME file the worktree touched.
    (project / "new.txt").write_text("local edit\n", encoding="utf-8")
    subprocess.run(["git", "add", "new.txt"], cwd=project, check=True,
                    capture_output=True, text=True)

    monkeypatch.setattr("mini_ork.acp.agent.mint_run_id", lambda: run_id)
    (home / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
    ):
        agent = MiniOrkAcpAgent(
            reader=lambda _r: {"status": "published", "events": [], "llm_calls": []},
            poll_interval=0,
            launcher=lambda rid, _kick: {"ok": True, "run_id": rid},
        )
        resp = asyncio.run(agent.new_session(cwd=str(project)))

    conn = _ReviewConn(outcome_id="merge")
    agent.on_connect(conn)
    asyncio.run(agent.prompt(resp.session_id, [_text_block("/run do it")]))

    from mini_ork import workspaces as ws_mod
    # The workspace stays; the user can fix and /merge again.
    assert ws_mod.load(home, run_id) is not None
    # Cleanup so subsequent tests are not affected.
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=project, check=True,
                    capture_output=True, text=True)


def test_child_run_ending_after_turn_emits_one_time_message_only(
    tmp_path, monkeypatch
):
    """An orchestrator child that finishes AFTER the orchestrator's turn
    ended gets the one-time slash-command hint and NO request_permission."""
    from unittest.mock import patch

    project = tmp_path / "proj"
    project.mkdir()
    fake_lanes = [{"id": "opus", "name": "Opus"}]
    fake_recipes = ["code-fix"]

    child_run_id = "run-s5-child"

    # First reader call: orchestrator's tool_result for start_run → create workspace.
    # Then a few polls returning "executing", then "published" with a workspace.
    call = {"n": 0}

    def fake_reader(rid: str) -> dict:
        if rid == child_run_id:
            call["n"] += 1
            return {"status": "published", "events": [], "llm_calls": []}
        return {"status": None, "events": [], "llm_calls": []}

    class _TurnResult:
        session_id = "sess-1"
        rc = 0
        text = ""
        cost_usd = 0.0
        error = ""

    def fake_turn(lane, prompt, cwd, home, resume, on_event):
        async def _run() -> Any:
            await on_event({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "tool-1",
                     "name": "mcp__mini-ork__start_run", "input": {"recipe": "code-fix"}}
                ]},
            })
            await on_event({
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result", "tool_use_id": "tool-1",
                    "is_error": False,
                    "content": [{"type": "text", "text": json.dumps({"run_id": child_run_id})}],
                }]},
            })
            # Turn ends BEFORE the child finishes (the orchestrator returned).
            return _TurnResult()

        return _run()

    # Build a real workspace BEFORE the prompt so the child run lands on
    # the ready-to-review path; the orchestrator's start_run normally would
    # create it, but the test bypasses that.
    import subprocess
    from mini_ork import workspaces as ws_mod

    home_dir = project / ".mini-ork"
    home_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=project, check=True,
        capture_output=True, text=True,
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=project, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=project, check=True,
                    capture_output=True, text=True)
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True,
                    capture_output=True, text=True)
    ws = ws_mod.create(project, home_dir, child_run_id)
    (ws.path / "new.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "new.txt"], cwd=ws.path, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "feat"], cwd=ws.path, check=True,
                    capture_output=True, text=True)

    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=fake_lanes,
    ), patch(
        "mini_ork.recipes_catalog.list_recipes",
        return_value=[_recipe(n) for n in fake_recipes],
    ):
        agent = MiniOrkAcpAgent(
            orchestrator_turn=fake_turn,
            reader=fake_reader,
            poll_interval=0,
        )
        resp = asyncio.run(agent.new_session(cwd=str(project)))
    conn = _ReviewConn(outcome_id="merge")  # would fail the test if called
    agent.on_connect(conn)

    async def _drive():
        await agent.prompt(resp.session_id, [_text_block("launch")])
        # Drain the child follower so it sees the terminal status.
        follower = agent._followers.get(child_run_id)
        if follower is not None:
            await follower

    asyncio.run(_drive())

    # request_permission was NEVER called — the orchestrator's turn ended
    # before the child, so the buttons don't fit in an active turn.
    assert conn.calls == []
    # The workspace is still open (the message did not mutate it).
    assert ws_mod.load(home_dir, child_run_id) is not None


# ── S6b-2: automation proposal card + scheduler offer ────────────────────────


_PROPOSAL_SPEC = {
    "id": "nightly-changelog",
    "name": "Nightly changelog",
    "recipe": "docs",
    "kickoff_markdown": (
        "# Nightly changelog entry\n\n"
        "## Files in scope\n\n"
        "- CHANGELOG.md\n"
    ),
    "schedule": "0 9 * * 1-5",
    "workspace": "worktree",
}


def _proposing_turn(home, seen_prompts, *, spec=None, ok=True, error="nope"):
    """A fake orchestrator turn: optionally calls the real ``propose``
    (as the MCP server would) and streams its tool_use + tool_result."""
    from mini_ork import automations as _auto

    payload = dict(spec or _PROPOSAL_SPEC)

    def turn(lane, prompt, cwd, home_arg, resume, on_event):
        async def _run():
            seen_prompts.append(prompt)
            if ok:
                result = _auto.propose(
                    home,
                    id=payload["id"],
                    name=payload["name"],
                    recipe=payload["recipe"],
                    kickoff=payload["kickoff_markdown"],
                    schedule=payload["schedule"],
                    workspace=payload.get("workspace", "worktree"),
                )
                tool_payload = json.dumps(result)
            else:
                tool_payload = json.dumps({"ok": False, "error": error})
            tool_input = {
                "id": payload["id"],
                "name": payload["name"],
                "recipe": payload["recipe"],
                "schedule": payload["schedule"],
                "kickoff_markdown": payload["kickoff_markdown"],
                "workspace": payload.get("workspace", "worktree"),
            }
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_p1",
                 "name": "mcp__mini-ork__propose_automation",
                 "input": tool_input}]}})
            await on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_p1",
                 "content": [{"type": "text", "text": tool_payload}]}]}})
            return _Turn()
        return _run()
    return lambda lane, prompt, cwd, home, resume, on_event: turn(
        lane, prompt, cwd, home, resume, on_event)


def test_a_proposal_renders_as_a_card_with_name_recipe_when_next_and_kickoff(tmp_path, monkeypatch):
    """Card body has the name, recipe, ``when``, cron, workspace, three next
    fires, and the first 40 lines of the kickoff in a fenced markdown block."""
    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_proposing_turn(home, prompts))
    conn = _PermConn([])  # no decision — just observe the card
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/automation new nightly changelog")]))
    # ``map_event`` surfaces the raw tool_result text first; our card
    # emits a SECOND ``ToolCallProgress`` whose content is a
    # ``ContentToolCallContent`` list. Look for the card body across all
    # updates on the proposal's tool_call id.
    cards = [
        u for _, u in conn.sent
        if isinstance(u, ToolCallProgress)
        and u.tool_call_id == "toolu_p1"
        and u.content
        and any(isinstance(c, ContentToolCallContent) for c in u.content)
    ]
    assert cards, "no proposal card body emitted"
    text_parts: list[str] = []
    for u in cards:
        for c in u.content:
            if isinstance(c, ContentToolCallContent):
                body = getattr(c, "content", None)
                if body is not None and isinstance(getattr(body, "text", None), str):
                    text_parts.append(body.text)
    text = "\n".join(text_parts)
    assert "Nightly changelog" in text
    assert "`docs`" in text
    assert "0 9 * * 1-5" in text  # the cron
    assert "in a new worktree" in text
    assert "Next:" in text
    # The kickoff is fenced and includes the section header.
    assert "```markdown" in text
    assert "Nightly changelog entry" in text
    assert "## Files in scope" in text
    # The orchestrator received the rewritten scheduling intent.
    assert prompts and "on a schedule" in prompts[0]
    assert "nightly changelog" in prompts[0]


def test_proposal_create_writes_automation_and_offers_scheduler_when_off(tmp_path, monkeypatch):
    """``create`` writes ``automations.json``, says ``Scheduled``, and offers
    the scheduler when it is off."""
    from mini_ork import automations as _auto

    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_proposing_turn(home, prompts))
    conn = _PermConn(["create", "on"])
    monkeypatch.setattr(_auto, "scheduler_status",
                        lambda _h: {"installed": False, "platform": "macos",
                                    "command": "/bin/echo", "log_path": "/tmp/log"})
    monkeypatch.setattr(_auto, "install_scheduler",
                        lambda _h: {"ok": True, "platform": "macos"})
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("schedule it")]))
    # First dialog: Create / Change / Discard on the proposal's tool_call id.
    assert conn.asked[0][0] == "toolu_p1"
    assert conn.asked[0][1] == ["Create automation", "Change something", "Discard"]
    # Second dialog: on / later, on its OWN tool_call id (the scheduler id,
    # NOT the proposal id) — reuse would overwrite the in-flight approval.
    assert conn.asked[1][0].startswith("automation-scheduler:")
    assert conn.asked[1][0] != "toolu_p1"
    assert conn.asked[1][1] == ["Turn on the scheduler", "Not now"]
    # The store now has the automation; the proposal file is gone.
    items = _auto.load(home)
    assert any(a.get("id") == "nightly-changelog" for a in items)
    assert not _auto._proposal_path(home, "nightly-changelog").exists()
    # The "Scheduled …" line was emitted.
    texts = [getattr(u.content, "text", "") for _, u in conn.sent
             if isinstance(u, AgentMessageChunk) and isinstance(u.content, TextContentBlock)]
    assert any("Scheduled Nightly changelog" in t for t in texts)
    assert any("Next run" in t for t in texts)


def test_proposal_create_then_scheduler_on_calls_install(tmp_path, monkeypatch):
    """``on`` → ``install_scheduler`` is invoked and the ``Scheduler on`` line
    is emitted."""
    from mini_ork import automations as _auto

    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_proposing_turn(home, prompts))
    conn = _PermConn(["create", "on"])
    called: dict[str, Any] = {}
    monkeypatch.setattr(_auto, "scheduler_status", lambda _h: {"installed": False})
    def fake_install(_h):
        called["n"] = called.get("n", 0) + 1
        return {"ok": True, "platform": "macos"}
    monkeypatch.setattr(_auto, "install_scheduler", fake_install)
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("schedule it")]))
    assert called.get("n") == 1
    texts = [getattr(u.content, "text", "") for _, u in conn.sent
             if isinstance(u, AgentMessageChunk) and isinstance(u.content, TextContentBlock)]
    assert any("Scheduler on" in t for t in texts)


def test_proposal_create_then_scheduler_later_message(tmp_path, monkeypatch):
    """``later`` → ``Saved, but it will not fire until the scheduler is on: …``;
    ``install_scheduler`` is NOT called."""
    from mini_ork import automations as _auto

    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_proposing_turn(home, prompts))
    conn = _PermConn(["create", "later"])
    called: dict[str, Any] = {}
    monkeypatch.setattr(_auto, "scheduler_status", lambda _h: {"installed": False})
    def fake_install(_h):
        called["n"] = called.get("n", 0) + 1
        return {"ok": True}
    monkeypatch.setattr(_auto, "install_scheduler", fake_install)
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("schedule it")]))
    assert called.get("n", 0) == 0
    texts = [getattr(u.content, "text", "") for _, u in conn.sent
             if isinstance(u, AgentMessageChunk) and isinstance(u.content, TextContentBlock)]
    assert any("will not fire" in t for t in texts)
    assert any("/automation scheduler on" in t for t in texts)


def test_proposal_create_skips_scheduler_when_already_on(tmp_path, monkeypatch):
    """Scheduler already installed → only one ``request_permission`` (create)."""
    from mini_ork import automations as _auto

    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_proposing_turn(home, prompts))
    conn = _PermConn(["create"])  # only one answer is needed
    monkeypatch.setattr(_auto, "scheduler_status", lambda _h: {"installed": True})
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("schedule it")]))
    assert len(conn.asked) == 1
    assert conn.asked[0][0] == "toolu_p1"


def test_proposal_change_keeps_it(tmp_path, monkeypatch):
    """``change`` → ``Tell me what to change.`` + proposal file still present."""
    from mini_ork import automations as _auto

    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_proposing_turn(home, prompts))
    conn = _PermConn(["change"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("schedule it")]))
    assert _auto._proposal_path(home, "nightly-changelog").is_file()
    texts = [getattr(u.content, "text", "") for _, u in conn.sent
             if isinstance(u, AgentMessageChunk) and isinstance(u.content, TextContentBlock)]
    assert any("Tell me what to change" in t for t in texts)


def test_proposal_discard_deletes_it(tmp_path, monkeypatch):
    """``discard`` → ``Proposal discarded.`` + proposal file is gone."""
    from mini_ork import automations as _auto

    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_proposing_turn(home, prompts))
    conn = _PermConn(["discard"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("schedule it")]))
    assert not _auto._proposal_path(home, "nightly-changelog").exists()
    texts = [getattr(u.content, "text", "") for _, u in conn.sent
             if isinstance(u, AgentMessageChunk) and isinstance(u.content, TextContentBlock)]
    assert any("Proposal discarded" in t for t in texts)


def test_dismissing_the_proposal_dialog_keeps_it(tmp_path, monkeypatch):
    """Dismissed → ``Proposal kept — ask me to create it when you're ready.``;
    proposal file still present."""
    from mini_ork import automations as _auto

    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_proposing_turn(home, prompts))
    conn = _PermConn([None])  # dismissed
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("schedule it")]))
    assert _auto._proposal_path(home, "nightly-changelog").is_file()
    texts = [getattr(u.content, "text", "") for _, u in conn.sent
             if isinstance(u, AgentMessageChunk) and isinstance(u.content, TextContentBlock)]
    assert any("Proposal kept" in t for t in texts)


def test_proposal_ok_false_offers_no_questions(tmp_path, monkeypatch):
    """``ok=false`` → ``turn_proposal`` stays empty → no approval dialog."""
    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_proposing_turn(home, prompts, ok=False))
    conn = _PermConn([])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("schedule it")]))
    assert conn.asked == []


def test_proposal_for_existing_id_says_update_automation(tmp_path, monkeypatch):
    """When the proposal id already exists, the verb is ``Update`` (not Create)."""
    from mini_ork import automations as _auto

    proj, home = _authoring_project(tmp_path, monkeypatch)
    _auto.add(home, id="nightly-changelog", name="Existing",
              recipe="docs", kickoff="# k", schedule="0 9 * * 1-5")
    prompts: list[str] = []
    agent, thread = _thread_agent(proj, orchestrator_turn=_proposing_turn(home, prompts))
    conn = _PermConn([])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("schedule it")]))
    assert conn.asked[0][0] == "toolu_p1"
    assert conn.asked[0][1] == ["Update automation", "Change something", "Discard"]


def test_recipe_draft_is_offered_before_the_proposal_in_the_same_turn(tmp_path, monkeypatch):
    """Both ``draft_recipe`` and ``propose_automation`` fire in one turn → the
    recipe draft is offered first (the automation may run it), then the
    proposal; both can be created in the same turn."""
    from mini_ork import automations as _auto
    from mini_ork import recipe_author

    proj, home = _authoring_project(tmp_path, monkeypatch)
    # Commit a project recipe so ``propose`` can target it.
    recipe_author.draft(home, _AUDIT_SPEC)
    recipe_author.commit_draft(home, "migration-audit")
    seen_prompts: list[str] = []

    def combined_turn(lane, prompt, cwd, home_arg, resume, on_event):
        async def _run():
            seen_prompts.append(prompt)
            draft_result = recipe_author.draft(home, _AUDIT_SPEC)
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_d1",
                 "name": "mcp__mini-ork__draft_recipe",
                 "input": {"spec": _AUDIT_SPEC}}]}})
            await on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_d1",
                 "content": [{"type": "text", "text": json.dumps(draft_result)}]}]}})
            proposal_result = _auto.propose(
                home, id="nightly-changelog", name="Nightly",
                recipe="docs", kickoff="# k", schedule="0 9 * * 1-5",
            )
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_p1",
                 "name": "mcp__mini-ork__propose_automation",
                 "input": {"id": "nightly-changelog", "name": "Nightly",
                           "recipe": "docs", "schedule": "0 9 * * 1-5",
                           "kickoff_markdown": "# k",
                           "workspace": "worktree"}}]}})
            await on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_p1",
                 "content": [{"type": "text", "text": json.dumps(proposal_result)}]}]}})
            return _Turn()
        return _run()
    turn_fn = lambda lane, prompt, cwd, home, resume, on_event: combined_turn(
        lane, prompt, cwd, home, resume, on_event)
    monkeypatch.setattr(_auto, "scheduler_status", lambda _h: {"installed": True})
    agent, thread = _thread_agent(proj, orchestrator_turn=turn_fn)
    # draft: create → "test it?": later → proposal: create (scheduler already on)
    conn = _PermConn(["create", "later", "create"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("draft and schedule")]))
    asked_ids = [tid for tid, _ in conn.asked]
    assert asked_ids[0] == "toolu_d1"
    assert ("Create recipe" in conn.asked[0][1] or "Update recipe" in conn.asked[0][1])
    assert asked_ids[-1] == "toolu_p1"  # after the recipe's questions
    assert "automation-scheduler:" not in str(asked_ids)
    assert [a["id"] for a in _auto.load(home)] == ["nightly-changelog"]
    assert not _auto._proposal_path(home, "nightly-changelog").is_file()


def test_automation_new_reaches_the_orchestrator_with_the_bridge(tmp_path, monkeypatch):
    """``/automation new nightly changelog`` rewrites to the orchestrator
    with the scheduling intent AND the dispatcher emits the automation
    bridge line."""
    proj, home = _authoring_project(tmp_path, monkeypatch)
    prompts: list[str] = []

    def recorder(lane, prompt, cwd, home_arg, resume, on_event):
        async def _run():
            prompts.append(prompt)
            return _Turn()
        return _run()
    turn_fn = lambda lane, prompt, cwd, home, resume, on_event: recorder(
        lane, prompt, cwd, home, resume, on_event)
    agent, thread = _thread_agent(proj, orchestrator_turn=turn_fn)
    conn = _PermConn([])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/automation new nightly changelog")]))
    assert prompts and "on a schedule" in prompts[0]
    assert "nightly changelog" in prompts[0]
    # The automation bridge line is the agent_message_chunk emitted BEFORE
    # the orchestrator runs.
    chunks = [u for _, u in conn.sent if isinstance(u, AgentMessageChunk)]
    assert any("When it has a proposal" in getattr(c.content, "text", "") for c in chunks)
    assert any("buttons to create it" in getattr(c.content, "text", "") for c in chunks)


# ── /race (Zed S7a) ──────────────────────────────────────────────────────────


class _RaceProbeWorkspace:
    """Minimal stand-in for ``workspaces.Workspace`` for race tests.

    Race flow reads ``ws.branch`` (marker suffix) and calls
    ``workspaces.status(ws)`` (per-row change counts) and
    ``workspaces.merge``/``workspaces.discard`` (decisions). Tests that
    exercise a partial surface only set the attributes they read.
    """


class _RaceWorkspaces:
    """In-memory ``mini_ork.workspaces`` shim for race-turn tests. Records calls."""

    def __init__(self, *, git_ok: bool = True) -> None:
        self.git_ok = git_ok
        self.created: list[str] = []
        self.discarded: list[str] = []
        self.merged: list[tuple[str, str]] = []
        self.status_calls: list[str] = []
        self.loaded: dict[str, _RaceProbeWorkspace] = {}

    def create(self, cwd, home, rid):  # noqa: ARG002 — race probe signature
        if not self.git_ok:
            raise RuntimeError("not a git repository")
        self.created.append(rid)
        ws = _RaceProbeWorkspace()
        ws.branch = f"wt/{rid}"
        ws.run_id = rid
        ws.path = Path(home) / "runs" / rid
        ws.added = 5
        ws.removed = 1
        self.loaded[rid] = ws
        return ws

    def load(self, home, rid):  # noqa: ARG002
        return self.loaded.get(rid)

    def discard(self, ws):
        self.discarded.append(ws.run_id)

    def status(self, ws):
        self.status_calls.append(ws.run_id)
        return {"added": getattr(ws, "added", 0), "removed": getattr(ws, "removed", 0)}

    def merge(self, ws, message=""):
        self.merged.append((ws.run_id, message))


def _race_home(tmp_path, *, git_ok: bool = True, monkeypatch=None) -> tuple[Path, _RaceWorkspaces]:
    """Build a project + a thread agent + a function-level shim of
    ``mini_ork.workspaces`` patched onto the real module.

    ``git_ok=True`` makes ``workspaces.create`` succeed and remember the
    run id; tests that want to verify a non-git refusal pass
    ``git_ok=False``. ``monkeypatch`` is the pytest fixture; when
    supplied the shim is installed via ``monkeypatch.setattr`` so the
    autouse teardown restores the real ``create``/``load``/etc. without
    leaking into the next test.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    shim = _RaceWorkspaces(git_ok=git_ok)

    # Seed a providers.yaml so ``known_lanes`` lists sonnet / glm / minimax
    # and ``parse_race_arg`` falls through to ``DEFAULT_LANES`` rather than
    # returning the "needs at least two lanes" error.
    home = proj / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "providers.yaml").write_text(
        "providers:\n"
        "  sonnet: {kind: anthropic-native}\n"
        "  glm: {kind: openai-compat}\n"
        "  minimax: {kind: anthropic-native}\n"
        "  codex: {kind: openai-chat}\n",
        encoding="utf-8",
    )

    from mini_ork import workspaces as _real_ws
    if monkeypatch is not None:
        for attr in ("create", "load", "discard", "status", "merge"):
            monkeypatch.setattr(_real_ws, attr, getattr(shim, attr))
        monkeypatch.setattr(_real_ws, "Workspace", _RaceProbeWorkspace)
        monkeypatch.setattr(_real_ws, "_is_git_repo", lambda _p: git_ok)
    else:
        # No fixture: caller is responsible for restoring. Used by the
        # standalone /tmp/race_debug.py scripts.
        import mini_ork.workspaces as _ws_mod
        for attr in ("create", "load", "discard", "status", "merge"):
            setattr(_ws_mod, attr, getattr(shim, attr))
        _ws_mod.Workspace = _RaceProbeWorkspace
        _ws_mod._is_git_repo = lambda _p: git_ok

    return proj, shim


def test_race_in_run_session_returns_refusal(tmp_path, monkeypatch):
    """``/race`` is thread-only; a run-id session returns the kickoff's exact refusal."""
    proj, _ = _race_home(tmp_path, monkeypatch=monkeypatch)
    agent, thread = _thread_agent(proj)
    conn = _CapturingConn()
    agent.on_connect(conn)
    # Mint a run session id directly so the prompt path treats it as a run, not a thread.
    rid = "run-not-thread"
    agent._sessions[rid] = str(proj)
    agent._recipes[rid] = "code-fix"
    resp = asyncio.run(agent.prompt(rid, [_text_block("/race fix the bug")]))
    assert resp.stop_reason == "end_turn"
    chunks = [u for u in conn.captured if isinstance(u, AgentMessageChunk)]
    assert any("Races start in a mini-ork thread" in getattr(c.content, "text", "")
               for c in chunks)


def test_race_unknown_lane_renders_parser_error(tmp_path, monkeypatch):
    """``/race bogus,sonnet x`` → race_parser error string rendered verbatim."""
    proj, _ = _race_home(tmp_path, monkeypatch=monkeypatch)
    agent, thread = _thread_agent(proj)
    conn = _CapturingConn()
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/race bogus,sonnet fix x")]))
    chunks = [u for u in conn.captured if isinstance(u, AgentMessageChunk)]
    assert any("Unknown lane" in getattr(c.content, "text", "") for c in chunks)


def test_race_non_git_project_returns_kickoff_refusal(tmp_path, monkeypatch):
    """A non-git project triggers the race's probe branch with the kickoff's exact wording."""
    proj, _ = _race_home(tmp_path, git_ok=False, monkeypatch=monkeypatch)
    agent, thread = _thread_agent(proj)
    conn = _CapturingConn()
    agent.on_connect(conn)
    resp = asyncio.run(agent.prompt(thread, [_text_block("/race fix x")]))
    assert resp.stop_reason == "end_turn"
    chunks = [u for u in conn.captured if isinstance(u, AgentMessageChunk)]
    assert any("Racing needs a git project" in getattr(c.content, "text", "")
               for c in chunks)


def test_race_default_lanes_fan_out_three_markers_and_seeds(tmp_path, monkeypatch):
    """``/race <task>`` (no explicit lanes) fans out to DEFAULT_LANES and:

    * opens one ``ToolCallStart`` per lane with the ``race i/N · <lane>`` title;
    * calls ``seed_run_config`` once per lane with the matching lane;
    * leaves the worktrees intact when no candidate keeps it.
    """
    import mini_ork.acp.race as race_mod

    proj, shim = _race_home(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setenv("MINI_ORK_HOME", str(proj / ".mini-ork"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.delenv("MO_RACE_LANES", raising=False)

    seeded: list[tuple[str, str]] = []  # (recipe, lane)
    real_seed = race_mod.seed_run_config

    def spy_seed(home, rid, recipe, lane):
        seeded.append((recipe, lane))
        return real_seed(home, rid, recipe, lane)

    # Injected launcher — bypasses ``_launch`` so the per-run env overlay
    # is not consulted (we cover that contract in the skip_review test).
    def fake_launcher(rid, text):
        return {"ok": True, "run_id": rid}

    with patch.object(race_mod, "seed_run_config", spy_seed):
        agent, thread = _thread_agent(proj, launcher=fake_launcher,
                                      reader=lambda _r: {
                                          "status": "published",
                                          "events": [], "llm_calls": []})
        conn = _CapturingConn()
        agent.on_connect(conn)
        asyncio.run(agent.prompt(thread, [_text_block("/race fix the bug")]))

    # 1. Three race markers, in declared order. The race emits the initial
    #    ``ToolCallStart`` (race i/N · <lane> — …) before the workspace
    #    create, then re-emits the same ``tool_call_id`` with the worktree
    #    suffix once the workspace is open. We dedup by id and use the
    #    FIRST emission (the canonical title per the kickoff).
    starts = [u for u in conn.captured if isinstance(u, ToolCallStart)]
    by_id: dict[str, ToolCallStart] = {}
    for s in starts:
        by_id.setdefault(s.tool_call_id, s)
    race_titles = [s.title for s in by_id.values() if s.title.startswith("race ")]
    assert len(race_titles) == 3
    assert "sonnet" in race_titles[0]
    assert "glm" in race_titles[1]
    assert "minimax" in race_titles[2]
    assert "1/3" in race_titles[0] and "2/3" in race_titles[1] and "3/3" in race_titles[2]

    # 2. seed_run_config called once per lane with the matching lane.
    assert [lane for _recipe, lane in seeded] == ["sonnet", "glm", "minimax"]

    # 3. One worktree per lane. With no connection to ask, the decision is
    #    "later": nothing merged, nothing discarded.
    assert len(shim.created) == 3
    assert shim.merged == []
    assert shim.discarded == []


def test_race_run_env_overlay_is_set_before_launch(tmp_path, monkeypatch):
    """``_run_env[rid]`` carries ``MO_ROUTING_POLICY=workflow_default`` so
    ``_launch`` can pass it to the subprocess env.

    The launcher is the only place that can observe the env overlay, so
    capture it at launch time (race cleanup clears the entry once the
    parent permission card resolves).
    """
    proj, _shim = _race_home(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setenv("MINI_ORK_HOME", str(proj / ".mini-ork"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.delenv("MO_RACE_LANES", raising=False)

    captured_env: list[dict | None] = []

    def spy_launch(self, rid, text):
        # Mirror ``_launch``'s body: merge ``_run_env`` overlay into the
        # extra_env that would go to the subprocess.
        cwd = self._sessions.get(rid)
        extra_env: dict[str, str] = {}
        if cwd:
            extra_env["MO_TARGET_CWD"] = cwd
        overlay = self._run_env.get(rid) or {}
        if overlay:
            extra_env.update(overlay)
        captured_env.append(extra_env or None)
        return {"ok": True, "run_id": rid}

    monkeypatch.setattr(MiniOrkAcpAgent, "_launch", spy_launch)

    agent, thread = _thread_agent(proj, reader=lambda _r: {
        "status": "published", "events": [], "llm_calls": []})
    asyncio.run(agent.prompt(thread, [_text_block("/race sonnet,glm fix x")]))

    assert len(captured_env) == 2
    for env in captured_env:
        assert env is not None
        assert env["MO_ROUTING_POLICY"] == "workflow_default"


def test_race_runs_are_added_to_skip_review_during_launch(tmp_path, monkeypatch):
    """Race runs land in ``_skip_review`` during the launch, so no per-run
    S5 button fires; cleanup clears them at the end of the turn.

    The kickoff pins this so the parent permission card owns the keep /
    later / discard decision — the per-run "ready to review" message
    would re-ask the same question.
    """
    import mini_ork.acp.race as race_mod

    proj, _shim = _race_home(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setenv("MINI_ORK_HOME", str(proj / ".mini-ork"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.delenv("MO_RACE_LANES", raising=False)
    monkeypatch.setattr(race_mod, "seed_run_config",
                        lambda *a, **k: Path("/dev/null"))

    captured_during_launch: list[tuple[str, bool]] = []

    def fake_launcher(rid, text):
        # Mirror the agent instance via ``agent._skip_review`` — the
        # closure captures ``agent`` below.
        captured_during_launch.append((rid, rid in agent._skip_review))
        return {"ok": True, "run_id": rid}

    agent, thread = _thread_agent(
        proj,
        launcher=fake_launcher,
        reader=lambda _r: {"status": "published", "events": [], "llm_calls": []},
    )
    asyncio.run(agent.prompt(thread, [_text_block("/race sonnet,glm fix x")]))

    assert len(captured_during_launch) == 2
    # Both runs were in _skip_review AT LAUNCH TIME; the per-run S5 path
    # would skip the "ready to review" message for them.
    assert all(present for _rid, present in captured_during_launch)


# ── Zed S7a — race decisions on a real git project ───────────────────────────


def _race_git_project(tmp_path, monkeypatch):
    """A real temp git repo whose home lists sonnet / glm / minimax."""
    import subprocess

    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    for cmd in (["git", "init", "-b", "main"], ["git", "config", "user.name", "T"],
                ["git", "config", "user.email", "t@example.com"]):
        subprocess.run(cmd, cwd=proj, check=True, capture_output=True)
    (proj / "README.md").write_text("hi\n", encoding="utf-8")
    (proj / ".git" / "info" / "exclude").write_text(".mini-ork/\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=proj, check=True, capture_output=True)
    home = proj / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "providers.yaml").write_text(
        "providers:\n  sonnet: {kind: anthropic-native}\n  glm: {kind: openai-compat}\n"
        "  minimax: {kind: anthropic-native}\n", encoding="utf-8")
    (home / "config" / "agents.yaml").write_text("lanes:\n  worker: sonnet\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MO_RACE_LANES", raising=False)
    return proj, home


class _PickConn(_SidConn):
    """Answers the race question with the first option whose id starts with
    ``prefix`` (``None`` → dismissed); records every question."""

    def __init__(self, prefix: str | None) -> None:
        super().__init__()
        self.prefix = prefix
        self.asked: list[tuple[str, list[str], list[str]]] = []

    async def request_permission(self, session_id, tool_call, options, **kw):
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        ids = [o.option_id for o in options]
        self.asked.append((tool_call.tool_call_id, ids, [o.name for o in options]))
        pick = next((i for i in ids if self.prefix and i.startswith(self.prefix)), None)
        outcome = (DeniedOutcome(outcome="cancelled") if pick is None
                   else AllowedOutcome(outcome="selected", option_id=pick))
        return RequestPermissionResponse(outcome=outcome)


def _race_agent(proj, *, outcomes: list[str], fail_launch: set[int] = frozenset()):
    """A thread agent whose launcher writes ``change.txt`` (= the run id) into
    each worktree and whose reader ends the i-th launched run with
    ``outcomes[i]``. Launch ``i`` in ``fail_launch`` fails."""
    state: dict[str, Any] = {"order": [], "agent": None}

    def launcher(rid, _kickoff):
        i = len(state["order"])
        state["order"].append(rid)
        if i in fail_launch:
            return {"ok": False, "error": "lane key missing"}
        wt = Path(state["agent"]._sessions[rid])
        (wt / "change.txt").write_text(rid + "\n", encoding="utf-8")
        return {"ok": True, "run_id": rid}

    def reader(rid):
        i = state["order"].index(rid)
        return {"status": outcomes[i], "events": [], "llm_calls": []}

    agent, thread = _thread_agent(proj, launcher=launcher, reader=reader)
    state["agent"] = agent
    return agent, thread, state


def _race_messages(conn) -> str:
    return "\n".join(
        getattr(getattr(u, "content", None), "text", "") or ""
        for _sid, u in conn.sent
    )


def test_race_keep_merges_one_change_and_discards_every_other_worktree(tmp_path, monkeypatch):
    from mini_ork import workspaces as ws_mod

    proj, home = _race_git_project(tmp_path, monkeypatch)
    agent, thread, state = _race_agent(proj, outcomes=["published", "published", "failed"])
    conn = _PickConn("keep:")
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/race add a changelog entry")]))
    (tool_id, ids, names) = conn.asked[-1]
    assert tool_id.startswith("race:")
    assert [i for i in ids if i.startswith("keep:")] == [f"keep:{r}" for r in state["order"][:2]]
    assert ids[-2:] == ["later", "discard_all"]
    assert names[0].startswith("Keep sonnet (+1 −0")
    # The first verified change is on main; every other worktree — the other
    # verified one AND the failed one — is gone.
    assert (proj / "change.txt").read_text() == state["order"][0] + "\n"
    assert ws_mod.list_open(home) == []
    text = _race_messages(conn)
    assert "| ✓ | sonnet | verified |" in text and "| ✗ | minimax | failed |" in text
    assert "Merged sonnet's change into main" in text and "Discarded the other 2." in text


def test_race_later_keeps_all_and_discard_all_removes_them(tmp_path, monkeypatch):
    from mini_ork import workspaces as ws_mod

    proj, home = _race_git_project(tmp_path, monkeypatch)
    agent, thread, _ = _race_agent(proj, outcomes=["published", "published", "failed"])
    agent.on_connect(_PickConn("later"))
    asyncio.run(agent.prompt(thread, [_text_block("/race add a changelog entry")]))
    assert len(ws_mod.list_open(home)) == 3
    assert not (proj / "change.txt").exists()

    proj2, home2 = _race_git_project(tmp_path / "b", monkeypatch)
    agent2, thread2, _ = _race_agent(proj2, outcomes=["published", "published", "failed"])
    agent2.on_connect(_PickConn("discard_all"))
    asyncio.run(agent2.prompt(thread2, [_text_block("/race add a changelog entry")]))
    assert ws_mod.list_open(home2) == []


def test_race_without_a_verified_change_asks_nothing_and_keeps_worktrees(tmp_path, monkeypatch):
    from mini_ork import workspaces as ws_mod

    proj, home = _race_git_project(tmp_path, monkeypatch)
    agent, thread, _ = _race_agent(proj, outcomes=["failed", "failed", "failed"])
    conn = _PickConn("keep:")
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/race add a changelog entry")]))
    assert conn.asked == []
    assert "No model produced a verified change" in _race_messages(conn)
    assert len(ws_mod.list_open(home)) == 3


def test_race_goes_on_when_one_lane_fails_to_launch(tmp_path, monkeypatch):
    from mini_ork import workspaces as ws_mod

    proj, home = _race_git_project(tmp_path, monkeypatch)
    agent, thread, state = _race_agent(proj, outcomes=["published", "published", "published"],
                                       fail_launch={1})
    conn = _PickConn("later")
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/race add a changelog entry")]))
    keep_ids = [i for i in conn.asked[-1][1] if i.startswith("keep:")]
    assert keep_ids == [f"keep:{state['order'][0]}", f"keep:{state['order'][2]}"]
    assert {w.run_id for w in ws_mod.list_open(home)} == {state["order"][0], state["order"][2]}
    assert "launch failed: lane key missing" in _race_messages(conn)


def test_cancelling_a_race_stops_every_contestant(tmp_path, monkeypatch):
    proj, _home = _race_git_project(tmp_path, monkeypatch)
    stopped: list[str] = []
    state: dict[str, Any] = {"order": [], "agent": None}

    def launcher(rid, _kickoff):
        state["order"].append(rid)
        return {"ok": True, "run_id": rid}

    agent, thread = _thread_agent(
        proj, launcher=launcher,
        reader=lambda _rid: {"status": "executing", "events": [], "llm_calls": []},
        stopper=lambda rid: stopped.append(rid) or {"ok": True},
        killer=lambda rid: {"ok": True})
    agent.on_connect(_PickConn(None))

    async def scenario():
        turn = asyncio.create_task(agent.prompt(thread, [_text_block("/race add an entry")]))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(state["order"]) == 3:
                break
        await agent.cancel(thread)
        return await asyncio.wait_for(turn, timeout=5)

    resp = asyncio.run(scenario())
    assert resp.stop_reason == "cancelled"
    assert sorted(stopped) == sorted(state["order"])


# ── /kickoff + draft_kickoff (Zed S7b) ──────────────────────────────────────


def _kickoff_project(tmp_path, monkeypatch, *, project_files=("present.py",)):
    """A tmp project with one real file under project/ + a stub docs recipe.

    Mirrors :func:`_authoring_project` so the same `_thread_agent` factory
    works. The kickoff lint checks scope paths against ``project`` =
    ``home.parent``, so a real ``present.py`` under the project lets the
    happy-path test reference it without a "not in the project" finding.
    """
    proj = tmp_path / "proj"
    home = proj / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text(
        "lanes:\n  worker: sonnet\n  reviewer: opus\n",
        encoding="utf-8",
    )
    for name in project_files:
        (proj / name).write_text("x", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
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


def _kickoff_turn(home, prompts, kickoff_markdown, *, recipe="docs"):
    """A fake orchestrator turn that calls the real ``draft_kickoff`` MCP
    helper (mirrors :func:`_drafting_turn`)."""
    from mini_ork.mcp_context.server import dispatch

    def turn(lane, prompt, cwd, home_arg, resume, on_event):
        async def _run():
            prompts.append(prompt)
            resp = dispatch({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "draft_kickoff", "arguments": {
                    "recipe": recipe, "kickoff_markdown": kickoff_markdown,
                }},
            }, control=True)
            # The MCP dispatcher wraps the handler result inside an envelope
            # (``{"content": [{"type": "text", "text": <json>}]}``) — the
            # agent's ``_kickoff_result`` parses the inner text, so surface
            # the unwrapped handler JSON to the fake tool_result.
            inner = resp["result"]["content"][0]["text"]
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_k1",
                 "name": "mcp__mini-ork__draft_kickoff",
                 "input": {"recipe": recipe,
                           "kickoff_markdown": kickoff_markdown}}]}})
            await on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_k1",
                 "content": [{"type": "text", "text": inner}]}]}})
            return _Turn()
        return _run()
    return lambda lane, prompt, cwd, home, resume, on_event: turn(
        lane, prompt, cwd, home, resume, on_event)


def test_kickoff_draft_shows_as_new_file_diff_with_findings(tmp_path, monkeypatch):
    """``draft_kickoff`` in a thread turn surfaces as a new-file diff with
    findings formatted ``⚠ msg — fix`` (warn) and ``✗ msg — fix`` (error).
    A complete kickoff → "Looks complete." text."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    prompts: list[str] = []
    body = (
        "# Title\n\n"
        "## Files in scope\n"
        "- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    agent, thread = _thread_agent(proj, orchestrator_turn=_kickoff_turn(home, prompts, body))
    conn = _PermConn([])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/kickoff fix the login redirect")]))
    updates = [u for _, u in conn.sent]
    # The preview arrives as a ToolCallProgress on toolu_k1.
    preview = [u for u in updates if isinstance(u, ToolCallProgress)
               and u.tool_call_id == "toolu_k1" and u.content]
    diffs = [c for u in preview for c in u.content if isinstance(c, FileEditToolCallContent)]
    assert len(diffs) == 1
    assert diffs[0].old_text is None
    assert diffs[0].new_text == body
    assert diffs[0].path.endswith(".mini-ork/kickoffs/title.md")
    text_blocks = [c.content for u in preview for c in u.content
                   if isinstance(c, ContentToolCallContent)]
    assert any("Looks complete." in b.text for b in text_blocks)
    # The orchestrator turn was kicked off with the rewritten intent.
    assert prompts and "fix the login redirect" in prompts[0]
    # The bridge line surfaces in the AgentMessageChunk stream.
    chunks = [u for _, u in conn.sent if isinstance(u, AgentMessageChunk)]
    assert any("When the kickoff is ready" in getattr(c.content, "text", "")
               for c in chunks)


def test_kickoff_run_launches_and_follows(tmp_path, monkeypatch):
    """``run`` → the kickoff's staged file is deleted, then the recipe runs
    via ``_prompt_thread_direct`` and is followed in-thread."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    body = (
        "# Title\n\n## Files in scope\n- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    prompts: list[str] = []

    def turn(lane, prompt, cwd, home_arg, resume, on_event):
        async def _run():
            prompts.append(prompt)
            from mini_ork.mcp_context.server import dispatch
            resp = dispatch({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "draft_kickoff", "arguments": {
                    "recipe": "docs", "kickoff_markdown": body,
                }},
            }, control=True)
            # MCP wraps the handler result inside an envelope — surface the
            # inner JSON string (matches ``_kickoff_result``'s parser).
            content = resp["result"]["content"][0]["text"]
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_k1",
                 "name": "mcp__mini-ork__draft_kickoff",
                 "input": {"recipe": "docs",
                           "kickoff_markdown": body}}]}})
            await on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_k1",
                 "content": [{"type": "text", "text": content}]}]}})
            return _Turn()
        return _run()
    turn_fn = lambda lane, prompt, cwd, home, resume, on_event: turn(
        lane, prompt, cwd, home, resume, on_event)

    # Mark a child run that the run-launched-follow flow tracks.
    def launcher(session_id, prompt_text):
        # _prompt_thread_direct forwards the kickoff markdown into the
        # launcher; the run id == session id here.
        assert prompt_text == body
        # The staged file is gone (the handler deletes it on ``run``).
        assert not (home / "kickoff-drafts" / "title.md").exists()
        return {"ok": True, "pid": 1, "log_path": "/tmp/x.log"}

    def reader(rid):
        return {
            "status": "published",
            "events": [],
            "llm_calls": [],
        }

    agent, thread = _thread_agent(
        proj, orchestrator_turn=turn_fn, launcher=launcher, reader=reader,
    )
    conn = _PermConn(["run"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/kickoff fix the login redirect")]))
    # _PermConn recorded one permission ask on the kickoff tool call.
    assert any(tid == "toolu_k1" for tid, _ in conn.asked)


def test_kickoff_save_moves_draft_to_kickoffs_dir(tmp_path, monkeypatch):
    """``save`` → move the staged draft to ``<home>/kickoffs/<slug>.md``
    with collision suffix, surface the canonical line + file link."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    body = (
        "# Title\n\n## Files in scope\n- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    prompts: list[str] = []
    agent, thread = _thread_agent(
        proj, orchestrator_turn=_kickoff_turn(home, prompts, body),
    )
    conn = _PermConn(["save"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/kickoff")]))
    saved = home / "kickoffs" / "title.md"
    assert saved.is_file()
    assert saved.read_text(encoding="utf-8") == body
    # The staged draft was deleted on save.
    assert not (home / "kickoff-drafts" / "title.md").exists()
    # The user-facing message + file link landed in the stream.
    chunks_text = " ".join(
        getattr(c.content, "text", "") for _, c in conn.sent
        if isinstance(c, AgentMessageChunk)
    )
    assert "Saved `.mini-ork/kickoffs/title.md`" in chunks_text
    assert "/run" in chunks_text


def test_kickoff_save_uses_dash_two_on_collision(tmp_path, monkeypatch):
    """When ``<home>/kickoffs/<slug>.md`` already exists, ``save`` picks
    ``<slug>-2.md``. The user-facing message names the actual file."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    # Pre-create a kickoff with the same slug.
    (home / "kickoffs").mkdir(parents=True, exist_ok=True)
    (home / "kickoffs" / "title.md").write_text("old", encoding="utf-8")
    body = (
        "# Title\n\n## Files in scope\n- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    prompts: list[str] = []
    agent, thread = _thread_agent(
        proj, orchestrator_turn=_kickoff_turn(home, prompts, body),
    )
    conn = _PermConn(["save"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/kickoff")]))
    # The pre-existing file is untouched; the new save gets -2.
    assert (home / "kickoffs" / "title.md").read_text(encoding="utf-8") == "old"
    assert (home / "kickoffs" / "title-2.md").read_text(encoding="utf-8") == body


def test_kickoff_change_keeps_draft(tmp_path, monkeypatch):
    """``change`` → the staged draft survives; the user gets
    "Tell me what to change." and the tool call is marked failed."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    body = (
        "# Title\n\n## Files in scope\n- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    prompts: list[str] = []
    agent, thread = _thread_agent(
        proj, orchestrator_turn=_kickoff_turn(home, prompts, body),
    )
    conn = _PermConn(["change"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/kickoff")]))
    staged = home / "kickoff-drafts" / "title.md"
    assert staged.is_file()
    chunks_text = " ".join(
        getattr(c.content, "text", "") for _, c in conn.sent
        if isinstance(c, AgentMessageChunk)
    )
    assert "Tell me what to change" in chunks_text
    failed = [u for _, u in conn.sent
              if isinstance(u, ToolCallProgress)
              and u.tool_call_id == "toolu_k1"
              and u.status == "failed"]
    assert failed, "change path must mark the tool call failed"


def test_kickoff_discard_deletes_draft(tmp_path, monkeypatch):
    """``discard`` → the staged draft is deleted; the user gets
    "Kickoff discarded." and the tool call is marked failed."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    body = (
        "# Title\n\n## Files in scope\n- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    prompts: list[str] = []
    agent, thread = _thread_agent(
        proj, orchestrator_turn=_kickoff_turn(home, prompts, body),
    )
    conn = _PermConn(["discard"])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/kickoff")]))
    staged = home / "kickoff-drafts" / "title.md"
    assert not staged.exists()
    chunks_text = " ".join(
        getattr(c.content, "text", "") for _, c in conn.sent
        if isinstance(c, AgentMessageChunk)
    )
    assert "Kickoff discarded" in chunks_text


def test_kickoff_dismissed_keeps_draft(tmp_path, monkeypatch):
    """A dismissed permission dialog keeps the draft."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    body = (
        "# Title\n\n## Files in scope\n- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    prompts: list[str] = []
    agent, thread = _thread_agent(
        proj, orchestrator_turn=_kickoff_turn(home, prompts, body),
    )
    conn = _PermConn([])  # empty → dismissed
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/kickoff")]))
    staged = home / "kickoff-drafts" / "title.md"
    assert staged.is_file()
    chunks_text = " ".join(
        getattr(c.content, "text", "") for _, c in conn.sent
        if isinstance(c, AgentMessageChunk)
    )
    assert "ask me to start it when you're ready" in chunks_text


def test_kickoff_ok_false_offers_no_questions(tmp_path, monkeypatch):
    """``ok: false`` (e.g. unknown recipe) → no permission ask; the orchestrator
    gets the lint findings back from the MCP tool."""
    proj, home = _kickoff_project(tmp_path, monkeypatch)
    prompts: list[str] = []

    def turn(lane, prompt, cwd, home_arg, resume, on_event):
        async def _run():
            prompts.append(prompt)
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_k1",
                 "name": "mcp__mini-ork__draft_kickoff",
                 "input": {"recipe": "no-such-recipe",
                           "kickoff_markdown": "# x\n"}}]}})
            await on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_k1",
                 "content": [{"type": "text",
                              "text": json.dumps({"ok": False,
                                                   "error": "unknown recipe"})}]}]}})
            return _Turn()
        return _run()
    turn_fn = lambda lane, prompt, cwd, home, resume, on_event: turn(
        lane, prompt, cwd, home, resume, on_event)
    agent, thread = _thread_agent(proj, orchestrator_turn=turn_fn)
    conn = _PermConn([])
    agent.on_connect(conn)
    asyncio.run(agent.prompt(thread, [_text_block("/kickoff")]))
    # No permission ask landed on toolu_k1.
    assert not any(tid == "toolu_k1" for tid, _ in conn.asked)
