"""Lane-repair channel: a run killed by an unavailable lane tells the thread
that started it, and offers a resume on a working lane.

Hermetic — no lane, no network, no subprocess: the launcher / reader seams are
stubs, ``mini_ork.web.control.launch_run`` is monkeypatched, the retry hint is
stubbed, and the ``/recover`` handler is replaced so nothing spawns. Covers:

  * ``MO_RUN_OWNER`` wiring (``/run`` env, ``start_run`` extra_env);
  * ``_offer_lane_repair`` on an active turn (buttons) and an inactive turn
    (one message, deduped), and that a non-lane hint keeps today's behaviour;
  * the resume / retry / abandon choices, and that a resume is followed through
    by the SAME follower task (no second ``_start_child_follow``), prompting
    again on a second failure and closing the run card ``failed``;
  * the offline thread append in ``retry_notify.notify`` (replay shape + dedupe);
  * never raising into the follower.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from acp.schema import (  # noqa: E402
    AgentMessageChunk,
    SessionNotification,
    TextContentBlock,
    ToolCallProgress,
)

from mini_ork.acp import commands as acp_commands  # noqa: E402
from mini_ork.acp.agent import MiniOrkAcpAgent  # noqa: E402
from mini_ork.recipes_catalog import RecipeInfo  # noqa: E402
from mini_ork.recovery import retry_hint, retry_notify  # noqa: E402


# ── fixtures + helpers ─────────────────────────────────────────────────────


def _recipe(name: str) -> RecipeInfo:
    """Minimal engine ``RecipeInfo`` for the picker mocks (id is all they read)."""
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
def _fast_lane_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2 s poll between hint reads is for a real run's lagging DB write;
    tests return a classified hint immediately or want the retry instant."""
    monkeypatch.setattr("mini_ork.acp.agent._LANE_REPAIR_RETRY_DELAY", 0)


@pytest.fixture(autouse=True)
def _catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        lambda *a, **k: [{"id": "opus", "name": "Opus"}],
    )
    monkeypatch.setattr(
        "mini_ork.recipes_catalog.list_recipes",
        lambda *a, **k: [_recipe("code-fix")],
    )


def _text_block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


def _failed_reader(_run_id: str) -> dict[str, Any]:
    return {"status": "failed", "events": [], "llm_calls": []}


def _seq_reader(statuses: list[str]):
    """Reader stub cycling ``statuses``, repeating the last — so a resume can
    be modelled as ``failed -> running -> failed`` across the follower's reads."""
    seq = list(statuses)
    box = {"i": 0}

    def _read(_run_id: str) -> dict[str, Any]:
        status = seq[min(box["i"], len(seq) - 1)]
        box["i"] += 1
        return {"status": status, "events": [], "llm_calls": []}

    return _read


def _lane_hint(
    run_id: str = "run-lane-1",
    *,
    lane: str = "minimax",
    alias: str = "codex_lens",
    nodes: tuple[str, ...] = ("prior_art_lens", "implementer"),
    suggestions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A ``retry_hint.load_or_compute`` result for a lane-unavailable run."""
    return {
        "run_id": run_id,
        "failed_node": "prior_art_lens",
        "retryable": True,
        "strategy": "resume",
        "needs_change": {
            "kind": "lane",
            "summary": "minimax is out of quota",
            "detail": "API Error: Request rejected (429) · Token Plan usage limit reached",
            "lane": lane,
            "alias": alias,
            "provider": "MiniMax",
            "nodes": list(nodes),
            "suggestions": (
                suggestions
                if suggestions is not None
                else [
                    {"lane": "deepseek", "reason": "19 successful calls in the last 6h"},
                    {"lane": "opus", "reason": "no recent calls (untested)"},
                ]
            ),
        },
        "command": f"mini-ork recover {run_id} --lane {alias}=deepseek",
    }


class _Conn:
    """ACP connection capturing session updates and scripted permission picks.

    ``outcome_id`` is one id (every prompt gets it) or a list (each prompt takes
    the next; the last repeats) — a resume that fails again prompts twice.
    """

    def __init__(
        self, outcome_id: str | list[str] | None = None, *, raise_perm: bool = False
    ) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.outcome_ids: list[str] = (
            list(outcome_id) if isinstance(outcome_id, list) else []
        )
        self.outcome_id = outcome_id if isinstance(outcome_id, str) else None
        self.raise_perm = raise_perm

    async def session_update(self, session_id: str, update: Any) -> None:
        self.sent.append((session_id, update))

    async def request_permission(self, *, session_id, tool_call, options) -> Any:
        if self.raise_perm:
            raise RuntimeError("no UI")
        from acp.schema import AllowedOutcome, RequestPermissionResponse

        self.calls.append({
            "session_id": session_id,
            "tool_call_id": tool_call.tool_call_id,
            "title": getattr(tool_call, "title", None),
            "option_ids": [o.option_id for o in options],
            "kinds": [o.kind for o in options],
        })
        picked = self.outcome_ids.pop(0) if self.outcome_ids else self.outcome_id
        if picked is None:
            return None
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=picked)
        )

    # convenience accessors
    def messages(self) -> list[AgentMessageChunk]:
        return [u for _s, u in self.sent if isinstance(u, AgentMessageChunk)]

    def progress(self, tool_call_id: str) -> list[ToolCallProgress]:
        return [
            u for _s, u in self.sent
            if isinstance(u, ToolCallProgress) and u.tool_call_id == tool_call_id
        ]


def _new_thread_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **ctor_kwargs: Any
) -> tuple[MiniOrkAcpAgent, Path, str]:
    """A thread session on an in-place (non-git) project. Returns (agent, proj, sid)."""
    monkeypatch.setattr("mini_ork.acp.agent.mint_run_id", lambda: "run-lane-1")
    proj = tmp_path / "proj"
    proj.mkdir()
    # ``_home_for(run_id)`` resolves to ``<proj>/.mini-ork`` when it exists.
    (proj / ".mini-ork").mkdir(parents=True, exist_ok=True)
    # A tiny start timeout: it also bounds the post-resume wait, so a reader
    # that never leaves the terminal set fails fast instead of spinning.
    ctor_kwargs.setdefault("start_timeout", 0.05)
    agent = MiniOrkAcpAgent(**ctor_kwargs)
    resp = asyncio.run(agent.new_session(cwd=str(proj)))
    asyncio.run(agent.set_config_option("workspace", resp.session_id, "in-place"))
    return agent, proj, resp.session_id


def _patch_hint(monkeypatch: pytest.MonkeyPatch, hint: Any) -> None:
    def _load(_home, _run_id, *, write=True):  # noqa: ANN001
        if isinstance(hint, Exception):
            raise hint
        return hint

    monkeypatch.setattr(retry_hint, "load_or_compute", _load)


# ── 1. owner = thread ───────────────────────────────────────────────────────


def test_run_launch_names_the_thread_as_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/run`` puts ``MO_RUN_OWNER=thread:<sid>`` into the launched run's env."""
    captured: dict[str, Any] = {}

    def fake_launch_run(home, recipe, kickoff_text, *, run_id=None, extra_env=None):
        captured["extra_env"] = dict(extra_env or {})
        return {"ok": True, "run_id": run_id or "run-x"}

    monkeypatch.setattr("mini_ork.web.control.launch_run", fake_launch_run)
    _patch_hint(monkeypatch, None)

    agent, _proj, sid = _new_thread_agent(
        tmp_path, monkeypatch, reader=_failed_reader, poll_interval=0
    )
    agent.on_connect(_Conn())
    resp = asyncio.run(agent.prompt(sid, [_text_block("/run do it")]))

    assert resp.stop_reason == "end_turn"
    assert captured["extra_env"]["MO_RUN_OWNER"] == f"thread:{sid}"
    # The overlay is dropped once the follow ends — one entry per ``/run``.
    assert "run-lane-1" not in agent._run_env


def test_start_run_names_the_thread_owner_when_mo_thread_id_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mini_ork.mcp_context import server as mcp_server

    home = tmp_path / "proj" / ".mini-ork"
    home.mkdir(parents=True)
    captured: dict[str, Any] = {}

    def fake_launch_run(h, recipe, kickoff_text, *, run_id=None, extra_env=None):
        captured["extra_env"] = dict(extra_env or {})
        return {"ok": True, "run_id": run_id or "run-x"}

    monkeypatch.setattr("mini_ork.web.control.launch_run", fake_launch_run)
    monkeypatch.setenv("MO_THREAD_ID", "orch-1")
    out = mcp_server._start_run(
        home,
        {"recipe": "code-fix", "kickoff_markdown": "# do it", "workspace": "in-place"},
    )
    assert out.get("ok") is True
    assert captured["extra_env"]["MO_RUN_OWNER"] == "thread:orch-1"


def test_start_run_has_no_owner_without_mo_thread_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mini_ork.mcp_context import server as mcp_server

    home = tmp_path / "proj" / ".mini-ork"
    home.mkdir(parents=True)
    captured: dict[str, Any] = {}

    def fake_launch_run(h, recipe, kickoff_text, *, run_id=None, extra_env=None):
        captured["extra_env"] = dict(extra_env or {})
        return {"ok": True, "run_id": run_id or "run-x"}

    monkeypatch.setattr("mini_ork.web.control.launch_run", fake_launch_run)
    monkeypatch.delenv("MO_THREAD_ID", raising=False)
    mcp_server._start_run(
        home,
        {"recipe": "code-fix", "kickoff_markdown": "# do it", "workspace": "in-place"},
    )
    assert "MO_RUN_OWNER" not in captured["extra_env"]


# ── 2. the follower prompt (active turn) ────────────────────────────────────


def _stub_recover(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the ``/recover`` handler so nothing spawns; record its args."""
    calls: list[tuple[str, str]] = []

    async def fake_recover(agent, session_id, arg):  # noqa: ANN001
        calls.append((session_id, arg))
        return f"`/recover` `{arg}` started."

    monkeypatch.setitem(acp_commands.HANDLERS, "recover", fake_recover)
    return calls


def test_active_turn_offers_repair_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``/run`` that dies on a lane gets Resume / Retry / Leave buttons."""
    hint = _lane_hint(suggestions=[{"lane": "deepseek", "reason": "19 calls in 6h"}])
    _patch_hint(monkeypatch, hint)
    agent, _proj, sid = _new_thread_agent(
        tmp_path, monkeypatch, reader=_failed_reader, poll_interval=0
    )
    conn = _Conn(outcome_id="lane:deepseek")
    agent.on_connect(conn)
    recover_calls = _stub_recover(monkeypatch)

    asyncio.run(agent.prompt(sid, [_text_block("/run do it")]))

    assert len(conn.calls) == 1
    call = conn.calls[0]
    assert call["session_id"] == sid
    assert call["tool_call_id"] == "run-lane-1:parent"
    assert call["option_ids"] == ["lane:deepseek", "same", "abandon"]
    assert call["kinds"] == ["allow_once", "allow_once", "reject_once"]
    assert "minimax" in (call["title"] or "")
    # Picking a lane starts the resume (the follow is covered by the next test).
    assert recover_calls == [(sid, "run-lane-1 --lane codex_lens=deepseek")]


def test_resume_is_followed_in_the_same_task_and_a_second_failure_prompts_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL follower keeps following after a resume.

    Reader sequence ``failed -> running -> failed`` models the detached
    ``recover`` flipping the DB row. The resume must (a) start ``/recover``,
    (b) not spawn a second follower task, (c) prompt AGAIN on the second lane
    failure (the dedupe marker is dropped on a resume), and (d) close the run
    card ``failed`` exactly once when the user finally leaves it.
    """
    hint = _lane_hint(suggestions=[{"lane": "deepseek", "reason": "19 calls in 6h"}])
    _patch_hint(monkeypatch, hint)
    agent, _proj, sid = _new_thread_agent(
        tmp_path,
        monkeypatch,
        reader=_seq_reader(["failed", "running", "failed"]),
        poll_interval=0,
    )
    conn = _Conn(outcome_id=["lane:deepseek", "abandon"])
    agent.on_connect(conn)
    recover_calls = _stub_recover(monkeypatch)

    asyncio.run(agent.prompt(sid, [_text_block("/run do it")]))

    assert recover_calls == [(sid, "run-lane-1 --lane codex_lens=deepseek")]
    assert any("`/recover`" in m.content.text for m in conn.messages())
    # A second lane failure prompted again (no silent short-circuit).
    assert len(conn.calls) == 2
    assert conn.calls[1]["option_ids"] == ["lane:deepseek", "same", "abandon"]
    # The final "Leave it" closed the card exactly once, and no follower lingers.
    assert [p.status for p in conn.progress("run-lane-1:parent")] == ["failed"]
    assert "run-lane-1" not in agent._followers


def test_choosing_same_retries_on_the_failed_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hint = _lane_hint(suggestions=[{"lane": "deepseek", "reason": "ok"}])
    _patch_hint(monkeypatch, hint)
    agent, _proj, sid = _new_thread_agent(
        tmp_path,
        monkeypatch,
        reader=_seq_reader(["failed", "running", "failed"]),
        poll_interval=0,
    )
    agent.on_connect(_Conn(outcome_id=["same", "abandon"]))
    recover_calls = _stub_recover(monkeypatch)

    asyncio.run(agent.prompt(sid, [_text_block("/run do it")]))

    assert recover_calls == [(sid, "run-lane-1")]


def test_child_follow_survives_a_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orchestrator-child path: the resume does NOT re-enter ``_start_child_follow``.

    ``_handle_lane_repair_decision`` used to call ``_start_child_follow`` from
    inside the follower task, which saw itself live and no-op'd ("already
    following") — the run card was then never closed. The follower now stays in
    its own task across the resume.
    """
    hint = _lane_hint(suggestions=[{"lane": "deepseek", "reason": "ok"}])
    _patch_hint(monkeypatch, hint)
    agent, _proj, sid = _new_thread_agent(
        tmp_path,
        monkeypatch,
        reader=_seq_reader(["failed", "running", "failed"]),
        poll_interval=0,
    )
    conn = _Conn(outcome_id=["lane:deepseek", "abandon"])
    agent.on_connect(conn)
    recover_calls = _stub_recover(monkeypatch)

    async def _drive() -> None:
        # A live orchestrator turn makes this an active turn (buttons path).
        live = asyncio.create_task(asyncio.sleep(3600))
        agent._orchestrator_tasks[sid] = live
        try:
            await agent._start_child_follow(sid, "run-lane-1", recipe="code-fix")
            follower = agent._followers.get("run-lane-1")
            assert follower is not None
            await follower
        finally:
            live.cancel()
            agent._orchestrator_tasks.pop(sid, None)

    asyncio.run(_drive())

    assert recover_calls == [(sid, "run-lane-1 --lane codex_lens=deepseek")]
    assert len(conn.calls) == 2
    assert [p.status for p in conn.progress("run-lane-1:parent")] == ["failed"]
    assert "run-lane-1" not in agent._followers


def test_a_resume_that_never_takes_closes_the_card_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detached ``recover`` never flips the row: the bounded wait expires
    and the marker is closed ``failed`` rather than left hanging ``in_progress``."""
    hint = _lane_hint(suggestions=[{"lane": "deepseek", "reason": "ok"}])
    _patch_hint(monkeypatch, hint)
    agent, _proj, sid = _new_thread_agent(
        tmp_path, monkeypatch, reader=_failed_reader, poll_interval=0
    )
    conn = _Conn(outcome_id="lane:deepseek")
    agent.on_connect(conn)
    recover_calls = _stub_recover(monkeypatch)

    asyncio.run(agent.prompt(sid, [_text_block("/run do it")]))

    assert recover_calls == [(sid, "run-lane-1 --lane codex_lens=deepseek")]
    assert len(conn.calls) == 1  # no second prompt
    assert [p.status for p in conn.progress("run-lane-1:parent")] == ["failed"]
    assert "run-lane-1" not in agent._followers


def test_abandon_closes_the_marker_and_prints_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hint = _lane_hint(suggestions=[{"lane": "deepseek", "reason": "ok"}])
    _patch_hint(monkeypatch, hint)
    agent, _proj, sid = _new_thread_agent(
        tmp_path, monkeypatch, reader=_failed_reader, poll_interval=0
    )
    conn = _Conn(outcome_id="abandon")
    agent.on_connect(conn)
    recover_calls = _stub_recover(monkeypatch)

    asyncio.run(agent.prompt(sid, [_text_block("/run do it")]))

    assert recover_calls == []
    assert [p.status for p in conn.progress("run-lane-1:parent")] == ["failed"]
    assert any("Left as is." in m.content.text for m in conn.messages())
    assert any("mini-ork recover run-lane-1" in m.content.text for m in conn.messages())


# ── 3. the inactive turn (one message, deduped) ─────────────────────────────


def test_inactive_turn_posts_one_message_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hint = _lane_hint()
    _patch_hint(monkeypatch, hint)
    agent, _proj, sid = _new_thread_agent(
        tmp_path, monkeypatch, reader=_failed_reader, poll_interval=0
    )
    conn = _Conn()
    agent.on_connect(conn)

    # No active turn → the one-time message path.
    assert asyncio.run(agent._offer_lane_repair(sid, "run-lane-1")) == "prompted"
    msgs = conn.messages()
    assert len(msgs) == 1
    text = msgs[0].content.text
    assert "**run-lane-1 needs repair.**" in text
    assert "MiniMax lane `minimax`" in text
    assert "used by codex_lens: prior_art_lens, implementer" in text
    assert "/recover run-lane-1 --lane codex_lens=deepseek" in text
    assert "/recover run-lane-1 --lane codex_lens=opus" in text

    # Second call → deduped, no second message.
    assert asyncio.run(agent._offer_lane_repair(sid, "run-lane-1")) == "prompted"
    assert len(conn.messages()) == 1


def test_non_lane_hint_keeps_todays_failed_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hint = _lane_hint()
    hint["needs_change"] = {"kind": "code", "summary": "impl bug", "detail": "x"}
    _patch_hint(monkeypatch, hint)
    agent, _proj, sid = _new_thread_agent(
        tmp_path, monkeypatch, reader=_failed_reader, poll_interval=0
    )
    conn = _Conn()
    agent.on_connect(conn)

    asyncio.run(agent.prompt(sid, [_text_block("/run do it")]))

    assert conn.calls == []  # no prompt
    assert [p.status for p in conn.progress("run-lane-1:parent")] == ["failed"]


# ── 4. never raise into the follower ────────────────────────────────────────


def test_a_raising_hint_does_not_break_the_follower(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_hint(monkeypatch, RuntimeError("hint exploded"))
    agent, _proj, sid = _new_thread_agent(
        tmp_path, monkeypatch, reader=_failed_reader, poll_interval=0
    )
    conn = _Conn()
    agent.on_connect(conn)

    resp = asyncio.run(agent.prompt(sid, [_text_block("/run do it")]))

    assert resp.stop_reason == "end_turn"
    assert [p.status for p in conn.progress("run-lane-1:parent")] == ["failed"]


def test_a_failing_permission_ui_keeps_the_marker_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_hint(monkeypatch, _lane_hint(suggestions=[{"lane": "deepseek", "reason": "ok"}]))
    agent, _proj, sid = _new_thread_agent(
        tmp_path, monkeypatch, reader=_failed_reader, poll_interval=0
    )
    conn = _Conn(raise_perm=True)
    agent.on_connect(conn)

    resp = asyncio.run(agent.prompt(sid, [_text_block("/run do it")]))

    assert resp.stop_reason == "end_turn"  # no exception escaped
    assert conn.progress("run-lane-1:parent") == []


# ── 5. the shared message builder ───────────────────────────────────────────


def test_lane_repair_message_shape() -> None:
    text = retry_notify.lane_repair_message(_lane_hint())
    lines = text.splitlines()
    assert lines[0] == (
        "**run-lane-1 needs repair.** MiniMax lane `minimax` "
        "(used by codex_lens: prior_art_lens, implementer) failed:"
    )
    assert lines[1].startswith("API Error: Request rejected (429)")
    assert lines[2] == "Resume from `prior_art_lens` on a working lane:"
    assert lines[3] == "/recover run-lane-1 --lane codex_lens=deepseek   (19 successful calls in the last 6h)"
    assert lines[4] == "/recover run-lane-1 --lane codex_lens=opus   (no recent calls (untested))"


def test_lane_repair_message_with_no_suggestions_offers_a_plain_recover() -> None:
    text = retry_notify.lane_repair_message(_lane_hint(suggestions=[]))
    assert "/recover run-lane-1" in text.splitlines()


# ── 6. the offline thread append (retry_notify.notify) ──────────────────────


def _notify_home(tmp_path: Path, run_id: str = "run-lane-1") -> Path:
    home = tmp_path / "proj" / ".mini-ork"
    (home / "runs" / run_id).mkdir(parents=True)
    return home


def test_notify_appends_one_lane_repair_record_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _notify_home(tmp_path)
    monkeypatch.setenv(retry_notify.MO_RUN_OWNER, "thread:orch-1")
    _patch_hint(monkeypatch, _lane_hint())

    assert retry_notify.notify(home, "run-lane-1") is not None

    store = home / "acp-threads" / "orch-1.jsonl"
    recs = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines() if line]
    lane_recs = [r for r in recs if r.get("marker") == "lane-repair:run-lane-1"]
    assert len(lane_recs) == 1
    upd = lane_recs[0]["update"]
    # The exact replay shape (camelCase; validates as a SessionNotification).
    assert upd["sessionUpdate"] == "agent_message_chunk"
    assert upd["content"]["type"] == "text"
    SessionNotification.model_validate({"sessionId": "orch-1", "update": upd})
    assert "/recover run-lane-1 --lane codex_lens=deepseek" in upd["content"]["text"]

    # Repeat notify → no duplicate.
    assert retry_notify.notify(home, "run-lane-1") is not None
    recs2 = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines() if line]
    assert len([r for r in recs2 if r.get("marker") == "lane-repair:run-lane-1"]) == 1


def test_notify_is_silent_for_a_non_lane_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _notify_home(tmp_path)
    monkeypatch.setenv(retry_notify.MO_RUN_OWNER, "thread:orch-1")
    hint = _lane_hint()
    hint["needs_change"] = {"kind": "code", "summary": "impl bug", "detail": "x"}
    _patch_hint(monkeypatch, hint)

    retry_notify.notify(home, "run-lane-1")

    assert not (home / "acp-threads" / "orch-1.jsonl").exists()


def test_notify_cross_path_dedupe_against_a_live_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message the live agent already emitted (same text) is not re-appended."""
    home = _notify_home(tmp_path)
    monkeypatch.setenv(retry_notify.MO_RUN_OWNER, "thread:orch-1")
    hint = _lane_hint()
    _patch_hint(monkeypatch, hint)

    store = home / "acp-threads" / "orch-1.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({
        "type": "update",
        "update": {
            "content": {"type": "text", "text": retry_notify.lane_repair_message(hint)},
            "sessionUpdate": "agent_message_chunk",
        },
    }) + "\n", encoding="utf-8")

    retry_notify.notify(home, "run-lane-1")

    recs = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines() if line]
    assert len(recs) == 1  # the live one only — no marker duplicate


def test_notify_fails_soft_on_an_unsafe_thread_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-``orch-`` owner id must not raise out of ``notify``."""
    home = _notify_home(tmp_path)
    monkeypatch.setenv(retry_notify.MO_RUN_OWNER, "thread:../escape")
    _patch_hint(monkeypatch, _lane_hint())

    assert retry_notify.notify(home, "run-lane-1") is not None
    assert not (home / "acp-threads").exists()
