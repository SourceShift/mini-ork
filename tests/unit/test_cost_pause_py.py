"""Parity gate: mini_ork.ported.cost_pause vs lib/cost_pause.sh.

Same spend sequence through the LIVE bash functions and the Python port in
separate run-dirs; rc, sentinel presence, accumulated spend, and status JSON
must match at every step.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import cost_pause as cp  # noqa: E402

CP_SH = REPO / "lib" / "cost_pause.sh"


def _bash_check(run_dir, run_id, delta):
    r = subprocess.run(
        ["bash", "-c", f'. "{CP_SH}" && mo_cost_pause_check "$1" "$2"',
         "_", run_id, str(delta)],
        env={**os.environ, "MINI_ORK_RUN_DIR": str(run_dir),
             "MO_PAUSE_EVERY_USD": "10"},
        capture_output=True, text=True)
    return r.returncode


def _bash_status(run_dir, run_id):
    r = subprocess.run(
        ["bash", "-c", f'. "{CP_SH}" && mo_cost_pause_status "$1"', "_", run_id],
        env={**os.environ, "MINI_ORK_RUN_DIR": str(run_dir),
             "MO_PAUSE_EVERY_USD": "10"},
        capture_output=True, text=True)
    return json.loads(r.stdout.strip())


def test_spend_sequence_parity(tmp_path, monkeypatch):
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    bash_dir.mkdir()
    py_dir.mkdir()
    monkeypatch.setenv("MO_PAUSE_EVERY_USD", "10")

    # Sequence: 5.00 (no pause) -> 8.00 (crosses $10 -> pause) -> 1.00 (no new window)
    for delta, expect_rc in [(5.00, 0), (8.00, 2), (1.00, 0)]:
        monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
        rc_py = cp.check("t-run", delta)
        rc_bash = _bash_check(bash_dir, "t-run", delta)
        assert rc_py == rc_bash == expect_rc, f"delta={delta}"

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    s_py = cp.status("t-run")
    s_bash = _bash_status(bash_dir, "t-run")
    assert s_py["paused"] is True and s_bash["paused"] is True
    assert abs(s_py["spent_usd"] - s_bash["spent_usd"]) < 1e-9
    assert s_py["spent_usd"] == 14.0
    assert (py_dir / ".cost-pause").is_file() and (bash_dir / ".cost-pause").is_file()


def test_no_run_id_is_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    assert cp.check("", 1.0) == 2
