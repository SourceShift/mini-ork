"""eco6-k5: the cost pause stops dispatch, and resume restarts the run.

The per-run ``.cost-pause`` sentinel used to be write-only: ``cost_pause.check``
left it in the run dir, the dispatch path never read it, and both resume
surfaces merely deleted it. These tests pin the closed loop:

  * the pre-dispatch gates park the run on the sentinel (rc 9), exactly like
    the intervention gate, without calling the lane dispatch function;
  * ``mini-ork resume`` clears + audits AND restarts the run through
    ``approvals_cmd._spawn_recover`` (``recover --strategy resume``);
  * the web ``resume_cost_run`` does the same and reports the outcome.

Hermetic: temp homes, the spawn helper monkeypatched at its documented seam,
no real process is forked and no LLM is dispatched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_ork.cli.approvals_cmd as approvals_cmd
from mini_ork.cli import execute, execute_handlers
from mini_ork.cli import resume as resume_mod
from mini_ork.context import run_context_scope
from mini_ork.gates import node_review
from mini_ork.web import control

_ENV_KEYS_TO_CLEAR = (
    "MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID", "MINI_ORK_HOME",
    "MINI_ORK_RECOVERY_CLOSURE", "MINI_ORK_RECOVERY_FROM",
    "MINI_ORK_PROMPT_OVERRIDE_DIR", "MO_ARTIFACT_BACKEND",
    "MO_NODE_FINISH_REASON", "MO_NODE_TIMEOUT_S", "MO_RESUME_SESSION_ID",
    "MO_ROUTING_POLICY", "MO_TARGET_CWD",
)

# An unregistered policy name is deterministic in routing.py: it warns and
# falls back to the workflow lane, consulting no learning DB.
_HERMETIC_POLICY = "eco6-k5-none-hermetic"


@pytest.fixture(autouse=True)
def _hermetic_env():
    """Isolate process env + the run-context contextvar layer per test."""
    for key in _ENV_KEYS_TO_CLEAR:
        os.environ.pop(key, None)
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-dispatch gate
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def gate(tmp_path, monkeypatch):
    """A dispatch_node harness whose only variable is the sentinel file."""
    root = tmp_path / "root"
    root.mkdir()
    run_id = "run-k5-gate"
    run_dir = tmp_path / "home" / "runs" / run_id
    run_dir.mkdir(parents=True)
    node_id = "k5probe-n1"

    monkeypatch.setenv("MO_ROUTING_POLICY", _HERMETIC_POLICY)
    # Neutralise the two pre-gate helpers that reach the wider world
    # (execute_handlers delegates them to execute, so patching there is
    # observable from dispatch_node).
    monkeypatch.setattr(execute, "_capture_pre_impl_baseline",
                        lambda *a, **k: None)
    monkeypatch.setattr(execute, "_intervention_decision",
                        lambda *a, **k: ("proceed", {}))

    dispatch_calls: list[tuple] = []

    def dispatch_fn(task_class, node_type, prompt):
        dispatch_calls.append((task_class, node_type, prompt))
        return 0, "ok"

    def handler(ctx):
        ctx.dispatch_fn(ctx.task_class, ctx.node_type, "k5 probe")
        return 0, "done"

    monkeypatch.setitem(execute_handlers.NODE_HANDLER_REGISTRY,
                        "k5probe", handler)

    def run_dispatch():
        fields = [node_id, "k5probe", "k5 probe node", "", "", "", "", ""]
        with run_context_scope({}):
            return execute_handlers.dispatch_node(
                fields,
                root=str(root),
                run_dir=str(run_dir),
                plan_path="",
                task_class="framework-edit",
                db=str(tmp_path / "missing.db"),
                run_id=run_id,
                dispatch_fn=dispatch_fn,
            )

    return SimpleNamespace(run=run_dispatch, run_dir=run_dir,
                           dispatch_calls=dispatch_calls,
                           node_id=node_id, run_id=run_id)


def test_cost_pause_sentinel_parks_before_dispatch(gate, capsys):
    (gate.run_dir / ".cost-pause").write_text(
        '{"threshold_usd": 25}\n', encoding="utf-8")

    rc, reason = gate.run()

    assert (rc, reason) == (node_review.REVIEW_PAUSE_RC, "paused_for_approval")
    assert gate.dispatch_calls == [], (
        "the lane dispatch function must never be called while the "
        "cost-pause sentinel is present"
    )
    assert os.environ.get("MO_NODE_FINISH_REASON") == "paused_for_approval"
    expected_stderr = (
        f"  [cost-pause] .cost-pause present — not dispatching "
        f"node_id={gate.node_id}; resume with: mini-ork resume {gate.run_id}"
    )
    assert expected_stderr in capsys.readouterr().err


def test_no_sentinel_proceeds_to_dispatch(gate):
    assert not (gate.run_dir / ".cost-pause").exists()

    rc, _reason = gate.run()

    assert rc == 0
    assert len(gate.dispatch_calls) == 1


def test_stop_requested_still_takes_precedence(gate):
    """.cost-pause is read directly after the .stop-requested check."""
    (gate.run_dir / ".stop-requested").write_text("0\n", encoding="utf-8")
    (gate.run_dir / ".cost-pause").write_text(
        '{"threshold_usd": 25}\n', encoding="utf-8")

    rc, reason = gate.run()

    assert (rc, reason) == (1, "interrupted")
    assert gate.dispatch_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# CLI resume
# ─────────────────────────────────────────────────────────────────────────────
def _make_cost_paused_home(tmp_path: Path, run_id: str):
    home = tmp_path / "home"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / ".cost-pause").write_text(
        json.dumps({"threshold_usd": 25, "run_id": run_id}) + "\n",
        encoding="utf-8",
    )
    return home, run_dir


def _spawn_recorder(monkeypatch, result=(123, "/tmp/cost-resume.log"),
                    exc: Exception | None = None):
    calls: list[dict] = []

    def fake_spawn_recover(home, run_id, extra_args, log_path=None):
        calls.append({"home": home, "run_id": run_id,
                      "extra_args": list(extra_args), "log_path": log_path})
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(approvals_cmd, "_spawn_recover", fake_spawn_recover)
    return calls


def _assert_restart_args(call: dict, home: Path, run_id: str,
                         run_dir: Path):
    assert os.path.realpath(str(call["home"])) == os.path.realpath(str(home))
    assert call["run_id"] == run_id
    assert call["extra_args"] == ["--strategy", "resume"]
    log_path = Path(str(call["log_path"]))
    assert log_path.name == "cost-resume.log"
    assert os.path.realpath(str(log_path.parent)) == os.path.realpath(
        str(run_dir))


def _audit_rows(run_dir: Path) -> list[dict]:
    approvals = run_dir / ".cost-pause-approvals.jsonl"
    assert approvals.is_file(), "the audit file must exist after resume"
    return [json.loads(line) for line in
            approvals.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_cli_resume_clears_audits_and_restarts(tmp_path, monkeypatch, capsys):
    run_id = "run-k5-resume-1"
    home, run_dir = _make_cost_paused_home(tmp_path, run_id)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    calls = _spawn_recorder(monkeypatch)

    rc = resume_mod.main([run_id])
    out = capsys.readouterr().out

    assert rc == 0
    assert not (run_dir / ".cost-pause").exists()
    rows = _audit_rows(run_dir)
    assert len(rows) == 1
    assert rows[0]["run_id"] == run_id
    assert len(calls) == 1
    _assert_restart_args(calls[0], home, run_id, run_dir)
    assert f"[mini-ork-resume] resumed {run_id}" in out
    assert "; restarting (log: " in out


def test_cli_resume_no_restart_clears_but_never_spawns(tmp_path, monkeypatch,
                                                       capsys):
    run_id = "run-k5-resume-2"
    home, run_dir = _make_cost_paused_home(tmp_path, run_id)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    calls = _spawn_recorder(monkeypatch)

    rc = resume_mod.main([run_id, "--no-restart"])
    out = capsys.readouterr().out

    assert rc == 0
    assert not (run_dir / ".cost-pause").exists()
    assert len(_audit_rows(run_dir)) == 1
    assert calls == []
    assert "restarting" not in out


def test_resume_restart_kwarg_false(tmp_path, monkeypatch):
    run_id = "run-k5-resume-3"
    home, run_dir = _make_cost_paused_home(tmp_path, run_id)
    calls = _spawn_recorder(monkeypatch)

    result = resume_mod.resume(run_id, home=str(home), restart=False)

    assert result[0] == 0
    assert not (run_dir / ".cost-pause").exists()
    assert len(_audit_rows(run_dir)) == 1
    assert calls == []


@pytest.mark.parametrize("exc", [
    approvals_cmd._LauncherMissingError("no launcher"),
    OSError("permission denied"),
])
def test_cli_resume_spawn_failure_hints_manual_recover(tmp_path, monkeypatch,
                                                       capsys, exc):
    run_id = "run-k5-resume-4"
    home, run_dir = _make_cost_paused_home(tmp_path, run_id)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    _spawn_recorder(monkeypatch, exc=exc)

    rc = resume_mod.main([run_id])
    out = capsys.readouterr().out

    assert rc == 0
    assert f"mini-ork recover {run_id} --strategy resume" in out
    assert "; restarting" not in out
    assert not (run_dir / ".cost-pause").exists()
    assert len(_audit_rows(run_dir)) == 1


def test_help_mentions_no_restart(capsys):
    rc = resume_mod.main(["--help"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "--no-restart" in out


# ─────────────────────────────────────────────────────────────────────────────
# Web resume
# ─────────────────────────────────────────────────────────────────────────────
def test_web_resume_cost_restarts_run(tmp_path, monkeypatch):
    run_id = "run-k5-web-1"
    home, run_dir = _make_cost_paused_home(tmp_path, run_id)
    calls = _spawn_recorder(monkeypatch)

    result = control.resume_cost_run(home, run_id, "op")

    assert result["ok"] is True
    assert result["restarted"] is True
    # The existing keys survive the extension.
    assert result["action"] == "resume-cost"
    assert result["approver"] == "op"
    assert result.get("log"), "the restarted dict must carry a log path"
    assert not (run_dir / ".cost-pause").exists()
    assert len(_audit_rows(run_dir)) == 1
    assert len(calls) == 1
    _assert_restart_args(calls[0], home, run_id, run_dir)


def test_web_resume_cost_spawn_failure_never_raises(tmp_path, monkeypatch):
    run_id = "run-k5-web-2"
    home, run_dir = _make_cost_paused_home(tmp_path, run_id)
    _spawn_recorder(monkeypatch,
                    exc=approvals_cmd._LauncherMissingError("no launcher"))

    result = control.resume_cost_run(home, run_id, "op")

    assert result["ok"] is True
    assert result["restarted"] is False
    assert result["log"] is None
    assert result["hint"] == f"mini-ork recover {run_id} --strategy resume"
    assert not (run_dir / ".cost-pause").exists()
    assert len(_audit_rows(run_dir)) == 1
