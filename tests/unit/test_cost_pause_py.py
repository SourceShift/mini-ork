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
