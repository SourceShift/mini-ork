"""Unit tests for the U1 (detached run launch) + U2 (HITL steering write)
control-plane primitives in mini_ork.web.control.

Exercises the helper functions directly (no httpx) against a throwaway
state.db, mirroring tests/test_web_smoke.py. The launch test points
MINI_ORK_ROOT at a temp dir with a no-op `bin/mini-ork` so the spawn path
is exercised without running a real recipe.
"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from mini_ork.web import control
from mini_ork.web.db import StateDB

OPERATOR_STEERING_DDL = """
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

TASK_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS task_runs (
    id      TEXT PRIMARY KEY,
    status  TEXT
);
"""


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".mini-ork"
    h.mkdir(parents=True)
    con = sqlite3.connect(h / "state.db")
    con.executescript(OPERATOR_STEERING_DDL + TASK_RUNS_DDL)
    con.execute("INSERT INTO task_runs(id, status) VALUES (?, ?)", ("run-test-1", "executing"))
    con.commit()
    con.close()
    return h


@pytest.fixture()
def db(home: Path) -> StateDB:
    return StateDB(home / "state.db")


# ── U2: steer_run ────────────────────────────────────────────────────────


def test_steer_run_inserts_row(db: StateDB) -> None:
    out = control.steer_run(db, "run-test-1", "focus on 2026 papers", role_target="planner")
    assert out["ok"] is True
    assert out["steering_id"] >= 1
    row = db.row(
        "SELECT run_id, role_target, message, severity FROM operator_steering WHERE id = ?",
        (out["steering_id"],),
    )
    assert row["run_id"] == "run-test-1"
    assert row["role_target"] == "planner"
    assert row["message"] == "focus on 2026 papers"
    assert row["severity"] == "info"


def test_steer_run_global_queue_when_run_none(db: StateDB) -> None:
    out = control.steer_run(db, None, "global guidance")
    assert out["ok"] is True
    row = db.row("SELECT run_id FROM operator_steering WHERE id = ?", (out["steering_id"],))
    assert row["run_id"] is None  # NULLIF('', '') → NULL → global queue


def test_steer_run_rejects_bad_input(db: StateDB) -> None:
    assert control.steer_run(db, "run-test-1", "")["ok"] is False
    assert control.steer_run(db, "run-test-1", "x", role_target="boss")["ok"] is False
    assert control.steer_run(db, "run-test-1", "x", severity="loud")["ok"] is False
    assert control.steer_run(db, "run-test-1", "x", confidence=2.0)["ok"] is False


def test_steer_run_unknown_run_is_not_found(db: StateDB) -> None:
    out = control.steer_run(db, "run-does-not-exist", "hi")
    assert out["ok"] is False
    assert "not found" in out["error"]


# ── U1: launch_run ───────────────────────────────────────────────────────


def test_launch_run_rejects_bad_input(db: StateDB, home: Path) -> None:
    assert control.launch_run(home, "bad recipe; rm -rf", "kick")["ok"] is False
    assert control.launch_run(home, "code-fix", "")["ok"] is False
    assert control.launch_run(home, "code-fix", "kick", run_id="../escape")["ok"] is False


def test_launch_run_spawns_and_stages_kickoff(home: Path, tmp_path: Path, monkeypatch) -> None:  # noqa: ARG001
    # Fake repo root with a no-op bin/mini-ork so the spawn path runs without
    # launching a real recipe.
    fake_root = tmp_path / "fake-root"
    (fake_root / "bin").mkdir(parents=True)
    bin_mo = fake_root / "bin" / "mini-ork"
    bin_mo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bin_mo.chmod(bin_mo.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("MINI_ORK_ROOT", str(fake_root))

    out = control.launch_run(home, "code-fix", "# kickoff\n\nGoal: x\n", run_id="run-launch-1")
    assert out["ok"] is True, out
    assert out["run_id"] == "run-launch-1"
    assert out["recipe"] == "code-fix"
    assert isinstance(out["pid"], int)
    # Kickoff was staged where the run can read it.
    staged = Path(out["kickoff_path"])
    assert staged.exists()
    assert "Goal: x" in staged.read_text(encoding="utf-8")
    assert staged.parent == home / "runs-inbox"
