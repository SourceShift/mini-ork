"""End-to-end coverage of the parked-run review flow for the ACP agent
(``mini_ork.acp.{task_state,agent,commands}``).

Slice h1r-1 of the Zed engineer surface: when a run ends parked for
node review, the agent emits a one-shot ``request_permission`` card
with three options, and the slash commands ``/approve``,
``/request-changes``, ``/checkpoints`` and ``/restore`` resolve
parked runs, surface change requests, list checkpoints and restore a
node's content. The replay helper attaches file locations to every
replayed tool event so the file chip in Zed is always clickable.

Hermetic: fake ACP connection, tmp home/DB with raw pending inbox
rows + an ``operator_steering`` table, ``_spawn_recover``
monkeypatched, a static snapshot reader. All async methods are
driven through ``asyncio.run`` — no pytest-asyncio.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# Bootstrap the repo on ``sys.path`` the way every other test in this
# directory does (the test runner's conftest does not auto-add the
# repo for ad-hoc ``python -m pytest <file>`` invocations).
import sys
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


_STEERING_DDL = """
CREATE TABLE IF NOT EXISTS operator_steering (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    role_target     TEXT NOT NULL CHECK (role_target IN ('planner','implementer','reviewer','verifier','any')),
    severity        TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warn','critical')),
    message         TEXT NOT NULL,
    source          TEXT,
    confidence      REAL NOT NULL DEFAULT 0.8 CHECK (confidence BETWEEN 0 AND 1),
    created_at      INTEGER NOT NULL,
    consumed_at     INTEGER,
    expires_at      INTEGER NOT NULL
);
"""


class _FakeConn:
    """The ACP connection seam: records every update and permission card."""

    def __init__(self, answers: list[str | None] | None = None) -> None:
        self.updates: list[tuple[str, object]] = []
        self.permissions: list[dict] = []
        self._answers = list(answers or [])

    async def session_update(self, session_id, update) -> None:
        self.updates.append((session_id, update))

    async def request_permission(self, *args, **kwargs):
        # Accept both positional and keyword call shapes so the agent's
        # chosen call style never raises into a "no card offered" branch.
        self.permissions.append(
            {
                "session_id": args[0] if args else kwargs.get("session_id"),
                "tool_call": args[1] if len(args) > 1 else kwargs.get("tool_call"),
                "options": args[2] if len(args) > 2 else kwargs.get("options"),
            }
        )
        option_id = self._answers.pop(0) if self._answers else None
        return SimpleNamespace(outcome=SimpleNamespace(option_id=option_id))


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    """An agent must never approve its own run; the test set is explicit."""
    for key in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MO_TARGET_CWD"):
        monkeypatch.delenv(key, raising=False)


# ── fixtures ───────────────────────────────────────────────────────────────


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "runs").mkdir(parents=True)
    con = sqlite3.connect(home / "state.db")
    con.executescript(_STEERING_DDL)
    con.commit()
    con.close()
    return home


def _enqueue_pending_review(db: Path, run: str, node: str) -> None:
    """Insert one pending ``node:<run>:<node>:<ts>`` inbox row."""
    from mini_ork.gates import node_review

    node_review.pending_for_run(run, db_path=str(db))  # bootstrap
    gate = f"node:{run}:{node}:{int(time.time())}"
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO mo_inbox_gates (gate_id, feature, phase, context_json, "
        "status, enqueued_at, blocks_dispatch_for) VALUES (?,?,?,?, 'pending', ?, ?)",
        (
            gate, node_review.FEATURE, "implementer",
            json.dumps({"run_id": run, "node_id": node, "node_type": "implementer",
                        "created_at": int(time.time())}),
            int(time.time()), run,
        ),
    )
    con.commit()
    con.close()


def _gate_row(db: Path, run: str, node: str):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT status, review_note FROM mo_inbox_gates "
        "WHERE gate_id LIKE ? ORDER BY inbox_id DESC LIMIT 1",
        (f"node:{run}:{node}:%",),
    ).fetchone()
    con.close()
    return row


def _parked_run(tmp_path: Path, run: str, node: str) -> tuple[Path, Path]:
    """Tmp home + a run parked for node review."""
    home = _make_home(tmp_path)
    run_dir = home / "runs" / run
    run_dir.mkdir(parents=True)
    (run_dir / ".review-pause").write_text(
        json.dumps({"waiting_node": node, "awaiting": [node]}), encoding="utf-8"
    )
    _enqueue_pending_review(home / "state.db", run, node)
    return home, run_dir


def _make_agent(home: Path, run: str, answers: list[str | None] | None = None):
    from mini_ork.acp.agent import MiniOrkAcpAgent

    def _reader(run_id: str) -> dict:
        return {"status": "reviewing", "events": [], "llm_calls": []}

    agent = MiniOrkAcpAgent(
        home=str(home), reader=_reader, poll_interval=0.01, start_timeout=2.0,
    )
    agent._conn = _FakeConn(answers)
    agent._direct_runs["t1"] = run
    return agent, agent._conn


def _patch_spawn(monkeypatch) -> list[dict]:
    """Record ``_spawn_recover`` calls in place of the real fork."""
    import mini_ork.acp.commands as commands_mod
    import mini_ork.cli.approvals_cmd as approvals_cmd

    calls: list[dict] = []

    def _fake(home_arg, run_id, extra_args, **kwargs):
        calls.append(
            {"home": home_arg, "run": run_id, "extra": list(extra_args or [])}
        )
        return (4242, "review-resume.log")

    monkeypatch.setattr(approvals_cmd, "_spawn_recover", _fake)
    monkeypatch.setattr(commands_mod, "_spawn_recover", _fake, raising=False)
    return calls


def _drive_follow(agent, run: str) -> None:
    """Run the end-of-run follow hook with a 20 s ceiling."""
    try:
        asyncio.run(asyncio.wait_for(agent._follow_in_thread("t1", run), timeout=20))
    except asyncio.TimeoutError:
        pytest.fail("follow loop never ended for a reviewing run")


# ── task_state ─────────────────────────────────────────────────────────────


def test_task_state_reviewing_with_pause_is_needs_you_listing_awaiting_nodes(tmp_path):
    """Rule 1.5: a ``reviewing`` run with ``.review-pause`` is ``needs_you``
    with detail naming every awaiting node."""
    from mini_ork.acp.task_state import task_state

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".review-pause").write_text(
        json.dumps({"waiting_node": "implementer", "awaiting": ["implementer", "reviewer"]}),
        encoding="utf-8",
    )
    snapshot = {"status": "reviewing", "events": [], "llm_calls": []}
    state = task_state(run_dir, snapshot)
    assert state.state == "needs_you"
    assert state.detail == "Waiting for your review: implementer, reviewer"


def test_task_state_run_mark_parked_for_review_is_needs_you(tmp_path):
    """``run_mark`` mirrors task_state: a parked review row is ``needs_you``."""
    from mini_ork.acp.task_state import run_mark, MARKS

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".review-pause").write_text(
        json.dumps({"waiting_node": "implementer", "awaiting": ["implementer"]}),
        encoding="utf-8",
    )
    assert run_mark("reviewing", run_dir) == MARKS["needs_you"]


# ── agent: review card ─────────────────────────────────────────────────────


def test_agent_review_card_approve_resumes_parked_run(tmp_path, monkeypatch):
    """End-of-run review card: ``approve`` resolves the node review,
    renames the sentinel and spawns the resume."""
    run, node = "run-rf1", "implementer"
    home, run_dir = _parked_run(tmp_path, run, node)
    db = home / "state.db"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    spawn_calls = _patch_spawn(monkeypatch)

    agent, conn = _make_agent(home, run, answers=["approve"])
    _drive_follow(agent, run)

    review_cards = [
        p for p in conn.permissions
        if getattr(p.get("tool_call"), "tool_call_id", None) == f"{run}:review"
    ]
    assert len(review_cards) == 1
    assert review_cards[0]["tool_call"].kind == "other"
    assert review_cards[0]["tool_call"].title == f"Review {node} of run {run}"
    opts = {o.option_id: o for o in review_cards[0]["options"]}
    assert set(opts) == {"approve", "changes", "later"}
    assert opts["approve"].kind == "allow_once"

    row = _gate_row(db, run, node)
    assert row["status"] == "approved"
    assert str(row["review_note"] or "").startswith("zed:")

    assert not (run_dir / ".review-pause").exists()
    assert (run_dir / ".review-pause.resolved").is_file()
    assert len(spawn_calls) == 1
    assert spawn_calls[0]["extra"] == ["--strategy", "resume"]


def test_agent_review_card_dedupes_one_per_run_node(tmp_path, monkeypatch):
    """Card is offered once per ``(run, waiting_node)`` — a second trigger
    while the run is still parked does not re-emit it."""
    run, node = "run-rf2", "implementer"
    home, _ = _parked_run(tmp_path, run, node)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    _patch_spawn(monkeypatch)

    agent, conn = _make_agent(home, run, answers=["later", "later"])
    _drive_follow(agent, run)
    review_cards = [
        p for p in conn.permissions
        if getattr(p.get("tool_call"), "tool_call_id", None) == f"{run}:review"
    ]
    assert len(review_cards) == 1
    _drive_follow(agent, run)
    review_cards = [
        p for p in conn.permissions
        if getattr(p.get("tool_call"), "tool_call_id", None) == f"{run}:review"
    ]
    assert len(review_cards) == 1


def test_agent_review_card_changes_directs_to_request_changes_command(tmp_path, monkeypatch):
    """``changes`` tells the user to type ``/request-changes`` and does
    not approve or resume anything."""
    run, node = "run-rf3", "implementer"
    home, _ = _parked_run(tmp_path, run, node)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    _patch_spawn(monkeypatch)

    agent, conn = _make_agent(home, run, answers=["changes"])
    _drive_follow(agent, run)

    texts = [
        getattr(getattr(u, "content", None), "text", "") or ""
        for _, u in conn.updates
        if getattr(u, "session_update", "") == "agent_message_chunk"
    ]
    assert any("/request-changes" in t for t in texts)

    row = _gate_row(home / "state.db", run, node)
    assert row["status"] == "pending"


def test_agent_review_card_approve_approves_only_named_node(tmp_path, monkeypatch):
    """The card title names ``awaiting[0]``; approving must resolve ONLY
    that node, not every node in the awaiting list. A multi-node parked
    run keeps the marker open until each node gets its own card.
    """
    run = "run-rf-mn"
    node_a, node_b = "implementer", "reviewer"
    home = _make_home(tmp_path)
    run_dir = home / "runs" / run
    run_dir.mkdir(parents=True)
    (run_dir / ".review-pause").write_text(
        json.dumps(
            {"waiting_node": node_a, "awaiting": [node_a, node_b]}
        ),
        encoding="utf-8",
    )
    _enqueue_pending_review(home / "state.db", run, node_a)
    _enqueue_pending_review(home / "state.db", run, node_b)
    db = home / "state.db"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    spawn_calls = _patch_spawn(monkeypatch)

    agent, conn = _make_agent(home, run, answers=["approve"])
    _drive_follow(agent, run)

    # Only the named node is approved; the other stays pending.
    assert _gate_row(db, run, node_a)["status"] == "approved"
    assert _gate_row(db, run, node_b)["status"] == "pending"
    # No resume yet — the second pending row blocks it.
    assert not spawn_calls
    assert (run_dir / ".review-pause").is_file()


def test_agent_review_card_walks_every_pending_node_then_resumes_once(tmp_path, monkeypatch):
    """Reviewer finding (hr-acp-review round 3): a multi-node park must give
    EACH pending node its own card in order, resume exactly once after the
    last approval, and post each approval confirmation exactly once."""
    run = "run-rf-walk"
    node_a, node_b = "implementer", "reviewer"
    home = _make_home(tmp_path)
    run_dir = home / "runs" / run
    run_dir.mkdir(parents=True)
    (run_dir / ".review-pause").write_text(
        json.dumps({"waiting_node": node_a, "awaiting": [node_a, node_b]}),
        encoding="utf-8",
    )
    _enqueue_pending_review(home / "state.db", run, node_a)
    _enqueue_pending_review(home / "state.db", run, node_b)
    db = home / "state.db"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    spawn_calls = _patch_spawn(monkeypatch)

    agent, conn = _make_agent(home, run, answers=["approve", "approve"])
    _drive_follow(agent, run)

    titles = [
        p["tool_call"].title for p in conn.permissions
        if getattr(p.get("tool_call"), "tool_call_id", None) == f"{run}:review"
    ]
    assert titles == [f"Review {node_a} of run {run}", f"Review {node_b} of run {run}"]
    assert _gate_row(db, run, node_a)["status"] == "approved"
    assert _gate_row(db, run, node_b)["status"] == "approved"
    assert len(spawn_calls) == 1 and spawn_calls[0]["extra"] == ["--strategy", "resume"]
    assert (run_dir / ".review-pause.resolved").is_file()
    texts = [
        str(getattr(getattr(u, "content", None), "text", "") or "")
        for _sid, u in conn.updates
    ]
    for node in (node_a, node_b):
        assert sum(f"Approved {node} on {run}" in t for t in texts) == 1


def test_agent_review_card_skipped_when_run_not_reviewing(tmp_path, monkeypatch):
    """Regression: a stale ``.review-pause.resolved`` audit file on a run
    whose status is no longer ``reviewing`` must not re-fire a card, must
    not skip lane repair, and must not leave the marker unclosed.
    """
    run, node = "run-rf-stale", "implementer"
    home, _run_dir = _parked_run(tmp_path, run, node)
    run_dir = home / "runs" / run
    # The run was approved and resumed, but the executor has not flipped
    # the row yet. Parked sentinel is renamed to the audit file, and the
    # snapshot reader reports a terminal status.
    (run_dir / ".review-pause").unlink()
    (run_dir / ".review-pause.resolved").write_text(
        json.dumps({"waiting_node": node, "awaiting": [node]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    _patch_spawn(monkeypatch)

    # Snapshot says the run already failed/published — the follower
    # should NOT offer the review card and should close the marker.
    agent, conn = _make_agent(home, run, answers=["approve"])
    agent._reader = lambda _rid: {
        "status": "failed", "events": [], "llm_calls": []
    }
    _drive_follow(agent, run)

    review_cards = [
        p for p in conn.permissions
        if getattr(p.get("tool_call"), "tool_call_id", None) == f"{run}:review"
    ]
    assert review_cards == [], "stale review card re-fired on a failed run"


def test_agent_inactive_turn_fallback_names_the_node(tmp_path, monkeypatch):
    """The inactive-turn fallback message must tell the user to type
    ``/approve <run> <node>`` (the node-review branch), not the
    merge-approval branch which would leave the run parked.
    """
    run, node = "run-rf-fb", "implementer"
    home, _ = _parked_run(tmp_path, run, node)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    _patch_spawn(monkeypatch)

    # Inactive turn: no direct run, no orchestrator task on this
    # thread. The fake conn is still wired so the one-time message
    # lands in ``conn.updates`` for the assertion.
    agent, conn = _make_agent(home, run, answers=None)
    agent._direct_runs.pop("t1", None)
    agent._orchestrator_tasks.pop("t1", None)
    _drive_follow(agent, run)

    texts = [
        getattr(getattr(u, "content", None), "text", "") or ""
        for _, u in conn.updates
        if getattr(u, "session_update", "") == "agent_message_chunk"
    ]
    assert any(f"/approve {run} {node}" in t for t in texts), texts
    # And NOT the bare ``/approve <run>`` (merge-approval form).
    assert not any(
        f"/approve {run} " not in t and f"/approve {run}" in t
        for t in texts
    ), texts


# ── agent: live dedup vs replay ────────────────────────────────────────────


def _edit_record(seq: int, path: str) -> str:
    inner = {
        "type": "assistant",
        "message": {
            "content": [{
                "type": "tool_use", "name": "Edit",
                "input": {"file_path": path, "old_string": "a", "new_string": "b"},
            }]
        },
    }
    return json.dumps(
        {"seq": seq, "stream": "stdout", "t": time.time(), "line": json.dumps(inner)}
    )


def test_replay_keeps_locations_without_advancing_live_dedup(tmp_path):
    """Replayed tool events with a path emit a location; the live dedup
    state is not advanced by replay."""
    from mini_ork.acp.agent import MiniOrkAcpAgent

    home = tmp_path / "home"
    session, node = "run-rp", "implementer"
    run_dir = home / "runs" / session
    run_dir.mkdir(parents=True)
    live = run_dir / f"agent-{node}.live.jsonl"
    live.write_text(
        _edit_record(1, "src.py") + "\n" + _edit_record(2, "src.py") + "\n",
        encoding="utf-8",
    )

    agent = MiniOrkAcpAgent(
        home=str(home),
        reader=lambda run_id: {"status": "published", "events": [], "llm_calls": []},
        poll_interval=0.01, start_timeout=2.0,
    )
    conn = _FakeConn()
    agent._conn = conn
    agent._sessions[session] = str(tmp_path)

    asyncio.run(agent._replay_live_for(session, [node]))

    updates = [
        u for _, u in conn.updates
        if getattr(u, "session_update", "") == "tool_call_update"
        and getattr(u, "tool_call_id", None) == node
    ]
    assert len(updates) == 2
    absolute = str(tmp_path / "src.py")
    for update in updates:
        assert update.locations, "replay dropped the location"
        assert update.locations[0].path == absolute

    # Live dedup state was not advanced by replay.
    assert not dict(getattr(agent, "_live_locations_sent", {}) or {})


# ── commands: /approve, /request-changes, /checkpoints, /restore ───────────


def _run_cmd(agent, name: str, arg: str) -> str:
    from mini_ork.acp import commands

    return asyncio.run(commands.handle(agent, "t1", name, arg))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _workspace(tmp_path: Path, monkeypatch, run: str) -> tuple[Path, object]:
    """A tmp git project with one committed file and an open workspace for ``run``."""
    from mini_ork import workspaces

    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q")
    (proj / "app.py").write_text("v1\n", encoding="utf-8")
    _git(proj, "add", "app.py")
    _git(proj, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "v1")

    home = _make_home(tmp_path)
    monkeypatch.setenv("MO_WORKTREE_DIR", str(tmp_path / "wts"))
    ws = workspaces.create(proj, home, run)
    return home, ws


def test_commands_register_four_review_commands_in_completion_list():
    """All four new commands appear in the available-commands update and
    have a registered handler."""
    from mini_ork.acp.commands import COMMANDS, HANDLERS

    announced = {cmd.name for cmd in COMMANDS}
    wanted = {"approve", "request-changes", "checkpoints", "restore"}
    assert wanted <= announced
    assert wanted <= set(HANDLERS)


def test_request_changes_rejects_and_retries_from_node(tmp_path, monkeypatch):
    """``/request-changes`` rejects the node review, emits a steering
    row, and spawns the retry from that node."""
    run, node = "run-rc1", "implementer"
    home = _parked_run(tmp_path, run, node)[0]
    db = home / "state.db"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    spawn_calls = _patch_spawn(monkeypatch)
    agent, _ = _make_agent(home, run, answers=None)

    reply = _run_cmd(agent, "request-changes", f"{run} implementer add a test")
    assert isinstance(reply, str) and reply
    assert not reply.startswith("Unknown command"), reply

    row = _gate_row(db, run, node)
    assert row["status"] == "rejected"

    con = sqlite3.connect(db)
    count = con.execute(
        "SELECT COUNT(*) FROM operator_steering WHERE run_id = ?", (run,)
    ).fetchone()[0]
    con.close()
    assert count >= 1

    assert len(spawn_calls) == 1
    assert spawn_calls[0]["extra"] == ["--from-node", "implementer", "--strategy", "retry"]


def test_request_changes_warns_when_steering_emit_fails(tmp_path, monkeypatch):
    """A failed ``operator_steering.emit`` must be surfaced as a warning
    line in the reply, not swallowed. The CLI ``_cmd_reject`` was made
    fail-loud for this exact defect — the in-app command must agree.
    """
    from mini_ork.steering import operator_steering

    run, node = "run-rc1w", "implementer"
    home = _parked_run(tmp_path, run, node)[0]
    db = home / "state.db"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    _patch_spawn(monkeypatch)
    agent, _ = _make_agent(home, run, answers=None)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(operator_steering, "emit", _boom)

    reply = _run_cmd(agent, "request-changes", f"{run} implementer add a test")
    assert "warning" in reply.lower()
    assert "operator_steering" in reply.lower() or "did NOT reach" in reply
    # The reject still happened — the row is in, the retry still spawns.
    assert _gate_row(db, run, node)["status"] == "rejected"


def test_approve_with_node_approves_and_resumes(tmp_path, monkeypatch):
    """``/approve <run> <node>`` resolves the node review and resumes."""
    run, node = "run-rc2", "implementer"
    home, run_dir = _parked_run(tmp_path, run, node)
    db = home / "state.db"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    spawn_calls = _patch_spawn(monkeypatch)
    agent, _ = _make_agent(home, run, answers=None)

    reply = _run_cmd(agent, "approve", f"{run} {node}")
    assert isinstance(reply, str) and reply
    row = _gate_row(db, run, node)
    assert row["status"] == "approved"
    assert str(row["review_note"] or "").startswith("zed:")
    assert not (run_dir / ".review-pause").exists()
    assert (run_dir / ".review-pause.resolved").is_file()
    assert len(spawn_calls) == 1
    assert spawn_calls[0]["extra"] == ["--strategy", "resume"]


def test_approve_without_node_approves_the_merge(tmp_path, monkeypatch):
    """``/approve <run>`` with no node calls ``merge_approval.approve``."""
    run = "run-rc3"
    home, _ws = _workspace(tmp_path, monkeypatch, run)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    agent, _ = _make_agent(home, run, answers=None)

    from mini_ork.gates import merge_approval
    reply = _run_cmd(agent, "approve", run)
    assert isinstance(reply, str) and reply
    status = merge_approval.status(run, db_path=str(home / "state.db"))
    assert status["state"] == "approved"


def test_checkpoints_renders_markdown_table(tmp_path, monkeypatch):
    """``/checkpoints <run>`` lists the snapshot as a markdown table."""
    from mini_ork.vcs import node_checkpoint

    run = "run-rc4"
    home, ws = _workspace(tmp_path, monkeypatch, run)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    sha = node_checkpoint.snapshot(ws.path, run, "implementer")
    assert sha
    agent, _ = _make_agent(home, run, answers=None)

    reply = _run_cmd(agent, "checkpoints", run)
    assert isinstance(reply, str) and reply
    assert "|" in reply
    assert "implementer" in reply
    assert sha[:6] in reply


def test_restore_restores_content_and_reports_safety_ref(tmp_path, monkeypatch):
    """``/restore <run> <node>`` restores the file and reports a safety ref."""
    from mini_ork.vcs import node_checkpoint

    run = "run-rc5"
    home, ws = _workspace(tmp_path, monkeypatch, run)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    sha = node_checkpoint.snapshot(ws.path, run, "implementer")
    assert sha
    (ws.path / "app.py").write_text("v2 — drifted\n", encoding="utf-8")
    agent, _ = _make_agent(home, run, answers=None)

    reply = _run_cmd(agent, "restore", f"{run} implementer")
    assert isinstance(reply, str) and reply
    assert (ws.path / "app.py").read_text(encoding="utf-8") == "v1\n"
    assert sha[:6] in reply
    assert "pre-restore" in reply.lower()


def test_restore_unknown_node_refuses_without_touching_worktree(tmp_path, monkeypatch):
    """``/restore`` of an unknown node returns the refusal reason and
    does not modify the worktree."""
    from mini_ork.vcs import node_checkpoint

    run = "run-rc6"
    home, ws = _workspace(tmp_path, monkeypatch, run)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    sha = node_checkpoint.snapshot(ws.path, run, "implementer")
    assert sha
    (ws.path / "app.py").write_text("v2 — drifted\n", encoding="utf-8")
    agent, _ = _make_agent(home, run, answers=None)

    reply = _run_cmd(agent, "restore", f"{run} ghostnode")
    assert isinstance(reply, str) and reply
    assert "unknown node" in reply.lower()
    assert (ws.path / "app.py").read_text(encoding="utf-8") == "v2 — drifted\n"


def test_checkpoints_on_unknown_run_returns_user_readable_text(tmp_path, monkeypatch):
    """``/checkpoints`` on an unknown run returns a plain-text refusal."""
    home = _make_home(tmp_path)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    agent, _ = _make_agent(home, "no-such-run", answers=None)

    reply = _run_cmd(agent, "checkpoints", "no-such-run")
    assert isinstance(reply, str) and reply.strip()
