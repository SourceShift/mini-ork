"""Unit tests for mini_ork.learning.calibration_backtest.

Hermetic: a temp SQLite execution_traces built by hand for the pure functions,
and the real migrated schema (init_db) for the trace_write round-trip. No lane,
no network, no real run.

The editable install resolves mini_ork to MAIN, so insert the repo root on
sys.path before importing.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import trace_store  # noqa: E402
from mini_ork.dispatch import calibration  # noqa: E402
from mini_ork.learning import calibration_backtest as cb  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402


def _init_prediction_db(path: Path, rows) -> str:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE execution_traces ("
                "trace_id TEXT PRIMARY KEY, task_class TEXT, status TEXT, "
                "predicted_error REAL)")
    con.executemany(
        "INSERT INTO execution_traces (trace_id, task_class, status, predicted_error) "
        "VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return str(path)


def _init_no_prediction_db(path: Path) -> str:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE execution_traces ("
                "trace_id TEXT PRIMARY KEY, task_class TEXT, status TEXT)")
    con.execute("INSERT INTO execution_traces (trace_id, task_class, status) "
                "VALUES ('a','code-fix','success')")
    con.commit()
    con.close()
    return str(path)


def _init_migrated_db(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    return dbp


def test_ece_and_brier_perfect_and_worst():
    # Perfectly calibrated: every prediction is 0 or 1 and matches the outcome,
    # so predicted == observed in every non-empty bin.
    perfect = [(0.0, False)] * 5 + [(1.0, True)] * 5
    assert cb.ece(perfect, bins=10) == pytest.approx(0.0, abs=1e-9)
    assert cb.brier(perfect) == pytest.approx(0.0, abs=1e-9)
    # Worst case: predict 0.0, every row errored -> Brier == 1.0.
    worst = [(0.0, True), (0.0, True), (0.0, True)]
    assert cb.brier(worst) == pytest.approx(1.0)
    assert cb.ece([], bins=10) is None
    assert cb.brier([]) is None


def test_blind_spot_counts_kept_only():
    rows = [
        (0.05, False), (0.05, True), (0.05, False),  # 3 kept, 1 errored
        (0.90, True), (0.90, True),                  # 2 escalated, 2 errored
    ]
    bs = cb.blind_spot(rows, target=0.15)
    assert bs["n_kept"] == 3
    assert bs["n_kept_errored"] == 1
    assert bs["blind_spot_rate"] == pytest.approx(1 / 3)
    assert bs["n_escalated"] == 2
    assert bs["n_escalated_errored"] == 2


def test_blind_spot_none_when_no_kept():
    rows = [(0.9, True), (0.9, False)]
    bs = cb.blind_spot(rows, target=0.15)
    assert bs["n_kept"] == 0
    assert bs["blind_spot_rate"] is None  # not 0.0


def test_blind_spot_boundary_inclusive():
    rows = [(0.15, True), (0.15, False)]  # predicted_error == target -> kept
    bs = cb.blind_spot(rows, target=0.15)
    assert bs["n_kept"] == 2
    assert bs["n_escalated"] == 0
    bs2 = cb.blind_spot(rows, target=0.15, include_boundary=False)
    assert bs2["n_kept"] == 0
    assert bs2["n_escalated"] == 2


def test_reliability_omits_empty_bins_and_monotone():
    rows = [(0.05, False), (0.25, False), (0.55, False), (0.85, False)]
    rel = cb.reliability(rows, bins=10)
    # Bins 1, 3, 4, 6, 7, 9 are empty and omitted; every emitted bin has n > 0.
    assert len(rel) == 4
    assert all(b["n"] > 0 for b in rel)
    means = [b["mean_predicted"] for b in rel]
    assert means == sorted(means)  # non-decreasing across emitted bins


def test_load_prediction_rows_missing_column(tmp_path):
    db = _init_no_prediction_db(tmp_path / "nocol.db")
    assert cb.load_prediction_rows(db) == []
    assert cb.load_prediction_rows(db, task_class="code-fix") == []


def test_load_prediction_rows_task_class_filter(tmp_path):
    db = _init_prediction_db(tmp_path / "filter.db", [
        ("a", "code-fix", "success", 0.1),
        ("b", "book-gen", "failure", 0.3),
        ("c", "book-gen", "success", 0.2),
    ])
    assert len(cb.load_prediction_rows(db)) == 3
    rows = sorted(cb.load_prediction_rows(db, task_class="book-gen"))
    assert rows == [(0.2, False), (0.3, True)]


def test_summarize_target_and_blind_spot_agree(tmp_path):
    db = _init_prediction_db(tmp_path / "sum.db", [
        ("a", "code-fix", "success", 0.05),
        ("b", "code-fix", "failure", 0.05),
        ("c", "code-fix", "failure", 0.9),
    ])
    s = cb.summarize(db, "code-fix", bins=10)
    assert s["target"] == calibration.target_error()
    rows = cb.load_prediction_rows(db, "code-fix")
    assert s["blind_spot"] == cb.blind_spot(rows, s["target"])
    assert s["n"] == len(rows)


def test_predicted_error_roundtrip(tmp_path):
    """predicted_error survives write and is preserved by a re-write that omits
    it — the COALESCE, mirroring test_route_margin_roundtrip."""
    db = _init_migrated_db(tmp_path / "rt-home")
    trace_store.trace_write({
        "trace_id": "tr-pred", "task_class": "code-fix", "status": "success",
        "agent_version_id": "glm_lens",
        "route_source": "learned", "route_explore": False,
        "route_margin": 0.1875, "predicted_error": 0.42,
    }, db=db)
    row = trace_store.trace_get("tr-pred", db=db)
    assert row["predicted_error"] == pytest.approx(0.42)

    # Re-write with no prediction leaves the recorded value alone (COALESCE).
    trace_store.trace_write(
        {"trace_id": "tr-pred", "task_class": "code-fix", "status": "success"}, db=db)
    row = trace_store.trace_get("tr-pred", db=db)
    assert row["predicted_error"] == pytest.approx(0.42)
