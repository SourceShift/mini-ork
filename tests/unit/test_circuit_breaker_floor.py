"""Hermetic unit tests for the diversity / pass@k floor on ``circuit_breaker``.

Pin RSI Tier-1 #12b (kickoff auto/rsi-t1-collapse-floor-breaker.md): the
``collapse_detector.floor`` signal is wired into ``check_liveness_breaker`` as
an edge-triggered trip. A breach trips once, then latches via the
``collapse_floor`` token in ``circuit_breaker_state.last_reason``; a healthy
evaluation clears the latch, and a later breach trips again.

Case list:
  (a) first breach trips (floor is the sole cause, vote stays silent)
  (b) the latch holds on an immediate second breach
  (c) a healthy evaluation clears the latch; a later breach re-trips
  (d) ``MO_CB_COLLAPSE_FLOOR=0`` disables the floor
  (e) ``MO_CB_COLLAPSE=0`` disables the floor too
  (f) production read path (no seam) — 40 terminal runs from the DB
  (g) a raising detector fails open (no trip)
  (h) both signals fire → last_reason carries both tokens in order

Hermetic: temp sqlite per test, no network, no provider credentials.
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.learning import collapse_detector as cd_module  # noqa: E402
from mini_ork.recovery import circuit_breaker as cb  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

SCOPE = "code_fix::code-fix"


@pytest.fixture
def db(tmp_path_factory, monkeypatch):
    """Bootstrap a fresh DB via init_db (full task_runs + execution_traces + collapse_history schema)."""
    for var in ("MO_CB_DISABLE", "MO_CB_COLLAPSE", "MO_CB_COLLAPSE_FLOOR",
                "MO_CB_FLOOR_WINDOW"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return dbp


def _now() -> int:
    return int(time.time())


def _seed_task_runs(db: str, rows: list[dict]) -> None:
    """rows = list of {id, task_class, recipe?, artifact_hash?, cost_usd?, created_at}."""
    con = sqlite3.connect(db)
    for r in rows:
        con.execute(
            """
            INSERT INTO task_runs
                (id, task_class, recipe, artifact_hash, cost_usd,
                 kickoff_path, workflow_version, created_at, updated_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'latest', ?, ?, 'classified')
            """,
            (
                r["id"], r["task_class"], r.get("recipe"),
                r.get("artifact_hash"), r.get("cost_usd", 0.0),
                "/tmp/test.kickoff.md",
                r["created_at"], r["created_at"],
            ),
        )
    con.commit()
    con.close()


def _reset_cb_state(db: str) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute("DELETE FROM circuit_breaker_state")
        con.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()


def _seed_collapse_history(db: str, task_class: str, rows: list[dict]) -> None:
    """Seed the ``collapse_history`` table (production read path for collapse_halt)."""
    con = sqlite3.connect(db)
    now = _now()
    for r in rows:
        con.execute(
            """
            INSERT INTO collapse_history
                (task_class, step, score, anchor, directives, run_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_class, r["step"], r["score"], r["anchor"],
                r.get("directives", 0), r.get("run_id"), now,
            ),
        )
    con.commit()
    con.close()


def _live_run(db: str) -> None:
    """One classified run so the vote is silent (fired_count == 0)."""
    _seed_task_runs(db, [
        {"id": "run-live", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "1111" * 4, "cost_usd": 0.05,
         "created_at": _now() - 1},
    ])
    _reset_cb_state(db)


def _breach_rows() -> list[dict]:
    """40 rows: prior 20 first-10-passed lanes a/b alternating; recent 20
    first-2-passed every lane ["a"]."""
    rows = []
    for i in range(40):
        if i < 20:
            passed = i < 10
            lanes = ["a"] if i % 2 == 0 else ["b"]
        else:
            passed = i < 22
            lanes = ["a"]
        rows.append({"run_id": f"r{i}", "passed": passed,
                     "task_key": f"k{i}", "lanes": lanes})
    return rows


def _healthy_rows() -> list[dict]:
    """40 passing rows with lanes ["a", "b"]."""
    return [{"run_id": f"h{i}", "passed": True, "task_key": f"hk{i}",
             "lanes": ["a", "b"]} for i in range(40)]


def _last_reason(db: str) -> str | None:
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT last_reason FROM circuit_breaker_state WHERE scope_key=?",
            (SCOPE,),
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


# ── (a) first breach trips ───────────────────────────────────────────────────
def test_first_breach_trips(db):
    _live_run(db)
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 1
    assert out["verdict"] == "LIVENESS_TRIP"
    assert out["collapse"]["floor_fired"] is True
    assert out["collapse"]["floor_independent_trip"] is True
    assert out["collapse"]["floor_latched"] is False
    # `fired` is still the score-vs-anchor divergence signal only.
    assert out["collapse"]["fired"] is False
    assert out["rationale"].startswith("collapse_floor fired under policy=majority")
    assert _last_reason(db) == "collapse_floor"
    assert out["collapse"]["floor"]["recommendation"] == "halt"


# ── (b) the latch holds ──────────────────────────────────────────────────────
def test_latch_holds_on_second_breach(db):
    _live_run(db)
    cb.check_liveness_breaker("run-live", db=db, floor_runs=_breach_rows())
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["verdict"] == "PROCEED"
    assert out["collapse"]["floor_fired"] is True
    assert out["collapse"]["floor_latched"] is True
    assert out["collapse"]["floor_independent_trip"] is False
    assert _last_reason(db) == "collapse_floor"


# ── (c) the latch clears on health, then re-trips ────────────────────────────
def test_latch_clears_then_retrips(db):
    _live_run(db)
    cb.check_liveness_breaker("run-live", db=db, floor_runs=_breach_rows())
    cb.check_liveness_breaker("run-live", db=db, floor_runs=_breach_rows())
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_healthy_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_fired"] is False
    assert _last_reason(db) is None
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 1


# ── (d) MO_CB_COLLAPSE_FLOOR=0 ───────────────────────────────────────────────
def test_opt_out_env_disables_floor(db, monkeypatch):
    _live_run(db)
    monkeypatch.setenv("MO_CB_COLLAPSE_FLOOR", "0")
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_enabled"] is False
    assert out["collapse"]["floor_fired"] is False
    assert out["collapse"]["floor"]["recommendation"] == "none"


# ── (e) MO_CB_COLLAPSE=0 disables the floor too ──────────────────────────────
def test_opt_out_collapse_off_disables_floor(db, monkeypatch):
    _live_run(db)
    monkeypatch.setenv("MO_CB_COLLAPSE", "0")
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_enabled"] is False


# ── (f) production read path (no seam) ───────────────────────────────────────
def test_production_read_path_trips(db):
    _live_run(db)
    now = _now()
    con = sqlite3.connect(db)
    for i in range(40):
        if i < 20:
            status = "published" if i < 10 else "failed"
            lane = "a" if i % 2 == 0 else "b"
        else:
            status = "published" if i < 22 else "failed"
            lane = "a"
        con.execute(
            "INSERT INTO task_runs (id, task_class, recipe, artifact_hash,"
            " cost_usd, created_at, updated_at, status, kickoff_path)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"r{i}", "code_fix", "code-fix", f"h{i:04d}", 0.05,
             now - 1000 + i, now - 1000 + i, status, f"kp{i}"),
        )
        con.execute(
            "INSERT INTO execution_traces (trace_id, run_id, task_class,"
            " agent_version_id, status, verifier_output, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"tr-implementer-implementer-{i:08x}", f"r{i}", "code_fix", lane,
             "success", '{"node_type": "implementer"}',
             datetime.datetime.fromtimestamp(
                 now - 1000 + i, tz=datetime.timezone.utc).isoformat()),
        )
    con.commit()
    con.close()

    # run-live is `classified`, so the floor sees only the 40 terminal runs.
    out, rc = cb.check_liveness_breaker("run-live", db=db)
    assert rc == 1
    assert out["collapse"]["floor_fired"] is True
    assert out["collapse"]["floor_independent_trip"] is True
    assert out["collapse"]["floor"]["recommendation"] == "halt"


# ── (g) fail-open when the detector raises ───────────────────────────────────
def test_fail_open_when_floor_raises(db, monkeypatch):
    _live_run(db)

    def _raise(runs, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cd_module, "floor", _raise)
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_fired"] is False
    assert "floor raised RuntimeError" in out["collapse"]["floor"]["reason"]


# ── (h) both signals: collapse_halt and the floor latch both persist ─────────
def test_both_signals_last_reason(db):
    _live_run(db)
    _seed_collapse_history(db, "code_fix", [
        {"step": 0, "score": 0.40, "anchor": 0.80, "directives": 1},
        {"step": 1, "score": 0.50, "anchor": 0.75, "directives": 1},
        {"step": 2, "score": 0.60, "anchor": 0.60, "directives": 1},
        {"step": 3, "score": 0.70, "anchor": 0.50, "directives": 1},
    ])
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 1
    assert out["rationale"].startswith("collapse_halt fired under policy=")
    assert _last_reason(db) == "collapse_halt,collapse_floor"
    # out["collapse"]["fired"] stays the divergence signal only.
    assert out["collapse"]["fired"] is True
    assert out["collapse"]["floor_fired"] is True
