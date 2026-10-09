"""Focused tests for the reflect trace-leak fix.

Reflect is spawned by the parent with ``subprocess.run(timeout=...)``. On
timeout Python SIGKILLs the child, so reflect's own terminal trace write never
runs and its ``status='running'`` row leaks — inflating the running count AND
hiding the run from ``resolve_finished_runs`` (which reads "run over = no trace
running"). The fix has three parts:

  (1) ``trace_store.finalize_reflect_traces`` — the terminal write the parent
      owes (keyed on run_id) plus an age-guarded global sweep (self-heal),
  (2) the parent's reflect ``finally`` calling (1) with this run's id,
  (3) a child-side atexit guard for the exception/crash path.

These lock (1) and (3), plus a source guard for (2); the live run smoke
exercises (2) end-to-end.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
INIT_SH = REPO / "db" / "init.sh"

from mini_ork import trace_store  # noqa: E402


@pytest.fixture
def db(tmp_path_factory):
    """A real mini-ork DB (schema parity with production) via db/init.sh."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def _insert(dbp, trace_id, run_id, task_class, status, created_at):
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute(
        "INSERT INTO execution_traces(trace_id, run_id, task_class, status, created_at) "
        "VALUES (?,?,?,?,?)", (trace_id, run_id, task_class, status, created_at))
    con.commit()
    con.close()


def _status(dbp, trace_id):
    con = sqlite3.connect(dbp)
    row = con.execute(
        "SELECT status FROM execution_traces WHERE trace_id=?", (trace_id,)).fetchone()
    con.close()
    return row[0] if row else None


# ── (1) trace_store.finalize_reflect_traces ─────────────────────────────────

def test_keyed_sweep_finalizes_only_that_runs_reflect(db):
    now = _iso(int(time.time()))
    _insert(db, "tr-a", "run-a", "__reflect__", "running", now)
    _insert(db, "tr-b", "run-b", "__reflect__", "running", now)
    _insert(db, "tr-a-verify", "run-a", "verify", "success", now)

    n = trace_store.finalize_reflect_traces("run-a", db=db)

    assert n == 1
    assert _status(db, "tr-a") == "failure"        # this run's reflect → terminal
    assert _status(db, "tr-b") == "running"        # scoped: another run untouched
    assert _status(db, "tr-a-verify") == "success"  # non-reflect untouched


def test_global_sweep_is_age_guarded(db):
    now = int(time.time())
    _insert(db, "tr-old", "run-old", "__reflect__", "running", _iso(now - 3600))
    _insert(db, "tr-new", "run-new", "__reflect__", "running", _iso(now - 5))

    n = trace_store.finalize_reflect_traces(db=db, grace_s=720)

    assert n == 1
    assert _status(db, "tr-old") == "failure"   # stale orphan healed
    assert _status(db, "tr-new") == "running"   # in-flight, never raced


def test_global_sweep_never_touches_non_reflect(db):
    _insert(db, "tr-plan", "run-p", "plan", "running", _iso(int(time.time()) - 3600))
    assert trace_store.finalize_reflect_traces(db=db, grace_s=720) == 0
    assert _status(db, "tr-plan") == "running"


def test_cold_safe_missing_db(tmp_path):
    assert trace_store.finalize_reflect_traces("run-x", db=str(tmp_path / "nope.db")) == 0


# ── (3) child-side atexit guard ─────────────────────────────────────────────

def test_atexit_finalizer_writes_failure(db):
    from mini_ork.cli import reflect
    env = {**os.environ, "MINI_ORK_DB": db}
    reflect._trace_finalizer("tr-ref-1", env, {"terminal": False})
    assert _status(db, "tr-ref-1") == "failure"


def test_atexit_finalizer_noop_when_terminal(db):
    from mini_ork.cli import reflect
    env = {**os.environ, "MINI_ORK_DB": db}
    reflect._trace_finalizer("tr-ref-2", env, {"terminal": True})
    assert _status(db, "tr-ref-2") is None


# ── (2) parent wiring (source guard; smoke covers behaviour) ────────────────

def test_run_flow_wires_reflect_sweep():
    src = (REPO / "mini_ork" / "cli" / "main.py").read_text()
    assert "trace_store.finalize_reflect_traces(run_id)" in src
