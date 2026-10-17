"""The ACP thread yields trouble detection to a live per-run watcher.

One run gets one observer. The thread still narrates the boundaries that ride
its conversation; it stops sampling node logs while the sidecar owns them, and
resumes the moment the sidecar's state goes stale or ends.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from mini_ork.acp.agent import MiniOrkAcpAgent
from mini_ork.acp_orchestrator import watch as w
from mini_ork.orchestration import runner_orchestrator as orch


def _snapshot():
    return {
        "status": "executing",
        "events": [{
            "event_type": "node_start",
            "created_at": time.time() - 60,
            "payload_json": json.dumps({"node_id": "impl", "node_type": "implementer"}),
        }],
        "llm_calls": [],
    }


def _agent(home):
    agent = MiniOrkAcpAgent.__new__(MiniOrkAcpAgent)
    agent._watch_seen = {}
    agent._watch_opened = set()
    agent._watch_closed = set()
    agent._watch_signals = {}
    agent._cancelled = set()
    agent._home_for = lambda _sid: home
    agent._build_refusal_message = lambda text: text
    agent.emitted = []
    agent.narrated = []

    async def _emit(_thread_id, text):
        agent.emitted.append(text)

    async def _narrate(_thread_id, _run_id, instruction):
        agent.narrated.append(instruction)

    agent._emit = _emit
    agent._narrate = _narrate
    return agent


def _run(agent):
    return asyncio.run(agent._narrate_watch("thread-1", "run-1", _snapshot()))


def _write_live_state(home, *, tick_age=0.0, ended=None):
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    state = orch.WatchState(pid=os.getpid(), started_at=time.time(),
                            last_tick=time.time() - tick_age, tick_count=2,
                            ended_reason=ended)
    orch.save_watch_state(run_dir, state)
    return run_dir


def test_live_watcher_suppresses_sampling(tmp_path, monkeypatch):
    _write_live_state(tmp_path)
    agent = _agent(tmp_path)
    calls = []
    monkeypatch.setattr(w, "sample", lambda *a, **k: calls.append(1))

    _run(agent)

    assert calls == []
    assert agent.narrated == []


def test_stale_watcher_restores_the_threads_own_sampling(tmp_path, monkeypatch):
    _write_live_state(tmp_path, tick_age=3600.0)
    agent = _agent(tmp_path)
    calls = []
    monkeypatch.setattr(w, "sample", lambda *a, **k: calls.append(k) or
                        w.Observation(run_id="run-1", node_id="impl",
                                      node_type="implementer",
                                      started_at=time.time() - 60,
                                      now=time.time(), idle_seconds=1.0))
    monkeypatch.setattr(w, "judge", lambda _obs: [])

    _run(agent)

    assert len(calls) == 1


def test_ended_watcher_restores_the_threads_own_sampling(tmp_path, monkeypatch):
    _write_live_state(tmp_path, ended="terminal:published")
    agent = _agent(tmp_path)
    calls = []
    monkeypatch.setattr(w, "sample", lambda *a, **k: calls.append(1) or
                        w.Observation(run_id="run-1", node_id="impl",
                                      node_type="implementer",
                                      started_at=time.time() - 60,
                                      now=time.time(), idle_seconds=1.0))
    monkeypatch.setattr(w, "judge", lambda _obs: [])

    _run(agent)

    assert len(calls) == 1


def test_no_watcher_keeps_the_old_path(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    calls = []
    monkeypatch.setattr(w, "sample", lambda *a, **k: calls.append(1) or
                        w.Observation(run_id="run-1", node_id="impl",
                                      node_type="implementer",
                                      started_at=time.time() - 60,
                                      now=time.time(), idle_seconds=1.0))
    monkeypatch.setattr(w, "judge", lambda _obs: [])

    _run(agent)

    assert len(calls) == 1
