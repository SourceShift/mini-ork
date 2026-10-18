"""Unit tests for the new failure-recovery card (`_offer_failure_recovery`).

Hermetic: the connection, snapshot reader, retry-hint loader and ``/recover``
handler are all stubbed, so nothing spawns a process or touches a real DB.
Covers the offer's options/card, the active-turn gate, every decision, the
per-incarnation dedupe, replay reset, and the never-raise contract.
"""
from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from acp.schema import AgentMessageChunk  # noqa: E402

from mini_ork.acp import commands as commands_module  # noqa: E402
from mini_ork.acp.agent import (  # noqa: E402
    _FAILURE_RECOVERY_DECLINED,
    _FAILURE_RECOVERY_NONE,
    _FAILURE_RECOVERY_OFFERED,
    MiniOrkAcpAgent,
)
from mini_ork.recovery import retry_hint  # noqa: E402

RUN = "run-recovery-1"
THREAD = "th-recovery-1"

_SNAPSHOT = {
    "status": "failed",
    "events": [
        {
            "event_type": "node_end",
            "payload_json": {
                "node_id": "implementer-1",
                "node_type": "implementer",
                "verdict": "refused",
            },
        }
    ],
    "llm_calls": [],
}

_HINT = {
    "version": 1,
    "run_id": RUN,
    "failed_node": "implementer-1",
    "retryable": True,
    "stderr": "STDERR_MARKER_BOOM: exploded",
    "needs_change": {"kind": "code", "summary": "check failed"},
    "command": f"mini-ork recover {RUN}",
}


class _Conn:
    def __init__(self, option_id=None):
        self.option_id = option_id
        self.permissions = []
        self.updates = []

    async def request_permission(self, *args, **kwargs):
        self.permissions.append((args, kwargs))
        return SimpleNamespace(outcome=SimpleNamespace(option_id=self.option_id))

    async def session_update(self, session_id, update):
        self.updates.append((session_id, update))


def _perm(entry):
    args, kwargs = entry
    tool_call = kwargs.get("tool_call") or (args[1] if len(args) > 1 else None)
    options = kwargs.get("options") or (args[2] if len(args) > 2 else [])
    return tool_call, options


def _option_id(option):
    if isinstance(option, dict):
        return option.get("option_id")
    return getattr(option, "option_id", None)


def _dump(update):
    return update.model_dump(mode="json", by_alias=True, exclude_none=True)


def _card(update):
    meta = _dump(update).get("_meta") or {}
    return meta.get("mini_ork") if isinstance(meta, dict) else None


def _text_of(message):
    content = getattr(message, "content", None)
    if isinstance(content, list):
        return " ".join(str(getattr(block, "text", "") or "") for block in content)
    return str(getattr(content, "text", "") or "")


def _messages(conn):
    return [update for _, update in conn.updates if isinstance(update, AgentMessageChunk)]


def _agent(tmp_path, monkeypatch, *, option_id=None, active=True,
           hint=_HINT, with_diagnosis=False, handle_reply=None):
    agent = MiniOrkAcpAgent(home=str(tmp_path), recipe="code-fix")
    conn = _Conn(option_id)
    agent._conn = conn
    agent._read_snapshot = lambda rid: copy.deepcopy(_SNAPSHOT)
    agent._reader = agent._read_snapshot
    if active:
        agent._direct_runs[THREAD] = RUN
    if with_diagnosis:
        run_dir = tmp_path / "runs" / RUN
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "diagnosis.json").write_text("{}", encoding="utf-8")

    calls = {"hint": [], "handle": []}

    def fake_load(home, run_id, *args, **kwargs):
        calls["hint"].append(kwargs)
        return copy.deepcopy(hint)

    async def fake_handle(agent_arg, session_id, verb, arg="", *args, **kwargs):
        calls["handle"].append((verb, arg))
        if handle_reply is not None:
            return handle_reply
        return f"recover started (log: `{tmp_path}/recover.log`)"

    monkeypatch.setattr(retry_hint, "load_or_compute", fake_load)
    monkeypatch.setattr(commands_module, "handle", fake_handle)
    agent._await_resume_started = _noop
    return agent, conn, calls


async def _noop(*args, **kwargs):
    return None


# ── offer surface ───────────────────────────────────────────────────────────


def test_offer_options_and_card(tmp_path, monkeypatch):
    agent, conn, _calls = _agent(
        tmp_path, monkeypatch, with_diagnosis=True
    )
    result = asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert result == _FAILURE_RECOVERY_DECLINED  # dismissed
    assert len(conn.permissions) == 1
    tool_call, options = _perm(conn.permissions[0])
    assert tool_call.tool_call_id == f"{RUN}:failure"
    assert {_option_id(o) for o in options} == {
        "recover", "resume:implementer-1", "forensics", "leave"
    }
    card = _card(tool_call)
    assert card["kind"] == "failure"
    assert card["id"] == f"{RUN}:failure"
    assert card["parent"] == f"{RUN}:parent"


def test_offer_calls_hint_loader_with_write_false(tmp_path, monkeypatch):
    agent, _conn, calls = _agent(tmp_path, monkeypatch)
    asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert calls["hint"]
    assert all(kwargs.get("write") is False for kwargs in calls["hint"])


def test_inactive_turn_emits_one_notice_and_no_permission(tmp_path, monkeypatch):
    agent, conn, _calls = _agent(tmp_path, monkeypatch, active=False)
    asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert conn.permissions == []
    naming = [
        message for message in _messages(conn)
        if "mini-ork recover" in _text_of(message) and RUN in _text_of(message)
    ]
    assert len(naming) == 1


def test_lane_hint_is_left_to_the_lane_repair_channel(tmp_path, monkeypatch):
    lane_hint = dict(_HINT, needs_change={"kind": "lane", "lane": "sonnet"})
    agent, conn, _calls = _agent(tmp_path, monkeypatch, hint=lane_hint)
    result = asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert result == _FAILURE_RECOVERY_NONE
    assert conn.permissions == []


# ── decisions ───────────────────────────────────────────────────────────────


def test_recover_decision_spawns_detached_recover(tmp_path, monkeypatch):
    agent, _conn, calls = _agent(tmp_path, monkeypatch, option_id="recover")
    result = asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert result == "resumed"
    assert ("recover", RUN) in calls["handle"]


def test_resume_decision_passes_from_node(tmp_path, monkeypatch):
    agent, _conn, calls = _agent(tmp_path, monkeypatch, option_id="resume:node-7")
    asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert ("recover", f"{RUN} --from-node node-7") in calls["handle"]


def test_forensics_decision_names_path_and_leaves_card_open(tmp_path, monkeypatch):
    agent, conn, calls = _agent(
        tmp_path, monkeypatch, option_id="forensics", with_diagnosis=True
    )
    result = asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert result == _FAILURE_RECOVERY_OFFERED
    assert calls["handle"] == []
    diag = str(tmp_path / "runs" / RUN / "diagnosis.json")
    assert any(diag in _text_of(message) for message in _messages(conn))


def test_leave_decision_does_not_spawn(tmp_path, monkeypatch):
    agent, conn, calls = _agent(tmp_path, monkeypatch, option_id="leave")
    result = asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert result == _FAILURE_RECOVERY_DECLINED
    assert calls["handle"] == []
    assert conn.permissions  # the offer was still made


def test_refused_spawn_is_not_reported_as_resumed(tmp_path, monkeypatch):
    agent, _conn, _calls = _agent(
        tmp_path,
        monkeypatch,
        option_id="recover",
        handle_reply="recover refused: run is not failed",
    )
    result = asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert result == _FAILURE_RECOVERY_DECLINED


def test_spawn_oserror_is_surfaced_but_never_raised(tmp_path, monkeypatch):
    agent, conn, _calls = _agent(tmp_path, monkeypatch, option_id="recover")

    async def boom(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(commands_module, "handle", boom)
    result = asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert result == _FAILURE_RECOVERY_DECLINED
    assert any("spawn failed" in _text_of(message) for message in _messages(conn))


# ── dedupe / replay / no-raise ──────────────────────────────────────────────


def test_dedupes_per_run_incarnation(tmp_path, monkeypatch):
    agent, conn, _calls = _agent(tmp_path, monkeypatch)
    asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
    assert len(conn.permissions) == 1


def test_replay_resets_the_failure_dedupe(tmp_path, monkeypatch):
    agent, _conn, _calls = _agent(tmp_path, monkeypatch)
    agent._failure_recovery_emitted.add(RUN)

    async def no_project(*args, **kwargs):
        return None

    agent._project_snapshot = no_project
    agent._replay_live_for = no_project
    agent._emit_diff_update_for_loaded_run = no_project
    asyncio.run(agent._replay_run_in_thread(THREAD, RUN, cwd=str(tmp_path)))
    assert RUN not in agent._failure_recovery_emitted


def test_never_raises_when_every_seam_fails(tmp_path, monkeypatch):
    agent = MiniOrkAcpAgent(home=str(tmp_path), recipe="code-fix")
    conn = _Conn(None)
    agent._conn = conn
    agent._direct_runs[THREAD] = RUN

    def boom(rid):
        raise RuntimeError("no snapshot")

    agent._read_snapshot = boom
    agent._reader = boom

    async def bad_load(home, run_id, *args, **kwargs):
        raise RuntimeError("no hint")

    monkeypatch.setattr(retry_hint, "load_or_compute", bad_load)
    # must not raise with every input seam failing
    asyncio.run(agent._offer_failure_recovery(THREAD, RUN))
