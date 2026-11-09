"""Hermetic unit tests for the diversity / pass@k floor on ``circuit_breaker``.

Pin RSI Tier-1 #12b (kickoff auto/rsi-t1-collapse-floor-breaker.md): the
``collapse_detector.floor`` signal is wired into ``check_liveness_breaker`` as
an edge-triggered trip — in **armed** mode. It ships in **shadow** mode: the
floor is evaluated and reported on every run, but neither trips nor latches,
so an operator can watch it against live traffic before arming it (the
2026-10-29 review's W1/W2/W3 and its "ship in report mode for one cycle" ask).

Case list:
  (a) the default is shadow — a breach is reported, never trips
  (b) an unrecognised ``MO_CB_COLLAPSE_FLOOR`` value stays in shadow
  (c) armed: the first breach trips (floor is the sole cause, vote silent)
  (d) armed: the latch holds on an immediate second breach
  (e) armed: a healthy evaluation clears the latch; a later breach re-trips
  (f) armed: a fail-open evaluation does NOT clear the latch (W1)
  (g) armed: the latch is keyed on task_class, not task_class::recipe (W3)
  (h) ``MO_CB_COLLAPSE_FLOOR=0`` turns the floor off
  (i) ``MO_CB_COLLAPSE=0`` turns the floor off too
  (j) armed, production read path (no seam) — 40 terminal runs from the DB
  (k) a raising detector fails open (no trip)
  (l) a detector returning a non-mapping fails open (W2)
  (m) armed, both signals fire → last_reason carries both tokens in order

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
TASK_CLASS = "code_fix"


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


@pytest.fixture
def armed(monkeypatch):
    """Put the floor in armed mode for the trip/latch cases."""
    monkeypatch.setenv("MO_CB_COLLAPSE_FLOOR", "1")


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
        for table in ("circuit_breaker_state", "collapse_floor_latch"):
            try:
                con.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass  # latch table is created by the first breaker call
        con.commit()
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


def _live_run(db: str, run_id: str = "run-live", recipe: str = "code-fix") -> None:
    """One classified run so the vote is silent (fired_count == 0)."""
    _seed_task_runs(db, [
        {"id": run_id, "task_class": TASK_CLASS, "recipe": recipe,
         "artifact_hash": "1111" * 4, "cost_usd": 0.05,
         "created_at": _now() - 1},
    ])


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


def _last_reason(db: str, scope: str = SCOPE) -> str | None:
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT last_reason FROM circuit_breaker_state WHERE scope_key=?",
            (scope,),
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _latched(db: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute(
            "SELECT task_class FROM collapse_floor_latch ORDER BY task_class")]
    finally:
        con.close()


# ── (a) the default is shadow ────────────────────────────────────────────────
def test_default_mode_is_shadow_and_never_trips(db):
    _live_run(db)
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["verdict"] == "PROCEED"
    assert out["collapse"]["floor_mode"] == "shadow"
    assert out["collapse"]["floor_enabled"] is True
    # Evaluated and reported ...
    assert out["collapse"]["floor_state"] == "breach"
    assert out["collapse"]["floor_fired"] is True
    assert out["collapse"]["floor"]["recommendation"] == "halt"
    # ... but never trips and never latches.
    assert out["collapse"]["floor_independent_trip"] is False
    assert out["collapse"]["floor_latched"] is False
    assert _latched(db) == []
    assert _last_reason(db) is None


# ── (b) an unrecognised mode value stays in shadow ───────────────────────────
def test_unrecognised_mode_value_stays_shadow(db, monkeypatch):
    _live_run(db)
    monkeypatch.setenv("MO_CB_COLLAPSE_FLOOR", "yeah-arm-it")
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_mode"] == "shadow"


# ── (c) armed: first breach trips ────────────────────────────────────────────
def test_first_breach_trips_when_armed(db, armed):
    _live_run(db)
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 1
    assert out["verdict"] == "LIVENESS_TRIP"
    assert out["collapse"]["floor_mode"] == "armed"
    assert out["collapse"]["floor_fired"] is True
    assert out["collapse"]["floor_independent_trip"] is True
    assert out["collapse"]["floor_latched"] is False
    # `fired` is still the score-vs-anchor divergence signal only.
    assert out["collapse"]["fired"] is False
    assert out["rationale"].startswith("collapse_floor fired under policy=majority")
    assert _last_reason(db) == "collapse_floor"
    assert _latched(db) == [TASK_CLASS]


# ── (d) the latch holds ──────────────────────────────────────────────────────
def test_latch_holds_on_second_breach(db, armed):
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
    assert _latched(db) == [TASK_CLASS]


# ── (e) the latch clears on health, then re-trips ────────────────────────────
def test_latch_clears_then_retrips(db, armed):
    _live_run(db)
    cb.check_liveness_breaker("run-live", db=db, floor_runs=_breach_rows())
    cb.check_liveness_breaker("run-live", db=db, floor_runs=_breach_rows())
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_healthy_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_state"] == "clear"
    assert out["collapse"]["floor_fired"] is False
    assert _latched(db) == []
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 1
    assert out["collapse"]["floor_independent_trip"] is True


# ── (f) a fail-open evaluation does NOT clear the latch (W1) ─────────────────
def test_fail_open_does_not_clear_the_latch(db, armed, monkeypatch):
    _live_run(db)
    cb.check_liveness_breaker("run-live", db=db, floor_runs=_breach_rows())
    assert _latched(db) == [TASK_CLASS]

    def _raise(runs, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cd_module, "floor", _raise)
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_state"] == "unknown"
    # "The detector did not answer" is not evidence that the collapse ended.
    assert _latched(db) == [TASK_CLASS]

    # The next real breach is therefore still latched, not a fresh trip.
    monkeypatch.undo()
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_latched"] is True
    assert out["collapse"]["floor_independent_trip"] is False


# ── (g) the latch is keyed on task_class, not task_class::recipe (W3) ────────
def test_latch_is_keyed_on_task_class(db, armed):
    _live_run(db, "run-a", recipe="code-fix")
    _live_run(db, "run-b", recipe="other-recipe")
    _reset_cb_state(db)

    out, rc = cb.check_liveness_breaker(
        "run-a", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 1, "the first breach under the first recipe trips"
    assert _last_reason(db, "code_fix::code-fix") == "collapse_floor"

    # Same task_class, different recipe: the floor is evaluated per CLASS, so
    # the breach is the same collapse and must not trip a second time.
    out, rc = cb.check_liveness_breaker(
        "run-b", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_latched"] is True
    assert out["collapse"]["floor_independent_trip"] is False
    assert _latched(db) == [TASK_CLASS]


# ── (h) MO_CB_COLLAPSE_FLOOR=0 ───────────────────────────────────────────────
def test_opt_out_env_disables_floor(db, monkeypatch):
    _live_run(db)
    monkeypatch.setenv("MO_CB_COLLAPSE_FLOOR", "0")
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_mode"] == "off"
    assert out["collapse"]["floor_enabled"] is False
    assert out["collapse"]["floor_state"] == "unknown"
    assert out["collapse"]["floor_fired"] is False
    assert out["collapse"]["floor"]["recommendation"] == "none"


# ── (i) MO_CB_COLLAPSE=0 disables the floor too ──────────────────────────────
def test_opt_out_collapse_off_disables_floor(db, monkeypatch):
    _live_run(db)
    monkeypatch.setenv("MO_CB_COLLAPSE", "0")
    monkeypatch.setenv("MO_CB_COLLAPSE_FLOOR", "1")  # armed loses to collapse=0
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_mode"] == "off"
    assert out["collapse"]["floor_enabled"] is False


# ── (j) production read path (no seam) ───────────────────────────────────────
def test_production_read_path_trips(db, armed):
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
            (f"r{i}", TASK_CLASS, "code-fix", f"h{i:04d}", 0.05,
             now - 1000 + i, now - 1000 + i, status, f"kp{i}"),
        )
        con.execute(
            "INSERT INTO execution_traces (trace_id, run_id, task_class,"
            " agent_version_id, status, verifier_output, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"tr-implementer-implementer-{i:08x}", f"r{i}", TASK_CLASS, lane,
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


# ── (k) fail-open when the detector raises ───────────────────────────────────
def test_fail_open_when_floor_raises(db, armed, monkeypatch):
    _live_run(db)

    def _raise(runs, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cd_module, "floor", _raise)
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_state"] == "unknown"
    assert out["collapse"]["floor_fired"] is False
    assert "floor raised RuntimeError" in out["collapse"]["floor"]["reason"]


# ── (l) a non-mapping detector result fails open, not with AttributeError ────
def test_fail_open_when_floor_returns_garbage(db, armed, monkeypatch):
    _live_run(db)

    def _nothing(runs, **kwargs):
        return None

    monkeypatch.setattr(cd_module, "floor", _nothing)
    out, rc = cb.check_liveness_breaker(
        "run-live", db=db, floor_runs=_breach_rows(),
    )
    assert rc == 0
    assert out["collapse"]["floor_state"] == "unknown"
    assert "not a mapping" in out["collapse"]["floor"]["reason"]


# ── (m) both signals: collapse_halt and the floor both persist ───────────────
def test_both_signals_last_reason(db, armed):
    _live_run(db)
    _seed_collapse_history(db, TASK_CLASS, [
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
    assert _latched(db) == [TASK_CLASS]
