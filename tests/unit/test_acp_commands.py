"""Hermetic tests for the ACP slash-command table (``mini_ork.acp.commands``).

Every test runs against an in-memory agent constructed with a tmp dir as the
``.mini-ork`` home. A migrated ``state.db`` + seeded rows cover the data
shapes each handler reads. Subprocess / network seams (``_spawn``, ``run``,
``_probe``) are monkeypatched so no real ``bin/mini-ork``, ``curl``, or socket
ever runs. ``asyncio.run`` drives the coroutines; the module has no
pytest-asyncio dependency.

Contract:
  * each handler returns markdown; failures are one-line strings, never raises;
  * ``/runs`` caps at 50 and defaults to 10;
  * thread sessions with no run yet reply "No run in this thread yet ...";
  * the ``_current_run_id`` resolver honours an explicit run id over the
    thread's most-recent run.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from acp.schema import AvailableCommand  # noqa: E402

from mini_ork.acp import commands as cmds  # noqa: E402
from mini_ork.acp.agent import MiniOrkAcpAgent  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


@pytest.fixture(scope="module")
def _migrated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Migrate once per module (a full migration takes seconds); tests copy it."""
    db_path = tmp_path_factory.mktemp("template") / "state.db"
    rc, out, err = mig.init_db(db=str(db_path), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return db_path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _migrated_db: Path) -> Path:
    """A migrated ``.mini-ork`` home; seeded rows come via ``seed()``."""
    h = tmp_path / ".mini-ork"
    h.mkdir()
    (h / "runs-inbox").mkdir()
    shutil.copyfile(_migrated_db, h / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "50")
    monkeypatch.setenv("MO_SERVE_PORT", "7090")
    monkeypatch.setenv("MO_ACP_CERTIFY_TIMEOUT_S", "30")
    return h


def seed_run(
    home: Path,
    *,
    run_id: str,
    recipe: str = "code-fix",
    task_class: str = "framework_edit",
    status: str = "published",
    cost_usd: float = 0.25,
    age_seconds: int = 60,
    recipe_pretty: str = "code-fix",
) -> None:
    """Insert one task_runs row + a kickoff file so list_runs finds it."""
    kickoff_path = str(home / "runs-inbox" / f"{run_id}.md")
    (home / "runs-inbox" / f"{run_id}.md").write_text(
        f"# {recipe_pretty} run {run_id}\n", encoding="utf-8"
    )
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        """
        INSERT INTO task_runs
            (id, recipe, status, cost_usd, created_at, updated_at,
             task_class, kickoff_path, workflow_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            recipe,
            status,
            cost_usd,
            now - age_seconds,
            now - age_seconds,
            task_class,
            kickoff_path,
            "latest",
        ),
    )
    con.commit()
    con.close()


def seed_llm_call(home: Path, *, run_id: str, cost_usd: float = 0.5) -> None:
    """One llm_call the keep running spend gauge."""

    con = sqlite3.connect(home / "state.db")
    # llm_calls schema: id, provider, model, ts, total_tokens, prompt_tokens,
    # completion_tokens, cost_usd, task_run_id (column names vary by migration).
    cols = [r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()]
    if "task_run_id" not in cols:
        return
    con.execute(
        "INSERT INTO llm_calls (provider, model, ts, total_tokens, cost_usd, task_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("test", "test-model", time.strftime("%Y-%m-%dT%H:%M:%S"), 100, cost_usd, run_id),
    )
    con.commit()
    con.close()


def seed_gradient(home: Path, *, task_class: str, target: str, signal: str) -> None:
    """A gradient row the keep running ``/learnings`` failure-mode feed."""
    con = sqlite3.connect(home / "state.db")
    if "gradient_records" not in [
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    ]:
        return
    con.execute(
        "INSERT INTO gradient_records (gradient_id, task_class, target, signal, "
        "suggested_change, evidence, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"gr-{task_class}-{target}",
            task_class,
            target,
            signal,
            "no-op",
            json.dumps(["seed"]),
            0.95,
            int(time.time()),
        ),
    )
    con.commit()
    con.close()


def seed_learning_record(home: Path, *, run_id: str, title: str) -> None:
    """A learning_record row the keep running ``/learnings`` records feed."""
    con = sqlite3.connect(home / "state.db")
    if "learning_record" not in [
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    ]:
        return
    now = int(time.time())
    con.execute(
        "INSERT INTO learning_record (run_id, iter, rank, category, title, "
        "evidence_paths, arxiv_refs, outcome, severity, confidence, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            1,
            1,
            "meta",
            title,
            "[]",
            "[]",
            "open",
            "medium",
            0.5,
            now,
            now,
        ),
    )
    con.commit()
    con.close()


def _agent(home: Path) -> MiniOrkAcpAgent:
    """A bare agent bound to the tmp home so ``agent._home_for`` resolves."""
    a = MiniOrkAcpAgent(home=str(home))
    # Bind a fake session id whose cwd parent resolves to ``home``.
    a._sessions["run-1-abc"] = str(home.parent)
    return a


# ── /help ────────────────────────────────────────────────────────────────────


def test_help_lists_every_announced_command():
    agent = _agent(Path.cwd())  # home only needs to resolve
    out = asyncio.run(cmds.handle_help(agent, "run-1-abc", ""))
    assert "Available slash commands" in out
    for name in ("help", "runs", "status", "learnings", "cost", "lanes",
                 "recipes", "recipe",
                 "stop", "kill", "resume", "recover", "certify", "serve",
                 "workspaces", "merge", "discard",
                 "automations", "automation", "race"):
        assert f"`/{name}`" in out


# ── COMMANDS table ───────────────────────────────────────────────────────────


def test_commands_table_matches_handlers():
    """Every handler key (except ``run``) is announced OR routed via a
    longer-prefix key that IS announced.

    S6b-1's compound handlers (``automation run``, ``automation scheduler``,
    …) are reached through the longest-match dispatcher off the bare
    ``automation`` key — they are not separate UI announcements. So the
    invariant is: every HANDLERS key either appears in COMMANDS or starts
    with the name of one that does.
    """
    announced = {c.name for c in cmds.COMMANDS}
    announced_prefixes = {a for a in announced}  # same set, clearer name
    reachable = set(announced)
    for key in cmds.HANDLERS:
        if key in reachable:
            continue
        # Is there an announced key whose name is a prefix of this key?
        if any(key == a or key.startswith(a + " ") for a in announced_prefixes):
            reachable.add(key)
        elif key == "run":  # ``/run`` is announced but not dispatched here
            reachable.add(key)
    missing = set(cmds.HANDLERS) - reachable
    assert not missing, f"unannounced handler keys: {sorted(missing)}"
    # Each command carries the right SDK shape.
    for c in cmds.COMMANDS:
        assert isinstance(c, AvailableCommand)
        assert c.name and c.description


# ── /runs ────────────────────────────────────────────────────────────────────


def test_runs_returns_markdown_table(home: Path):
    seed_run(home, run_id="run-aaa-001", recipe="code-fix", recipe_pretty="Fix bug")
    seed_run(home, run_id="run-aaa-002", recipe="framework-edit", recipe_pretty="Edit scope")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_runs(agent, "run-1-abc", ""))
    # Tabs line leads with the active filter bolded (All, since no arg).
    assert "**All**" in out
    for label in ("Working", "Needs you", "Done", "Failed"):
        assert label in out
    # At least one seeded run shows up in the table.
    assert "run-aaa-001" in out or "run-aaa-002" in out
    # Filter hint footer.
    assert "details: `/status <run id>`" in out


def test_runs_caps_count(home: Path):
    """/runs N — N clamped to [1, 50], default 20."""
    for i in range(20):
        seed_run(home, run_id=f"run-many-{i:03d}")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_runs(agent, "run-1-abc", "5"))
    # Only 5 task_run rows requested → 5 body rows in the table. Body rows
    # begin with the gutter cell "| <mark> <title> `run-id` |" — match the
    # backtick-quoted id pattern the kickoff mandates.
    body_rows = [
        ln for ln in out.splitlines()
        if ln.startswith("|") and "`run-many-" in ln
    ]
    assert len(body_rows) == 5


def test_runs_rejects_invalid_count(home: Path):
    seed_run(home, run_id="run-bad-001")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_runs(agent, "run-1-abc", "not-a-number"))
    # Falls back to default 20; the row is shown.
    assert "run-bad-001" in out


def test_runs_empty(home: Path):
    agent = _agent(home)
    out = asyncio.run(cmds.handle_runs(agent, "run-1-abc", ""))
    assert "No runs match." in out


def test_runs_state_filter(home: Path):
    """/runs <state> — only matching rows; tabs line bolds the active filter."""
    seed_run(home, run_id="run-done-001", status="published")
    seed_run(home, run_id="run-exec-001", status="executing")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_runs(agent, "run-1-abc", "done"))
    assert "run-done-001" in out
    assert "run-exec-001" not in out


def test_runs_recipe_filter(home: Path):
    """/runs recipe:<id> — exact recipe id match."""
    seed_run(home, run_id="run-cf-001", recipe="code-fix")
    seed_run(home, run_id="run-fe-001", recipe="framework-edit")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_runs(agent, "run-1-abc", "recipe:code-fix"))
    assert "run-cf-001" in out
    assert "run-fe-001" not in out


# ── /status ──────────────────────────────────────────────────────────────────


def test_status_run_session(home: Path):
    seed_run(home, run_id="run-st-001", recipe="code-fix")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_status(agent, "run-st-001", ""))
    assert "run-st-001" in out
    assert "code-fix" in out


def test_status_thread_with_run(home: Path):
    seed_run(home, run_id="run-thr-001", recipe="framework-edit")
    agent = _agent(home)
    agent._thread_sessions.add("orch-test-001")
    agent._sessions["orch-test-001"] = str(home.parent)
    agent._thread_runs["orch-test-001"] = ["run-thr-001"]
    out = asyncio.run(cmds.handle_status(agent, "orch-test-001", ""))
    assert "run-thr-001" in out


def test_status_thread_without_run():
    agent = _agent(Path.cwd())
    agent._thread_sessions.add("orch-empty-001")
    agent._sessions["orch-empty-001"] = str(Path.cwd())
    out = asyncio.run(cmds.handle_status(agent, "orch-empty-001", ""))
    assert "No run in this thread yet" in out


def test_status_accepts_explicit_run_id(home: Path):
    seed_run(home, run_id="run-aaa-001")
    seed_run(home, run_id="run-aaa-002")
    agent = _agent(home)
    agent._thread_sessions.add("orch-test-001")
    agent._sessions["orch-test-001"] = str(home.parent)
    agent._thread_runs["orch-test-001"] = ["run-aaa-001"]
    out = asyncio.run(cmds.handle_status(agent, "orch-test-001", "run-aaa-002"))
    # Explicit run id wins — the named run appears in the reply.
    assert "run-aaa-002" in out
    # The thread's most-recent (run-aaa-001) is NOT reported because the
    # explicit id won.
    assert "run-aaa-001" not in out


# ── /learnings ───────────────────────────────────────────────────────────────


def test_learnings_sections_present(home: Path):
    seed_run(home, run_id="run-lr-001", task_class="framework_edit")
    seed_gradient(home, task_class="framework_edit", target="dispatch", signal="simulate")
    seed_learning_record(home, run_id="run-lr-001", title="remember to plan")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_learnings(agent, "run-lr-001", ""))
    assert "Failure-mode gradients" in out
    assert "Learning records" in out
    assert "Emergent patterns" in out


def test_learnings_filter_caps_results(home: Path):
    seed_run(home, run_id="run-lr-002", task_class="framework_edit")
    seed_gradient(home, task_class="framework_edit", target="dispatch",
                  signal="a really specific signal the filter should pick up")
    seed_gradient(home, task_class="framework_edit", target="other",
                  signal="completely unrelated payload the filter ignores")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_learnings(agent, "run-lr-002", "specific"))
    assert "specific" in out.lower()
    # The non-matching gradient is filtered out.
    assert "completely unrelated payload" not in out


def test_learnings_empty_sections_say_so(home: Path):
    seed_run(home, run_id="run-lr-003")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_learnings(agent, "run-lr-003", ""))
    assert "none recorded yet" in out


# ── /cost ────────────────────────────────────────────────────────────────────


def test_cost_shows_window_and_budget(home: Path):
    seed_run(home, run_id="run-cost-001", cost_usd=1.50)
    seed_llm_call(home, run_id="run-cost-001", cost_usd=0.5)
    agent = _agent(home)
    out = asyncio.run(cmds.handle_cost(agent, "run-cost-001", "1"))
    assert "Cost (last 1 day)" in out
    assert "rolling 24h" in out
    assert "$50.00" in out


def test_cost_days_default_and_caps(home: Path):
    seed_run(home, run_id="run-cost-002", cost_usd=0.0)
    agent = _agent(home)
    out = asyncio.run(cmds.handle_cost(agent, "run-cost-002", ""))
    # Default 1 day.
    assert "Cost (last 1 day)" in out
    # Garbage arg falls back to 1 day.
    out2 = asyncio.run(cmds.handle_cost(agent, "run-cost-002", "garbage"))
    assert "Cost (last 1 day)" in out2


# ── /lanes ───────────────────────────────────────────────────────────────────


def test_lanes_returns_role_table(home: Path):
    agent = _agent(home)
    out = asyncio.run(cmds.handle_lanes(agent, "run-1-abc", ""))
    # Either a populated table or the empty marker — both are valid.
    assert out.startswith("## Lanes") or "No lanes configured" in out


# ── /stop / /kill / /resume ──────────────────────────────────────────────────


def test_stop_calls_control_stop_run(home: Path, monkeypatch: pytest.MonkeyPatch):
    seed_run(home, run_id="run-stop-001", status="executing")
    captured: dict[str, object] = {}

    def fake_stop(home, db, task_run_id):
        captured["home"] = home
        captured["task_run_id"] = task_run_id
        return {"ok": True, "task_run_id": task_run_id, "note": "soft"}

    monkeypatch.setattr("mini_ork.web.control.stop_run", fake_stop)
    agent = _agent(home)
    out = asyncio.run(cmds.handle_stop(agent, "run-stop-001", ""))
    assert captured.get("task_run_id") == "run-stop-001"
    assert "ok" in out


def test_kill_calls_control_kill_run(home: Path, monkeypatch: pytest.MonkeyPatch):
    seed_run(home, run_id="run-kill-001", status="executing")
    captured: dict[str, object] = {}

    def fake_kill(home, db, task_run_id):
        captured["task_run_id"] = task_run_id
        return {"ok": True, "task_run_id": task_run_id}

    monkeypatch.setattr("mini_ork.web.control.kill_run", fake_kill)
    agent = _agent(home)
    out = asyncio.run(cmds.handle_kill(agent, "run-kill-001", ""))
    assert captured.get("task_run_id") == "run-kill-001"
    assert "ok" in out


def test_resume_reports_no_pause_when_ok_false(home: Path, monkeypatch: pytest.MonkeyPatch):
    seed_run(home, run_id="run-res-001")
    monkeypatch.setattr(
        "mini_ork.web.control.resume_cost_run",
        lambda home, run_id, approver: {"ok": False, "error": "no sentinel"},
    )
    agent = _agent(home)
    out = asyncio.run(cmds.handle_resume(agent, "run-res-001", ""))
    assert "no cost pause" in out


def test_stop_in_thread_picks_thread_run(home: Path, monkeypatch: pytest.MonkeyPatch):
    seed_run(home, run_id="run-thr-stop", status="executing")
    captured: list[str] = []

    def fake_stop(home, db, task_run_id):
        captured.append(task_run_id)
        return {"ok": True, "task_run_id": task_run_id}

    monkeypatch.setattr("mini_ork.web.control.stop_run", fake_stop)
    agent = _agent(home)
    agent._thread_sessions.add("orch-stop")
    agent._sessions["orch-stop"] = str(home.parent)
    agent._thread_runs["orch-stop"] = ["run-thr-stop"]
    asyncio.run(cmds.handle_stop(agent, "orch-stop", ""))
    assert captured == ["run-thr-stop"]


# ── /recover ────────────────────────────────────────────────────────────────


def test_recover_spawns_detached_subprocess(home: Path, monkeypatch: pytest.MonkeyPatch):
    seed_run(home, run_id="run-rec-001")
    captured: dict[str, object] = {}

    class FakePopen:
        pid = 4242

        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(cmds, "_spawn", lambda *a, **kw: FakePopen(*a, **kw))
    agent = _agent(home)
    out = asyncio.run(cmds.handle_recover(agent, "run-rec-001", ""))
    assert "started" in out
    assert "4242" in out  # pid
    # The ``recover`` argv includes the run id; cwd is the engine root; the
    # MINI_ORK_VENV_ACTIVE marker is dropped (the spawn env copy).
    spawn_args = captured["args"]
    assert "recover" in spawn_args[0]
    assert "run-rec-001" in spawn_args[0]
    env = captured["kwargs"]["env"]
    assert "MINI_ORK_VENV_ACTIVE" not in env


def test_recover_parses_from_node(home: Path, monkeypatch: pytest.MonkeyPatch):
    seed_run(home, run_id="run-rec-002")
    captured: dict[str, object] = {}

    class FakePopen:
        pid = 99

        def __init__(self, *args, **kwargs):
            captured["args"] = args

    monkeypatch.setattr(cmds, "_spawn", lambda *a, **kw: FakePopen(*a, **kw))
    agent = _agent(home)
    asyncio.run(cmds.handle_recover(agent, "run-rec-002", "--from-node planner"))
    assert "--from-node" in captured["args"][0]
    assert "planner" in captured["args"][0]


def test_recover_thread_no_run_returns_placeholder(home: Path):
    agent = _agent(home)
    agent._thread_sessions.add("orch-empty-rec")
    agent._sessions["orch-empty-rec"] = str(home.parent)
    out = asyncio.run(cmds.handle_recover(agent, "orch-empty-rec", ""))
    assert "No run" in out


# ── /certify ─────────────────────────────────────────────────────────────────


def test_certify_missing_text_shows_usage():
    agent = _agent(Path.cwd())
    out = asyncio.run(cmds.handle_certify(agent, "run-1", ""))
    assert "Usage" in out


def test_certify_proven_verdict(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        cmds,
        "_run",
        lambda argv, *, timeout: subprocess.CompletedProcess(
            argv, 0, stdout="ok\nPROVEN\n", stderr=""
        ),
    )
    agent = _agent(home)
    out = asyncio.run(cmds.handle_certify(agent, "run-1", "bug X"))
    assert "PROVEN" in out


def test_certify_refuted_verdict(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        cmds,
        "_run",
        lambda argv, *, timeout: subprocess.CompletedProcess(
            argv, 1, stdout="bad\nREFUTED\n", stderr=""
        ),
    )
    agent = _agent(home)
    out = asyncio.run(cmds.handle_certify(agent, "run-1", "bug X"))
    assert "REFUTED" in out


def test_certify_unverified_verdict(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        cmds,
        "_run",
        lambda argv, *, timeout: subprocess.CompletedProcess(
            argv, 2, stdout="UNVERIFIED", stderr=""
        ),
    )
    agent = _agent(home)
    out = asyncio.run(cmds.handle_certify(agent, "run-1", "bug X"))
    assert "UNVERIFIED" in out


def test_certify_other_rc_reports_error(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        cmds,
        "_run",
        lambda argv, *, timeout: subprocess.CompletedProcess(
            argv, 7, stdout="partial", stderr="traceback\noh no\n"
        ),
    )
    agent = _agent(home)
    out = asyncio.run(cmds.handle_certify(agent, "run-1", "bug X"))
    assert "errored" in out
    assert "oh no" in out


def test_certify_timeout(home: Path, monkeypatch: pytest.MonkeyPatch):
    """The handler survives a timeout: rc=124 sentinel + stderr tail."""
    def fake_run(argv, *, timeout):
        return subprocess.CompletedProcess(
            argv, 124, stdout="", stderr="timeout after 30s"
        )

    monkeypatch.setattr(cmds, "_run", fake_run)
    agent = _agent(home)
    out = asyncio.run(cmds.handle_certify(agent, "run-1", "bug X"))
    assert "errored" in out or "timeout" in out.lower()


# ── /serve ───────────────────────────────────────────────────────────────────


def test_serve_down_returns_command():
    agent = _agent(Path.cwd())
    # Default: probe fails (no serve actually running).
    out = asyncio.run(cmds.handle_serve(agent, "run-1", ""))
    assert "Run `mini-ork serve`" in out


def test_serve_up_returns_url(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cmds, "_probe", lambda url, *, timeout=1.0: True)
    seed_run(home, run_id="run-srv-001")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_serve(agent, "run-srv-001", ""))
    assert "mini-ork serve is up" in out
    assert "/runs/run-srv-001" in out


# ── handler error contract ───────────────────────────────────────────────────


def test_handler_error_returns_one_line(home: Path, monkeypatch: pytest.MonkeyPatch):
    """A handler that raises must NOT propagate — the dispatcher swallows it."""
    async def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(cmds.HANDLERS, "lanes", boom)
    agent = _agent(home)
    out = asyncio.run(cmds.handle(agent, "run-1", "lanes", ""))
    # ``cmds.handle`` widens to ``str | CommandReply``; the error path here
    # always returns ``str`` so we coerce for the assertion.
    text = out.text if isinstance(out, cmds.CommandReply) else out
    assert "kaboom" in text
    assert "`/lanes` failed" in text


def test_unknown_command_via_handle_returns_one_line():
    """``cmds.handle`` is only called for known commands by the dispatcher;
    direct calls with an unknown name still degrade gracefully."""
    agent = _agent(Path.cwd())
    out = asyncio.run(cmds.handle(agent, "run-1", "nope", ""))
    text = out.text if isinstance(out, cmds.CommandReply) else out
    assert "Unknown command" in text or "/nope" in text

# ── /recipes / /recipe (Zed S3a) ────────────────────────────────────────────


def test_recipes_table_lists_engine_and_project(
    monkeypatch, tmp_path, home: Path
):
    """``/recipes`` returns a Project/Engine summary line plus a markdown
    table that names every visible recipe."""
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "recipes").mkdir()
    (engine / "recipes" / "alpha").mkdir()
    (engine / "recipes" / "alpha" / "workflow.yaml").write_text(
        "name: alpha\n", encoding="utf-8"
    )
    (engine / "recipes" / "alpha" / "task_class.yaml").write_text(
        "name: alpha\ndescription: Engine alpha\n", encoding="utf-8"
    )
    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: engine)
    # A project recipe under the home.
    (home / "recipes").mkdir(parents=True, exist_ok=True)
    (home / "recipes" / "docs").mkdir()
    (home / "recipes" / "docs" / "workflow.yaml").write_text(
        "name: docs\n", encoding="utf-8"
    )
    (home / "recipes" / "docs" / "task_class.yaml").write_text(
        "name: docs\ndescription: Project docs\n", encoding="utf-8"
    )

    agent = _agent(home)
    out = asyncio.run(cmds.handle_recipes(agent, "run-1-abc", ""))
    assert "Project 1 · Engine 1" in out
    assert "`alpha` |" in out
    assert "`docs` |" in out
    # Filter hint footer.
    assert "Filter: `/recipes project`" in out


def test_recipes_filter_project(monkeypatch, tmp_path, home: Path):
    """``/recipes project`` returns only the project entries."""
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "recipes" / "alpha").mkdir(parents=True)
    (engine / "recipes" / "alpha" / "workflow.yaml").write_text(
        "name: alpha\n", encoding="utf-8"
    )
    (engine / "recipes" / "alpha" / "task_class.yaml").write_text(
        "name: alpha\ndescription: Engine\n", encoding="utf-8"
    )
    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: engine)
    (home / "recipes").mkdir(parents=True, exist_ok=True)
    (home / "recipes" / "docs").mkdir()
    (home / "recipes" / "docs" / "workflow.yaml").write_text(
        "name: docs\n", encoding="utf-8"
    )
    (home / "recipes" / "docs" / "task_class.yaml").write_text(
        "name: docs\ndescription: Project\n", encoding="utf-8"
    )

    agent = _agent(home)
    out = asyncio.run(cmds.handle_recipes(agent, "run-1-abc", "project"))
    assert "`docs` |" in out
    assert "`alpha`" not in out


def test_recipes_empty_returns_no_match(home: Path, monkeypatch: pytest.MonkeyPatch):
    """No recipes in either source → ``No recipes match.``"""
    empty = home / "empty-engine"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: empty)
    agent = _agent(home)
    out = asyncio.run(cmds.handle_recipes(agent, "run-1-abc", ""))
    assert "No recipes match." in out


def test_recipe_returns_command_reply_with_links(monkeypatch, tmp_path, home: Path):
    """``/recipe <id>`` returns a ``CommandReply`` carrying the recipe
    markdown + the file paths that should be emitted as resource links."""
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "recipes" / "docs").mkdir(parents=True)
    (engine / "recipes" / "docs" / "workflow.yaml").write_text(
        "name: docs\n", encoding="utf-8"
    )
    (engine / "recipes" / "docs" / "task_class.yaml").write_text(
        "name: docs\ndescription: Project docs\n", encoding="utf-8"
    )
    (engine / "recipes" / "docs" / "artifact_contract.yaml").write_text(
        "expected_artifact: diff\n", encoding="utf-8"
    )
    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: engine)

    agent = _agent(home)
    reply = asyncio.run(cmds.handle_recipe(agent, "run-1-abc", "docs"))
    assert isinstance(reply, cmds.CommandReply)
    assert "**docs**" in reply.text
    assert reply.links, "recipe card must carry at least one file link"
    assert any(p.name == "workflow.yaml" for p in reply.links)


def test_recipe_unknown_id_returns_one_line(home: Path):
    """An unknown id → plain ``str`` reply with the kickoff's wording."""
    agent = _agent(home)
    out = asyncio.run(cmds.handle_recipe(agent, "run-1-abc", "does-not-exist"))
    # Either a bare string or a ``CommandReply`` whose text matches; the
    # contract is "No recipe <id>" either way.
    text = out.text if isinstance(out, cmds.CommandReply) else out
    assert "No recipe does-not-exist" in text
    if isinstance(out, cmds.CommandReply):
        assert out.links == []


def test_recipe_missing_arg_returns_usage(home: Path):
    """``/recipe`` alone → usage hint."""
    agent = _agent(home)
    out = asyncio.run(cmds.handle_recipe(agent, "run-1-abc", ""))
    text = out.text if isinstance(out, cmds.CommandReply) else out
    assert "Usage" in text


# ── review regressions ───────────────────────────────────────────────────────


def _seed_events(home: Path, run_id: str, events: list[tuple[str, str]]) -> None:
    con = sqlite3.connect(home / "state.db")
    for i, (kind, node) in enumerate(events):
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"{run_id}-{node}-{kind}", run_id, kind, json.dumps({"node_id": node}), 1000 + i),
        )
    con.commit()
    con.close()


def test_status_counts_a_finished_node_as_done_not_running(home: Path):
    seed_run(home, run_id="run-nodes-001", status="executing")
    _seed_events(home, "run-nodes-001", [("node_start", "n1"), ("node_end", "n1"), ("node_start", "n2")])
    out = asyncio.run(cmds.handle_status(_agent(home), "run-nodes-001", ""))
    # New shape: steps table with one row per node. ``n1`` has a matching
    # node_end (``done``); ``n2`` is the running node (no node_end yet).
    assert "| `n1` |" in out
    assert "| `n2` |" in out
    # The result column carries "done" for the finished node and
    # "running" for the still-open one.
    n1_done = [ln for ln in out.splitlines() if ln.startswith("| `n1` |") and "done" in ln]
    n2_running = [ln for ln in out.splitlines() if ln.startswith("| `n2` |") and "running" in ln]
    assert n1_done, f"expected done row for n1, got:\n{out}"
    assert n2_running, f"expected running row for n2, got:\n{out}"


def test_cost_window_is_days_not_rows(home: Path):
    seed_run(home, run_id="run-today-a", recipe="docs", cost_usd=1.0)
    seed_run(home, run_id="run-today-b", recipe="code-fix", cost_usd=2.0)
    seed_run(home, run_id="run-old-001", recipe="docs", cost_usd=4.0, age_seconds=5 * 86400)
    agent = _agent(home)
    one_day = asyncio.run(cmds.handle_cost(agent, "run-today-a", "1"))
    assert "**total**: $3.00" in one_day          # both recipes of today, not the old day
    week = asyncio.run(cmds.handle_cost(agent, "run-today-a", "7"))
    assert "**total**: $7.00" in week


def test_certify_does_not_block_the_agent_and_announces_itself(home: Path, monkeypatch):
    """While certify runs, the agent's event loop must keep serving other work.
    The fake certify waits for a signal only a running loop can send; a call
    that blocks the loop never gets it (deterministic, no timing thresholds)."""
    import threading

    loop_ran = threading.Event()

    def certify_waiting_for_the_loop(argv, *, timeout):
        verdict = "PROVEN" if loop_ran.wait(5) else "LOOP-BLOCKED"
        return subprocess.CompletedProcess(argv, 0, stdout=f"{verdict}: fix holds\n", stderr="")

    monkeypatch.setattr(cmds, "_run", certify_waiting_for_the_loop)
    agent = _agent(home)
    agent._sessions["run-cert-001"] = str(home.parent)
    sent: list[str] = []

    async def capture(session_id, update):
        sent.append(getattr(getattr(update, "content", None), "text", ""))

    monkeypatch.setattr(agent, "_emit", capture)

    async def other_work():
        await asyncio.sleep(0.01)
        loop_ran.set()

    async def scenario():
        result, _ = await asyncio.gather(
            cmds.handle_certify(agent, "run-cert-001", "the parser drops tabs"), other_work())
        return result

    out = asyncio.run(scenario())
    assert "LOOP-BLOCKED" not in out and "PROVEN" in out
    assert sent and "Certifying HEAD~1..HEAD" in sent[0]



# ── /recipe new + /recipe edit (S3b-2) ───────────────────────────────────────


def test_recipe_new_returns_rewrite_sentinel_with_intent(home):
    """/recipe new always returns a _RewriteToOrchestrate sentinel."""
    from mini_ork.acp.commands import _RewriteToOrchestrate

    agent = _agent(home)
    out = asyncio.run(cmds.handle_recipe_new(agent, "orch-1", "audit SQL migrations"))
    assert isinstance(out, _RewriteToOrchestrate)
    assert out.recipe_id is None
    # Intent text mentions draft_recipe + the user's intent.
    assert "audit SQL migrations" in out.intent_text
    assert "draft_recipe" in out.intent_text


def test_recipe_new_empty_arg_uses_default_intent(home):
    """/recipe new with no arg → default intent (still a rewrite)."""
    from mini_ork.acp.commands import _RewriteToOrchestrate

    agent = _agent(home)
    out = asyncio.run(cmds.handle_recipe_new(agent, "orch-1", ""))
    assert isinstance(out, _RewriteToOrchestrate)
    assert out.recipe_id is None
    # Default intent calls out the recipe_author MCP tools.
    assert "draft_recipe" in out.intent_text






def test_recipe_edit_unknown_id_returns_one_line(home):
    """/recipe edit <bogus> → plain string (NOT a sentinel)."""
    agent = _agent(home)
    out = asyncio.run(cmds.handle_recipe_edit(agent, "orch-1", "does-not-exist"))
    assert isinstance(out, str)
    assert "does-not-exist" in out
    assert "No recipe" in out


def test_recipe_edit_empty_arg_returns_usage(home):
    """/recipe edit with no id → usage hint string."""
    agent = _agent(home)
    out = asyncio.run(cmds.handle_recipe_edit(agent, "orch-1", ""))
    assert isinstance(out, str)
    assert "Usage" in out
    assert "recipe edit" in out


def test_recipe_new_and_edit_are_announced(home):
    """The COMMANDS table includes recipe new and recipe edit."""
    names = {c.name for c in cmds.COMMANDS}
    assert "recipe new" in names
    assert "recipe edit" in names
    assert "recipe new" in cmds.HANDLERS
    assert "recipe edit" in cmds.HANDLERS


# ── /workspaces / /merge / /discard (Zed S5) ──────────────────────────────────


def _make_workspace(tmp_path, home, *, run_id: str, commit_in_worktree: bool = True):
    """Create a temp project at ``tmp_path/proj`` + workspace under ``home``.

    ``home`` is the test fixture's ``.mini-ork`` (the same home the agent
    resolves via ``_home_for(session_id)``). Real git; mirrors
    ``tests/unit/test_workspaces.py``. Returns ``(project, home, ws)``.
    """
    import subprocess
    from mini_ork import workspaces as ws_mod

    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=project, check=True,
        capture_output=True, text=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                    cwd=project, check=True, capture_output=True, text=True)
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True,
                    capture_output=True, text=True)
    ws = ws_mod.create(project, home, run_id)
    if commit_in_worktree:
        (ws.path / "new.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "new.txt"], cwd=ws.path, check=True,
                        capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "feat"], cwd=ws.path, check=True,
                        capture_output=True, text=True)
    return project, home, ws


def test_workspaces_empty_home(home):
    """No workspaces on disk → ``No open task workspaces.``"""
    agent = _agent(home)
    out = asyncio.run(cmds.handle_workspaces(agent, "run-1-abc", ""))
    assert out == "No open task workspaces."


def test_workspaces_lists_one_row(tmp_path, home):
    """One workspace with one new file → one body row + footer hint."""
    _make_workspace(tmp_path, home, run_id="run-ws-001")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_workspaces(agent, "run-1-abc", ""))
    assert "Open task workspaces" in out
    assert "`run-ws-001`" in out
    assert "mini-ork/run-ws-001" in out
    assert "/merge <run>" in out and "/discard <run>" in out


def test_merge_resolves_explicit_run_id_and_merges(tmp_path, home):
    """``/merge run-ws-001`` → ``Merged into main (fast-forward <sha>).``"""
    _, _, ws = _make_workspace(tmp_path, home, run_id="run-mg-001")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_merge(agent, "run-mg-001", "run-mg-001"))
    assert "Merged into main" in out
    assert "fast-forward" in out
    # Workspace is gone after merge.
    from mini_ork import workspaces as ws_mod
    assert ws_mod.load(home, "run-mg-001") is None


def test_merge_thread_run_picks_thread_run_id(tmp_path, home):
    """In a thread session, ``/merge`` with no arg resolves to the thread's
    latest followed run."""
    _, _, _ = _make_workspace(tmp_path, home, run_id="run-mg-thread")
    agent = _agent(home)
    agent._thread_sessions.add("orch-mg")
    agent._sessions["orch-mg"] = str(home.parent)
    agent._thread_runs["orch-mg"] = ["run-mg-thread"]
    out = asyncio.run(cmds.handle_merge(agent, "orch-mg", ""))
    assert "Merged into main" in out


def test_merge_no_workspace_message(tmp_path, home):
    """A run id with no workspace record → ``Run <id> has no open workspace.``"""
    agent = _agent(home)
    out = asyncio.run(cmds.handle_merge(agent, "run-nope-001", "run-nope-001"))
    assert "no open workspace" in out


def test_merge_thread_no_run_message(tmp_path, home):
    """A thread with no runs yet → placeholder."""
    agent = _agent(home)
    agent._thread_sessions.add("orch-mg-empty")
    agent._sessions["orch-mg-empty"] = str(home.parent)
    out = asyncio.run(cmds.handle_merge(agent, "orch-mg-empty", ""))
    assert "No run" in out


def test_discard_resolves_explicit_run_id_and_removes(tmp_path, home):
    """``/discard run-d-001`` → ``Discarded <id> — its worktree and branch are gone.``"""
    _, _, _ = _make_workspace(tmp_path, home, run_id="run-d-001")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_discard(agent, "run-d-001", "run-d-001"))
    assert "Discarded run-d-001" in out
    assert "worktree and branch are gone" in out
    from mini_ork import workspaces as ws_mod
    assert ws_mod.load(home, "run-d-001") is None


def test_discard_thread_run_picks_thread_run_id(tmp_path, home):
    _, _, _ = _make_workspace(tmp_path, home, run_id="run-d-thread")
    agent = _agent(home)
    agent._thread_sessions.add("orch-d")
    agent._sessions["orch-d"] = str(home.parent)
    agent._thread_runs["orch-d"] = ["run-d-thread"]
    out = asyncio.run(cmds.handle_discard(agent, "orch-d", ""))
    assert "Discarded run-d-thread" in out


def test_discard_no_workspace_message(tmp_path, home):
    agent = _agent(home)
    out = asyncio.run(cmds.handle_discard(agent, "run-nope-002", "run-nope-002"))
    assert "no open workspace" in out


# ── /automations / /automation … (Zed S6b-1) ────────────────────────────────


def _automation_home(home: Path) -> Path:
    """Seed one automation (cron fires every weekday at 09:00) for the
    command-table tests. ``home`` already has ``state.db`` from the
    fixture, so /runs lookups don't crash."""
    from mini_ork import automations as _auto

    _auto.add(
        home, id="weekday", name="Weekday build",
        recipe="code-fix", kickoff="# k",
        schedule="0 9 * * 1-5", workspace="worktree",
    )
    return home


def test_automations_table_lists_one_row(home, monkeypatch):
    """``/automations`` renders a markdown row per automation."""
    _automation_home(home)
    monkeypatch_scheduler(monkeypatch, home, installed=False)
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automations(agent, "run-1-abc", ""))
    assert "| automation |" in out
    assert "`weekday`" in out
    assert "every weekday at 09:00" in out


def test_automations_empty_returns_kickoff_copy(home):
    """No automations → the kickoff's onboarding sentence."""
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automations(agent, "run-1-abc", ""))
    assert "No automations yet" in out


def test_automation_card_for_known_id(home, monkeypatch):
    """``/automation <id>`` → card with heading + metadata line."""
    _automation_home(home)
    monkeypatch_scheduler(monkeypatch, home, installed=True, last_tick="2026-01-01T09:00:00")
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation(agent, "run-1-abc", "weekday"))
    assert "### " in out
    assert "`weekday`" in out
    assert "Next:" in out
    assert "/automation run weekday" in out


def test_automation_unknown_id_message(home):
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation(agent, "run-1-abc", "ghost"))
    assert "No automation ghost" in out
    assert "/automations" in out


def test_automation_empty_arg_falls_back_to_table(home, monkeypatch):
    _automation_home(home)
    monkeypatch_scheduler(monkeypatch, home, installed=False)
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation(agent, "run-1-abc", ""))
    assert "| automation |" in out


def test_automation_run_calls_fire(home, monkeypatch):
    """``/automation run <id>`` → ``fire`` → "Started …" line."""
    _automation_home(home)
    monkeypatch.setattr(
        "mini_ork.automations.fire",
        lambda _h, _id: {"ok": True, "run_id": "run-new-001",
                         "workspace": "worktree"},
    )
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation_run(agent, "run-1-abc", "weekday"))
    assert "Started run-new-001" in out
    assert "in worktree `mini-ork/run-new-001`" in out


def test_automation_run_missing_id_message(home):
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation_run(agent, "run-1-abc", ""))
    assert "Which automation?" in out


def test_automation_run_error_message(home, monkeypatch):
    """fire returning ok=False surfaces the error string verbatim."""
    _automation_home(home)
    monkeypatch.setattr(
        "mini_ork.automations.fire",
        lambda _h, _id: {"ok": False, "error": "nope"},
    )
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation_run(agent, "run-1-abc", "weekday"))
    assert "Could not start weekday" in out
    assert "nope" in out


def test_automation_pause_and_resume(home, monkeypatch):
    """``pause`` / ``resume`` messages include the name + id."""
    _automation_home(home)
    monkeypatch.setattr(
        "mini_ork.automations.pause",
        lambda _h, _id: {"ok": True, "automation": {"name": "Weekday build"}},
    )
    monkeypatch.setattr(
        "mini_ork.automations.resume",
        lambda _h, _id: {"ok": True, "automation": {"name": "Weekday build",
                                                   "schedule": "0 9 * * 1-5"}},
    )
    agent = _agent(home)
    out_pause = asyncio.run(cmds.handle_automation_pause(
        agent, "run-1-abc", "weekday"))
    assert "Paused Weekday build" in out_pause
    out_resume = asyncio.run(cmds.handle_automation_resume(
        agent, "run-1-abc", "weekday"))
    assert "Resumed Weekday build" in out_resume
    assert "09:00" in out_resume and "T09:00" not in out_resume  # the table's time format, not ISO


def test_automation_delete_returns_confirmation(home, monkeypatch):
    """``/automation delete <id>`` removes and confirms."""
    _automation_home(home)
    monkeypatch.setattr(
        "mini_ork.automations.remove",
        lambda _h, _id: {"ok": True, "removed": _id},
    )
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation_delete(
        agent, "run-1-abc", "weekday"))
    assert "Deleted Weekday build" in out
    assert "/runs" in out


def test_automation_scheduler_status_paragraph(home, monkeypatch):
    """``/automation scheduler`` returns one paragraph (off + log path)."""
    monkeypatch_scheduler(monkeypatch, home, installed=False)
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation_scheduler(
        agent, "run-1-abc", ""))
    assert out.startswith("Scheduler off — automations in this project do not fire on their own.")
    assert "/automation scheduler on" in out
    assert "automations tick" in out or "`" in out  # the command it would install
    assert "log:" in out


def test_automation_scheduler_on_calls_install(home, monkeypatch):
    """``/automation scheduler on`` → ``install_scheduler``."""
    called: dict[str, Any] = {}
    def fake_install(h):
        called["home"] = h
        return {"ok": True, "platform": "macos"}
    monkeypatch.setattr(
        "mini_ork.automations.install_scheduler", fake_install)
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation_scheduler_on(
        agent, "run-1-abc", ""))
    assert "Scheduler on" in out
    assert called.get("home") == home


def test_automation_scheduler_on_error_message(home, monkeypatch):
    """A failing ``install_scheduler`` surfaces the error."""
    monkeypatch.setattr(
        "mini_ork.automations.install_scheduler",
        lambda _h: {"ok": False, "error": "unsupported platform"},
    )
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation_scheduler_on(
        agent, "run-1-abc", ""))
    assert "Could not turn the scheduler on" in out
    assert "unsupported platform" in out


def test_automation_scheduler_off_calls_uninstall(home, monkeypatch):
    monkeypatch.setattr(
        "mini_ork.automations.uninstall_scheduler",
        lambda _h: {"ok": True, "platform": "macos", "removed": True},
    )
    agent = _agent(home)
    out = asyncio.run(cmds.handle_automation_scheduler_off(
        agent, "run-1-abc", ""))
    assert "Scheduler off" in out


def test_longest_match_routes_compound_keys(home):
    """``HANDLERS`` keys are routed via the longest-match prefix scan."""
    # Just exercise the table — the agent dispatcher in
    # ``MiniOrkAcpAgent._dispatch_slash`` is the real matcher; here we
    # confirm every compound key has a callable handler.
    for key in (
        "automations", "automation", "automation run", "automation pause",
        "automation resume", "automation delete",
        "automation scheduler", "automation scheduler status",
        "automation scheduler on", "automation scheduler off",
    ):
        assert key in cmds.HANDLERS, f"missing handler: {key}"
        assert callable(cmds.HANDLERS[key])


def test_merge_uses_merge_message_helper(home):
    """``/merge`` uses the ``_merge_message`` helper for the commit subject."""
    # The helper reads ``agent._run_base_titles`` / ``_thread_titles``.
    agent = _agent(home)
    msg = cmds._merge_message(agent, "no-thread", "run-m-1")
    assert msg == "mini-ork run run-m-1"
    agent._run_base_titles["run-m-1"] = "Fix the bug"
    msg = cmds._merge_message(agent, "no-thread", "run-m-1")
    assert msg == "Fix the bug (mini-ork run-m-1)"


def monkeypatch_scheduler(monkeypatch: pytest.MonkeyPatch, home: Path, *, installed: bool,
                          last_tick=None) -> None:
    """Replace ``automations.scheduler_status`` with a stub for this test only.

    Through ``monkeypatch`` so it is undone at teardown: an unstopped
    ``mock.patch`` leaked the stub into every later test file in the process.
    """
    import mini_ork.automations as _auto
    payload = {
        "platform": "macos",
        "installed": installed,
        "command": "/bin/echo hello",
        "log_path": str(home / "automations-tick.log"),
        "last_tick": last_tick,
    }
    monkeypatch.setattr(_auto, "scheduler_status", lambda _h: payload)


# ── /automation new (Zed S6b-2) ─────────────────────────────────────────────


def test_automation_new_in_thread_returns_rewrite_sentinel_with_bridge():
    """``/automation new [what]`` in a thread session returns the
    :class:`_RewriteToOrchestrate` sentinel with the scheduling-intent
    text and the automation bridge line."""
    agent = _agent(Path.cwd())
    agent._thread_sessions.add("run-1-abc")
    try:
        out = asyncio.run(cmds.handle_automation_new(
            agent, "run-1-abc", "nightly changelog"))
        assert isinstance(out, cmds._RewriteToOrchestrate)
        assert "nightly changelog" in out.intent_text
        assert "Follow your scheduling steps" in out.intent_text
        assert out.bridge is not None
        assert "When it has a proposal" in out.bridge
        assert "buttons to create it" in out.bridge
        # The bare ``/automation new`` (no arg) still asks.
        out_empty = asyncio.run(cmds.handle_automation_new(
            agent, "run-1-abc", ""))
        assert isinstance(out_empty, cmds._RewriteToOrchestrate)
        assert "Ask what should run and when" in out_empty.intent_text
    finally:
        agent._thread_sessions.discard("run-1-abc")


def test_automation_new_in_run_session_returns_string():
    """``/automation new`` in a run session (NOT a thread) returns a plain
    string the dispatcher emits directly."""
    agent = _agent(Path.cwd())
    out = asyncio.run(cmds.handle_automation_new(
        agent, "run-1-abc", "anything"))
    assert isinstance(out, str)
    assert "Automations are set up in a mini-ork thread" in out
    assert "start one from New Thread" in out
