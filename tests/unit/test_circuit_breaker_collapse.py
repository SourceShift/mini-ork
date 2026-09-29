"""Hermetic unit tests for the collapse_halt signal on ``circuit_breaker``.

Pin the four cases from the I4a kickoff
(kickoffs/auto/rsi-i4-collapse-halt-repair.md):
  (a) rows seeded in ``collapse_history`` table (NO ``collapse_history=`` kwarg)
      that show score-rise / anchor-drop → breaker trips with verdict
      LIVENESS_TRIP, state OPEN, ``out["collapse"]["fired"] is True``, and the
      vote contributes ``fired_count == 0`` (collapse is independent);
  (b) healthy rows → no trip; ``fired_count == 0``, ``signal_count == 3``,
      ``out["collapse"]["fired"] is False``;
  (c) missing ``collapse_history`` table → no trip (fail-open);
  (d) ``MO_CB_COLLAPSE=0`` → never trips via collapse; ``out["collapse"]
      ["enabled"] is False``;
  (e) collapse trips under ``policy="and"`` with zero stagnation signals
      (verifies the independent-trip branch).

Plus three retained cases for the synthetic-history / detector-fail-open /
production-empty contract:
  (f) synthetic ``collapse_history=`` kwarg → trip (test seam);
  (g) synthetic healthy history → no trip;
  (h) detector raising on malformed rows → no trip.

The kickoff requires (a)–(e) to be exercised through the no-kwarg path —
production never forward-passes ``collapse_history=``. (f)–(h) cover the
test seam so a refactor that breaks the detector integration is caught
even when the DB read path is unchanged.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.recovery import circuit_breaker as cb  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


@pytest.fixture
def db(tmp_path_factory):
    """Bootstrap a fresh DB via init_db (full task_runs + execution_traces + collapse_history schema)."""
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


def _seed_collapse_history(db: str, task_class: str,
                           rows: list[dict]) -> None:
    """rows = list of {step, score, anchor, directives?, run_id?, created_at?}.

    ``directives`` is the per-row INTEGER count (matches the migration's
    column type); the breaker coerces it to ``[]`` when feeding the
    detector, so a value of 0 is the most common shape.
    """
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
                task_class,
                r["step"],
                r["score"],
                r["anchor"],
                r.get("directives", 0),
                r.get("run_id"),
                r.get("created_at", now),
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


def _collapse_history(
    scores: list[float],
    anchors: list[float],
    directives: list[list[str]] | None = None,
) -> list[dict]:
    """Shape rows the way ``collapse_detector.detect`` expects (TEST SEAM only)."""
    if directives is None:
        directives = [[] for _ in scores]
    assert len(scores) == len(anchors) == len(directives), (
        f"history shape mismatch: {len(scores)}/{len(anchors)}/{len(directives)}"
    )
    return [
        {"step": i, "score": s, "anchor": a, "directives": list(d)}
        for i, (s, a, d) in enumerate(zip(scores, anchors, directives))
    ]


# ──────────────────────────────────────────────────────────────────────────────
# (a) Stuck / collapsing fixture → LIVENESS_TRIP / OPEN.
#     Seed rows in the ``collapse_history`` table for the run's task_class;
#     call ``check_liveness_breaker`` WITHOUT ``collapse_history=`` so the
#     production read path is exercised end-to-end.
#     Vote contributes 0 (sparse task_runs, no stagnant artifact / verdict /
#     cost history) → collapse is the SOLE trip cause → fired_count == 0
#     and the independent-trip branch fires.
# ──────────────────────────────────────────────────────────────────────────────
def test_seeded_rows_trip(db, monkeypatch):
    monkeypatch.delenv("MO_CB_COLLAPSE", raising=False)
    now = _now()
    task_class = "code_fix"
    # Sparse task_runs — artifact / verdict / cost all stay silent so the
    # collapse signal is the SOLE firer and ``independent_trip`` is True.
    _seed_task_runs(db, [
        {"id": "run-collapse", "task_class": task_class, "recipe": "code-fix",
         "artifact_hash": "1111" * 4, "cost_usd": 0.05, "created_at": now - 1},
    ])
    _reset_cb_state(db)
    _seed_collapse_history(db, task_class, [
        {"step": 0, "score": 0.40, "anchor": 0.80, "directives": 1},
        {"step": 1, "score": 0.50, "anchor": 0.75, "directives": 1},
        {"step": 2, "score": 0.60, "anchor": 0.60, "directives": 1},
        {"step": 3, "score": 0.70, "anchor": 0.50, "directives": 1},
    ])

    obj, rc = cb.check_liveness_breaker("run-collapse", db=db, policy="or")
    assert rc == 1
    assert obj["verdict"] == "LIVENESS_TRIP"
    assert obj["state"] == "OPEN"
    # Top-level collapse sub-object exists and reports the trip.
    assert obj["collapse"]["fired"] is True
    assert obj["collapse"]["independent_trip"] is True
    assert obj["collapse"]["history_rows"] == 4
    # Vote stayed clean — collapse is the only cause.
    assert obj["fired_count"] == 0
    assert obj["signal_count"] == 3
    assert obj["signals"]["artifact_invariant"]["fired"] is False
    assert obj["signals"]["verdict_stuck"]["fired"] is False
    assert obj["signals"]["cost_burn_no_write"]["fired"] is False
    # No `collapse_halt` under `signals` (it would invite re-folding into the vote).
    assert "collapse_halt" not in obj["signals"]
    # Rationale names the detector's own evidence so the audit row is auditable.
    rationale = obj["collapse"]["rationale"]
    assert "halt" in rationale
    assert "score_rise=" in rationale
    assert "anchor_drop=" in rationale
    # last_reason on circuit_breaker_state pins the single collapse token.
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT last_reason FROM circuit_breaker_state "
        "WHERE scope_key='code_fix::code-fix'"
    ).fetchone()
    con.close()
    assert row is not None
    assert row[0] == "collapse_halt"


# ──────────────────────────────────────────────────────────────────────────────
# (b) Healthy rows → no trip.
#     Score and anchor rise together — the detector returns
#     ``recommendation == "none"`` and the signal reports fired=False.
# ──────────────────────────────────────────────────────────────────────────────
def test_seeded_healthy_no_trip(db, monkeypatch):
    monkeypatch.delenv("MO_CB_COLLAPSE", raising=False)
    now = _now()
    task_class = "code_fix"
    _seed_task_runs(db, [
        {"id": "run-healthy", "task_class": task_class, "recipe": "code-fix",
         "artifact_hash": "9999" * 4, "cost_usd": 0.1, "created_at": now - 1},
    ])
    _reset_cb_state(db)
    _seed_collapse_history(db, task_class, [
        {"step": 0, "score": 0.40, "anchor": 0.40, "directives": 0},
        {"step": 1, "score": 0.50, "anchor": 0.50, "directives": 0},
        {"step": 2, "score": 0.60, "anchor": 0.60, "directives": 0},
        {"step": 3, "score": 0.70, "anchor": 0.70, "directives": 0},
    ])
    obj, rc = cb.check_liveness_breaker("run-healthy", db=db)
    assert rc == 0
    assert obj["verdict"] == "PROCEED"
    assert obj["state"] == "CLOSED"
    assert obj["collapse"]["fired"] is False
    assert obj["collapse"]["independent_trip"] is False
    assert obj["collapse"]["history_rows"] == 4
    assert obj["fired_count"] == 0
    assert obj["signal_count"] == 3
    # Rationale reports the actual detector verdict — not a blanket "no fire".
    rationale = obj["collapse"]["rationale"]
    assert "recommendation='none'" in rationale or '"none"' in rationale


# ──────────────────────────────────────────────────────────────────────────────
# (c) Missing ``collapse_history`` table → fail-open, no trip.
#     Use a DB initialised on a checkout where the migration file is renamed
#     (avoids touching the repo's live migration set). The breaker must NOT
#     crash and must NOT trip.
# ──────────────────────────────────────────────────────────────────────────────
def test_missing_table_fail_open(monkeypatch, tmp_path):
    """Stash 0060 out of db/migrations/, init_db, then expect no-trip.

    Uses ``shutil.move`` for cross-FS safety (the test tmp_path is on
    /var/folders while the repo lives on /Volumes). The fixture-scoped
    ``db`` is not used: this test needs a DB without migration 0060
    applied, which is impossible to construct via the fixture.
    """
    monkeypatch.delenv("MO_CB_COLLAPSE", raising=False)
    mig_dir = REPO / "db" / "migrations"
    target = mig_dir / "0060_collapse_history.sql"
    stash = mig_dir / "0060_collapse_history.sql.stash"
    if target.exists():
        shutil.move(str(target), str(stash))
        renamed = True
    else:
        renamed = False
    try:
        # Fresh DB without the migration applied.
        db_dir = tmp_path / "db-no-mig"
        db_dir.mkdir()
        dbp = str(db_dir / "state.db")
        rc, out, err = mig.init_db(db=dbp, root=str(REPO))
        assert rc == 0, f"init_db failed:\n{out}\n{err}"

        now = _now()
        _seed_task_runs(dbp, [
            {"id": "run-no-table", "task_class": "code_fix", "recipe": "code-fix",
             "artifact_hash": "dddd" * 4, "cost_usd": 0.05,
             "created_at": now - 1},
        ])
        _reset_cb_state(dbp)

        obj, rc = cb.check_liveness_breaker(
            "run-no-table", db=dbp, policy="or",
        )
        assert rc == 0
        assert obj["verdict"] == "PROCEED"
        assert obj["collapse"]["fired"] is False
        assert obj["collapse"]["history_rows"] == 0
        # Rationale mentions "missing" so an operator can see fail-open.
        assert "missing" in obj["collapse"]["rationale"].lower()
    finally:
        if renamed:
            shutil.move(str(stash), str(target))


# ──────────────────────────────────────────────────────────────────────────────
# (d) MO_CB_COLLAPSE=0 → collapse signal is OFF, others still run.
#     The audit row must record ``enabled=False`` so downstream consumers
#     can distinguish "detector said none" from "detector was silenced".
#     Even with a halting history the breaker must not trip via collapse.
# ──────────────────────────────────────────────────────────────────────────────
def test_mo_cb_collapse_zero_disables_signal(db, monkeypatch):
    monkeypatch.setenv("MO_CB_COLLAPSE", "0")
    now = _now()
    task_class = "code_fix"
    _seed_task_runs(db, [
        {"id": "run-disabled", "task_class": task_class, "recipe": "code-fix",
         "artifact_hash": "aaaa" * 4, "cost_usd": 0.05, "created_at": now - 1},
    ])
    _reset_cb_state(db)
    _seed_collapse_history(db, task_class, [
        {"step": 0, "score": 0.40, "anchor": 0.80, "directives": 1},
        {"step": 1, "score": 0.50, "anchor": 0.75, "directives": 1},
        {"step": 2, "score": 0.60, "anchor": 0.60, "directives": 1},
        {"step": 3, "score": 0.70, "anchor": 0.50, "directives": 1},
    ])
    obj, rc = cb.check_liveness_breaker("run-disabled", db=db)
    assert rc == 0
    assert obj["verdict"] == "PROCEED"
    assert obj["collapse"]["fired"] is False
    assert obj["collapse"]["enabled"] is False
    # Rationale states the opt-out so the audit log is honest.
    assert "MO_CB_COLLAPSE=0" in obj["collapse"]["rationale"]
    # Other signals still ran (and stayed silent on this fixture).
    assert obj["signals"]["artifact_invariant"]["fired"] is False
    assert obj["signals"]["verdict_stuck"]["fired"] is False
    assert obj["signals"]["cost_burn_no_write"]["fired"] is False


# ──────────────────────────────────────────────────────────────────────────────
# (e) Collapse trips under ``policy="and"`` with zero stagnation signals.
#     This is the kickoff's load-bearing independence test: a 4th
#     stagnation signal cannot fire (only 1 task_run exists, sparse), so
#     ``fired_count < signal_count`` under ``and`` and the vote stays
#     CLOSED. The independent-trip branch MUST fire.
# ──────────────────────────────────────────────────────────────────────────────
def test_collapse_trips_under_and(db, monkeypatch):
    monkeypatch.delenv("MO_CB_COLLAPSE", raising=False)
    now = _now()
    task_class = "code_fix"
    # Sparse task_runs → artifact / verdict / cost cannot fire.
    _seed_task_runs(db, [
        {"id": "run-and-collapse", "task_class": task_class,
         "recipe": "code-fix",
         "artifact_hash": "eeee" * 4, "cost_usd": 0.05,
         "created_at": now - 1},
    ])
    _reset_cb_state(db)
    _seed_collapse_history(db, task_class, [
        {"step": 0, "score": 0.40, "anchor": 0.80, "directives": 1},
        {"step": 1, "score": 0.50, "anchor": 0.75, "directives": 1},
        {"step": 2, "score": 0.60, "anchor": 0.60, "directives": 1},
        {"step": 3, "score": 0.70, "anchor": 0.50, "directives": 1},
    ])
    obj, rc = cb.check_liveness_breaker(
        "run-and-collapse", db=db, policy="and",
    )
    assert rc == 1
    assert obj["verdict"] == "LIVENESS_TRIP"
    assert obj["state"] == "OPEN"
    # Vote contributes zero (sparse fixture).
    assert obj["fired_count"] == 0
    assert obj["signal_count"] == 3
    # Collapse carries it independently.
    assert obj["collapse"]["fired"] is True
    assert obj["collapse"]["independent_trip"] is True


# ──────────────────────────────────────────────────────────────────────────────
# (f) Test-seam trip via the synthetic ``collapse_history=`` kwarg.
#     Confirms the unit-test seam still works after the read-path rewrite —
#     a refactor that breaks the detector integration is caught even when
#     the DB read path is unchanged.
# ──────────────────────────────────────────────────────────────────────────────
def test_collapse_history_trips_breaker(db, monkeypatch):
    monkeypatch.delenv("MO_CB_COLLAPSE", raising=False)
    now = _now()
    _seed_task_runs(db, [
        {"id": "run-collapse-seam", "task_class": "code_fix",
         "recipe": "code-fix",
         "artifact_hash": "1111" * 4, "cost_usd": 0.05,
         "created_at": now - 1},
    ])
    _reset_cb_state(db)
    history = _collapse_history(
        scores=[0.40, 0.50, 0.60, 0.70],
        anchors=[0.80, 0.75, 0.60, 0.50],
        directives=[["a"], ["b"], ["c"], ["d"]],
    )
    obj, rc = cb.check_liveness_breaker(
        "run-collapse-seam", db=db, policy="or", collapse_history=history,
    )
    assert rc == 1
    assert obj["verdict"] == "LIVENESS_TRIP"
    assert obj["state"] == "OPEN"
    # Vote contributes zero (sparse fixture); collapse is the sole cause.
    assert obj["fired_count"] == 0
    assert obj["signal_count"] == 3
    assert obj["collapse"]["fired"] is True
    assert obj["collapse"]["independent_trip"] is True
    assert obj["signals"]["artifact_invariant"]["fired"] is False
    assert obj["signals"]["verdict_stuck"]["fired"] is False
    assert obj["signals"]["cost_burn_no_write"]["fired"] is False
    # Rationale names the detector's own evidence.
    rationale = obj["collapse"]["rationale"]
    assert "halt" in rationale
    assert "score_rise=" in rationale
    assert "anchor_drop=" in rationale
    # last_reason on circuit_breaker_state pins the single collapse token.
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT last_reason FROM circuit_breaker_state "
        "WHERE scope_key='code_fix::code-fix'"
    ).fetchone()
    con.close()
    assert row is not None
    assert row[0] == "collapse_halt"


# ──────────────────────────────────────────────────────────────────────────────
# (g) Test-seam healthy history → no trip.
# ──────────────────────────────────────────────────────────────────────────────
def test_healthy_history_does_not_trip(db, monkeypatch):
    monkeypatch.delenv("MO_CB_COLLAPSE", raising=False)
    now = _now()
    _seed_task_runs(db, [
        {"id": "run-healthy-seam", "task_class": "code_fix",
         "recipe": "code-fix",
         "artifact_hash": "9999" * 4, "cost_usd": 0.1,
         "created_at": now - 1},
    ])
    _reset_cb_state(db)
    history = _collapse_history(
        scores=[0.40, 0.50, 0.60, 0.70],
        anchors=[0.40, 0.50, 0.60, 0.70],
    )
    obj, rc = cb.check_liveness_breaker(
        "run-healthy-seam", db=db, collapse_history=history,
    )
    assert rc == 0
    assert obj["verdict"] == "PROCEED"
    assert obj["state"] == "CLOSED"
    assert obj["collapse"]["fired"] is False
    assert obj["fired_count"] == 0
    assert obj["signal_count"] == 3
    rationale = obj["collapse"]["rationale"]
    assert "recommendation='none'" in rationale or '"none"' in rationale


# ──────────────────────────────────────────────────────────────────────────────
# (h) Detector raising on malformed rows → no trip (fail-open contract).
# ──────────────────────────────────────────────────────────────────────────────
def test_detector_raising_does_not_trip(db, monkeypatch):
    """Malformed history rows make ``detect`` raise — signal must fail-open."""
    monkeypatch.delenv("MO_CB_COLLAPSE", raising=False)
    now = _now()
    _seed_task_runs(db, [
        {"id": "run-raise", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "cccc" * 4, "cost_usd": 0.1, "created_at": now - 1},
    ])
    _reset_cb_state(db)
    # Missing "score" key triggers KeyError inside detect.
    bad_history = [{"step": 0, "anchor": 0.5, "directives": []}]
    obj, rc = cb.check_liveness_breaker(
        "run-raise", db=db, collapse_history=bad_history,
    )
    assert rc == 0
    assert obj["verdict"] == "PROCEED"
    assert obj["state"] == "CLOSED"
    assert obj["collapse"]["fired"] is False
    # Rationale must surface the exception class so an operator can diagnose.
    assert "KeyError" in obj["collapse"]["rationale"] or "raised" in obj["collapse"]["rationale"]
    assert obj["fired_count"] == 0