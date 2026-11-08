"""Unit tests: mini_ork.dispatch.cost_pause (bash parity halves removed; formerly vs lib/cost_pause.sh).

A spend sequence through the Python module in a run-dir; rc, sentinel presence,
accumulated spend, and status JSON are asserted at every step.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.dispatch import cost_pause as cp


def test_spend_sequence(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()
    monkeypatch.setenv("MO_PAUSE_EVERY_USD", "10")

    # Sequence: 5.00 (no pause) -> 8.00 (crosses $10 -> pause) -> 1.00 (no new window)
    for delta, expect_rc in [(5.00, 0), (8.00, 2), (1.00, 0)]:
        monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
        rc_py = cp.check("t-run", delta)
        assert rc_py == expect_rc, f"delta={delta}"

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    s_py = cp.status("t-run")
    assert s_py["paused"] is True
    assert s_py["spent_usd"] == 14.0
    assert (py_dir / ".cost-pause").is_file()


def test_no_run_id_is_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    assert cp.check("", 1.0) == 2


# ── reconcile: the ledger, not the last sidecar, is the meter's truth ───────


def test_reconcile_pauses_on_true_spend_a_single_sidecar_could_not_reach(
        tmp_path, monkeypatch):
    """The panel/fan-out shape: three $10 lanes, one sidecar → delta says $10.

    Charging only the sidecar leaves the run at $10 and nothing pauses; the
    ledger knows $30, which crosses a $25 threshold.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_PAUSE_EVERY_USD", "25")

    # One check, charging only the LAST lane's $10 while the run spent $30:
    only_last = cp.check("panel-run-2", 10.0)
    assert only_last == 0
    assert cp.reconcile("panel-run-2", 30.0) == 2
    assert (run_dir / ".cost-pause").is_file()
    assert cp.status("panel-run-2")["spent_usd"] == 30.0


def test_reconcile_never_lowers_the_meter(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_PAUSE_EVERY_USD", "100")
    cp.check("r", 40.0)
    assert cp.reconcile("r", 5.0) == 0
    assert cp.status("r")["spent_usd"] == 40.0


def test_reconcile_after_a_delta_does_not_spend_the_same_dollar_twice(
        tmp_path, monkeypatch):
    """delta then reconcile: the meter is the ledger total, not delta+total."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_PAUSE_EVERY_USD", "100")
    assert cp.check("r", 10.0) == 0        # sidecar just charged $10
    assert cp.reconcile("r", 10.0) == 0    # the ledger total already includes it
    assert cp.status("r")["spent_usd"] == 10.0


def test_reconcile_keeps_the_same_window_math(tmp_path, monkeypatch):
    """Crossing into a new window pauses; staying in it does not."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_PAUSE_EVERY_USD", "10")
    assert cp.reconcile("r", 9.0) == 0
    assert cp.reconcile("r", 11.0) == 2
    (run_dir / ".cost-pause").unlink()
    assert cp.reconcile("r", 14.0) == 0   # same window, already paused once
