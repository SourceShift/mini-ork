"""An interrupted run resumes with no operator change (kickoff interrupted-resume).

``retry_hint`` case 1.6 classifies a run that stopped mid-node as
``needs_change.kind == "interrupted"``, ``retryable: true``, ``strategy:
"resume"``, ``from_node: <node>``. Three consumers used to treat *every*
``needs_change`` as "the operator must change something first" and refuse
without ``--ack-change``; ``auto_repair.decide`` had no rule and fell through to
``human``. Nothing needs changing for an interruption — resuming IS the fix — so
each consumer now exempts ``retry_hint.NO_CHANGE_KINDS``.

Fixtures mirror the sibling suites: a temp mini-ork home with an initialised DB,
``retry_hint`` stubbed so the hint is a controlled input, and — for the recover
tests — the minimal ``SCHEMA_SQL`` + run dir the lane-resume tests use.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from mini_ork.cli import board_cmd  # noqa: E402
from mini_ork.ide_pages import outcome  # noqa: E402
from mini_ork.ide_pages import run as run_page  # noqa: E402
from mini_ork.recovery import auto_repair as ar  # noqa: E402
from mini_ork.recovery import planner as rp  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from test_recover_lease_wiring import SCHEMA_SQL  # noqa: E402

RUN = "run-1791000000-abc123"
T0 = 1_791_000_000


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path):
    """Clear the ambient MINI_ORK_* family so the planner cannot leak into a
    parent run dir (mirrors ``test_lane_repair_resume.py``)."""
    for key in (
        "MINI_ORK_RUN_DIR",
        "MINI_ORK_RECIPE",
        "MINI_ORK_WORKFLOW",
        "MINI_ORK_TASK_CLASS",
        "MINI_ORK_RUN_ID",
        "MINI_ORK_DB",
        "MINI_ORK_LEASE_TOKEN",
        "MINI_ORK_RECOVERY_REQUEST",
        "MINI_ORK_AGENTS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))


def _interrupted_hint(run_id: str = "run-1") -> dict:
    """The exact shape ``retry_hint._case_interrupted`` produces (case 1.6)."""
    return {
        "version": 3,
        "run_id": run_id,
        "failed_node": "implementer",
        "retryable": True,
        "strategy": "resume",
        "from_node": "implementer",
        "needs_change": {
            "kind": "interrupted",
            "summary": (
                "Interrupted during implementer: the run stopped before the "
                "step finished. Resume from it."
            ),
            "detail": "",
            "evidence": "node_start without node_end",
        },
        "notes": [],
        "command": f"mini-ork recover {run_id} --strategy resume",
        "computed_at": "2026-01-01T00:00:00Z",
    }


def _code_hint(run_id: str = "run-1") -> dict:
    """A non-exempt ``needs_change`` — the gate must still refuse it."""
    return {
        "version": 3,
        "run_id": run_id,
        "failed_node": "implementer",
        "retryable": True,
        "strategy": "resume",
        "from_node": "implementer",
        "needs_change": {
            "kind": "code",
            "summary": "the reviewer rejected it",
            "detail": "the change is wrong",
        },
        "notes": [],
        "command": f"mini-ork recover {run_id}",
        "computed_at": "2026-01-01T00:00:00Z",
    }


# ── recover fixtures ────────────────────────────────────────────────────────

_NODES = [
    {"name": "planner", "type": "planner", "model_lane": "decomposer"},
    {"name": "implementer", "type": "implementer", "model_lane": "worker"},
    {"name": "verifier", "type": "verifier", "model_lane": "verifier"},
]
_EDGES = [
    {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
    {"from": "implementer", "to": "verifier", "edge_type": "verifies"},
]


def _write_workflow(path: Path) -> None:
    import yaml

    path.write_text(yaml.safe_dump(
        {"version": "0.1.0", "task_class": "framework_edit",
         "nodes": _NODES, "edges": _EDGES},
        sort_keys=False))


def _setup_recover(tmp_path: Path) -> tuple[str, str]:
    """Fresh minimal-schema ``state.db`` + a run dir, no seeded checkpoints, so
    the closure is every node and the dispatch path is reached."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.close()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps(
        {"recipe": "framework-edit", "roots": {"exec_cwd": None, "target": None}}))
    return str(db), str(run_dir)


# ── full-schema home (outcome + auto-repair) ────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed_run(home: Path, *, status: str = "failed",
              recipe: str = "framework-edit", run_id: str = RUN,
              notes: str | None = None) -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": recipe}))
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
        "updated_at, ended_at, task_class, kickoff_path, workflow_version, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, 0.5, T0, T0 + 120, T0 + 100,
         "framework_edit", "", "latest", notes),
    )
    con.commit()
    con.close()
    return run_dir


# ─────────────────────────────────────────────────────────────────────────────
# 1. ``mini-ork recover`` dispatches an interrupted hint without --ack-change
# ─────────────────────────────────────────────────────────────────────────────


def test_recover_interrupted_hint_dispatches_without_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """The interrupted hint's own command (``recover <run> --strategy resume``)
    must not be refused: no change is needed, resuming IS the fix."""
    db, run_dir = _setup_recover(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    run_id = "run-intr-1"
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf)
    monkeypatch.setattr(rp, "_load_retry_hint", lambda *a, **kw: _interrupted_hint(run_id))

    calls: list = []
    rc = rp.cli_main(
        [run_id, "--workflow", str(wf), "--db", db],
        execute_fn=lambda a: calls.append(list(a)) or 0,
    )
    out_err = capsys.readouterr()
    assert rc == 0, (rc, out_err)
    assert len(calls) == 1, calls


def test_recover_code_hint_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """A non-exempt ``needs_change`` (a code revision) is still an operator
    change: refused without ``--ack-change``, nothing dispatched."""
    db, run_dir = _setup_recover(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    run_id = "run-code-1"
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf)
    monkeypatch.setattr(rp, "_load_retry_hint", lambda *a, **kw: _code_hint(run_id))

    calls: list = []
    rc = rp.cli_main(
        [run_id, "--workflow", str(wf), "--db", db],
        execute_fn=lambda a: calls.append(list(a)) or 0,
    )
    out_err = capsys.readouterr()
    assert rc == 1, (rc, out_err)
    assert calls == [], calls
    assert "Needs a change before retrying" in out_err.err


# ─────────────────────────────────────────────────────────────────────────────
# 2. ``board retry`` needs no --ack-change for an interrupted hint
# ─────────────────────────────────────────────────────────────────────────────


def test_board_retry_interrupted_hint_needs_no_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured: dict = {}

    class _Fake:
        pid = 4242

    def _spawn(argv, *, cwd, env, stdout_path):
        captured["argv"] = argv
        return _Fake()

    monkeypatch.setattr(board_cmd, "_retry_spawn", _spawn)
    monkeypatch.setattr("mini_ork.recovery.retry_hint.load_or_compute",
                        lambda home, run_id, write=True: _interrupted_hint("run-1"))

    payload = board_cmd._act_retry(Path(tmp_path), "run-1")

    assert "needs a change" not in str(payload.get("error") or ""), payload
    assert payload.get("ok") is True, payload
    argv = captured["argv"]
    assert argv[2:4] == ["recover", "run-1"]
    assert "--ack-change" not in argv
    assert "--force" not in argv


# ─────────────────────────────────────────────────────────────────────────────
# 3. outcome page offers one ``Resume from <node>`` button, no --ack-change
# ─────────────────────────────────────────────────────────────────────────────


def test_outcome_interrupted_run_offers_resume_from_node(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    _seed_run(home, status="failed")
    import mini_ork.recovery.retry_hint as rh

    monkeypatch.setattr(rh, "load_or_compute", lambda *a, **k: _interrupted_hint(RUN))

    run = run_page._load(home, RUN)
    assert run is not None
    out = outcome.resolve(run)

    labels = [a["label"] for a in out["actions"]]
    assert "Resume from implementer" in labels, labels
    # The generic needs-change branch must NOT swallow it.
    assert "I fixed it — retry" not in labels, labels

    btn = next(a for a in out["actions"] if a["label"] == "Resume from implementer")
    assert btn["do"]["cli"] == ["board", "retry", RUN]
    assert "--ack-change" not in btn["do"]["cli"]
    assert "implementer" in btn["do"]["confirm"]
    assert btn["kind"] == "primary"


# ─────────────────────────────────────────────────────────────────────────────
# 4. auto-repair resumes an interrupted run (unless a person killed it)
# ─────────────────────────────────────────────────────────────────────────────


def _stub_ar(monkeypatch: pytest.MonkeyPatch, hint: dict) -> None:
    import mini_ork.recovery.retry_hint as rh

    monkeypatch.setattr(rh, "_recipe_workflow",
                        lambda home, recipe: (list(_NODES), list(_EDGES)))
    monkeypatch.setattr(rh, "load_or_compute", lambda *a, **k: hint)
    # tests/conftest.py turns auto-repair off suite-wide; these tests exercise
    # the loop itself.
    monkeypatch.delenv("MO_AUTO_REPAIR", raising=False)


def test_auto_repair_interrupted_resumes_from_the_hint_node(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    _seed_run(home, status="failed")
    _stub_ar(monkeypatch, _interrupted_hint(RUN))

    decision = ar.decide(home, RUN)

    assert decision["action"] == "infra", decision
    assert decision["from_node"] == "implementer"
    assert "implementer" in decision["reason"]
    assert decision["stop"] is False


def test_auto_repair_killed_by_user_is_not_resumed(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """``board kill`` writes ``notes = "killed-by-user"`` — a deliberate stop is
    handed back, never silently resumed."""
    _seed_run(home, status="failed", notes="killed-by-user")
    _stub_ar(monkeypatch, _interrupted_hint(RUN))

    decision = ar.decide(home, RUN)

    assert decision["action"] == "none", decision
    assert "killed by the user" in decision["reason"]


def test_auto_repair_killed_by_user_after_a_prior_note(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Notes are append-only: a note written BEFORE the kill pushes
    ``killed-by-user`` off the start of the column, so a prefix test on the
    whole string would wrongly auto-resume a killed run. Reproduce ``board
    kill``'s exact write (``web/control.py`` ``_writeback_terminal``)."""
    _seed_run(home, status="failed",
              notes="verifier_not_executed: node test_verifier (x)")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "UPDATE task_runs SET notes = COALESCE(notes || '; ', '') || ?, "
        "status = 'failed' WHERE id = ?",
        ("killed-by-user", RUN),
    )
    con.commit()
    con.close()
    _stub_ar(monkeypatch, _interrupted_hint(RUN))

    decision = ar.decide(home, RUN)

    assert decision["action"] == "none", decision
    assert "killed by the user" in decision["reason"]
