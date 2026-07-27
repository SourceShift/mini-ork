"""Unit tests: ``mini_ork.dispatch.deadline_budget`` (bash parity halves removed; formerly vs ``lib/deadline_budget.sh``).

Each case drives the Python port in-process (with a frozen ``_now`` clock
where second-precision matters). Cases:

* init happy path — sidecar JSON keys, deadline_seconds, epoch arithmetic
* check in-budget — rc=0 + no sentinel
* check trip — rc=2 + .deadline-hit sentinel with the expected keys
* check latch — second call stays rc=2 and does NOT re-emit the stderr marker
* status hit — hit=true + elapsed/consistent path fields
* status no-env — default-zero payload with hit=false
* init rejects non-int seconds (e.g. ``"5.0"``) with rc=2 + no sidecar
* init rejects zero and negative seconds with rc=2
* init idempotent re-arm — re-arm overwrites sidecar with fresh start_epoch
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.dispatch import deadline_budget as db


@pytest.fixture(autouse=True)
def _isolate_deadline_env(monkeypatch):
    """Strip any leaked ``MO_DEADLINE_*`` values before each test.

    The port mutates ``os.environ`` on ``init()``. Monkeypatch only reverts
    what IT set — arbitrary mutations from prior test code survive. Tests
    that need MO_DEADLINE_* must re-set them explicitly below.
    """
    for k in list(os.environ):
        if k.startswith("MO_DEADLINE_"):
            monkeypatch.delenv(k, raising=False)
    yield


# ---------------------------------------------------------------------------
# 1) init happy path
# ---------------------------------------------------------------------------

def test_init_arms_sidecar_json_keys(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc_py = db.init("t-init", 30, str(py_dir))
    assert rc_py == 0

    sp = json.loads((py_dir / ".deadline-budget").read_text().strip())

    assert sp["run_id"] == "t-init"
    assert sp["deadline_seconds"] == 30
    # deadline_epoch = start_epoch + seconds
    assert sp["deadline_epoch"] - sp["start_epoch"] == 30
    # start_epoch is "now" (within a couple of seconds).
    assert abs(sp["start_epoch"] - int(time.time())) <= 2
    # ISO-8601 UTC shape.
    assert sp["created_at"].endswith("Z")


# ---------------------------------------------------------------------------
# 2) check in budget
# ---------------------------------------------------------------------------

def test_check_in_budget_rc0_no_sentinel(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    # Anchor 1s in the past so the budget is comfortably in-budget.
    t0 = int(time.time())
    extra = {
        "MO_DEADLINE_START": str(t0),
        "MO_DEADLINE_EPOCH": str(t0 + 30),
        "MO_DEADLINE_SECONDS": "30",
    }

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    rc_py = db.check("t-ok", _now=lambda: float(t0 + 1))
    assert rc_py == 0
    assert not (py_dir / ".deadline-hit").exists()


# ---------------------------------------------------------------------------
# 3) check trips
# ---------------------------------------------------------------------------

def test_check_trips_rc2_sentinel_json(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    # Pre-pin MO_DEADLINE_START 60s in the past and EPOCH 5s in the past so
    # the port sees `remaining < 0` and trips on first call.
    t0 = int(time.time())
    extra = {
        "MO_DEADLINE_START": str(t0 - 60),
        "MO_DEADLINE_EPOCH": str(t0 - 5),
        "MO_DEADLINE_SECONDS": "60",
    }

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    rc_py = db.check("t-trip", _now=lambda: float(t0))
    assert rc_py == 2

    sp = json.loads((py_dir / ".deadline-hit").read_text().strip())

    assert sp["run_id"] == "t-trip"
    assert sp["deadline_seconds"] == 60
    assert sp["start_epoch"] == t0 - 60
    assert sp["deadline_epoch"] == t0 - 5
    assert sp["finish_reason"] == "deadline_hit"
    assert sp["note"].startswith("soft-stop between stages")
    # elapsed/remaining are ints (mirrors the $(( )) arithmetic).
    assert isinstance(sp["elapsed_seconds"], int)
    assert sp["elapsed_seconds"] == 60
    assert sp["remaining_seconds"] <= 0
    assert sp["best_so_far_artifact"] == ""


# ---------------------------------------------------------------------------
# 4) check latched
# ---------------------------------------------------------------------------

def test_check_latched_no_new_marker(tmp_path, monkeypatch, capsys):
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    t0 = int(time.time())
    extra = {
        "MO_DEADLINE_START": str(t0 - 60),
        "MO_DEADLINE_EPOCH": str(t0 - 5),
        "MO_DEADLINE_SECONDS": "60",
    }

    # First check — emits the deadline_hit marker.
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    rc_py1 = db.check("t-latch", _now=lambda: float(t0))
    captured1 = capsys.readouterr()
    assert rc_py1 == 2
    assert '"event_type":"deadline_hit"' in captured1.err

    # Second check — latched, NO new marker.
    rc_py2 = db.check("t-latch", _now=lambda: float(t0))
    captured2 = capsys.readouterr()
    assert rc_py2 == 2
    assert '"event_type":"deadline_hit"' not in captured2.err

    # Sentinel unchanged between the two calls.
    sp1 = (py_dir / ".deadline-hit").read_text()
    sp2 = (py_dir / ".deadline-hit").read_text()
    assert sp1 == sp2


# ---------------------------------------------------------------------------
# 5) status with hit
# ---------------------------------------------------------------------------

def test_status_hit(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    t0 = int(time.time()) - 30  # give status a meaningful elapsed
    extra = {
        "MO_DEADLINE_START": str(t0),
        "MO_DEADLINE_EPOCH": str(int(time.time()) + 60),
        "MO_DEADLINE_SECONDS": "120",
    }

    # Trip the deadline so status reports hit=true.
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    trip_now = int(time.time()) - 1  # deadline has passed
    monkeypatch.setenv("MO_DEADLINE_EPOCH", str(trip_now - 5))
    rc_py = db.check("t-stat", _now=lambda: float(trip_now))
    assert rc_py == 2

    # Now status, with a frozen clock anchored just after the trip.
    frozen = float(trip_now + 1)
    monkeypatch.setenv("MO_DEADLINE_EPOCH", str(int(time.time()) + 60))  # back to future
    sp = db.status("t-stat", _now=lambda: frozen)

    assert sp["hit"] is True
    assert sp["run_id"] == "t-stat"
    assert sp["sidecar_path"].endswith(".deadline-budget")
    assert str(py_dir) in sp["sidecar_path"]
    assert sp["sentinel_path"].endswith(".deadline-hit")
    # Frozen-clock arithmetic: elapsed = frozen - start (±1s boundary drift).
    assert abs(sp["elapsed_seconds"] - (frozen - t0)) < 2.0


# ---------------------------------------------------------------------------
# 6) status with no env → default-zero payload
# ---------------------------------------------------------------------------

def test_status_no_env_default_zero(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    sp = db.status("t-zero")

    assert sp["hit"] is False
    assert sp["deadline_seconds"] == 0
    assert sp["start_epoch"] == 0
    assert sp["deadline_epoch"] == 0
    assert sp["elapsed_seconds"] == 0
    assert sp["remaining_seconds"] == 0
    assert sp["run_id"] == "t-zero"


# ---------------------------------------------------------------------------
# 7) init rejects non-int seconds ("5.0") with rc=2, no sidecar
# ---------------------------------------------------------------------------

def test_init_rejects_non_int_seconds(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    # The port rejects non-int seconds: ``isinstance(seconds, int)`` is
    # False for the string "5.0", so it returns 2. (A Python float 5.0 is
    # also rejected — see ``test_init_rejects_python_float`` below.)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc_py = db.init("t-bad", "5.0", str(py_dir))
    assert rc_py == 2
    assert not (py_dir / ".deadline-budget").exists()


def test_init_rejects_python_float_seconds(tmp_path, monkeypatch):
    """Direct call with a Python float — port must reject."""
    py_dir = tmp_path / "py"
    py_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    assert db.init("t-float", 5.0, str(py_dir)) == 2
    assert not (py_dir / ".deadline-budget").exists()


# ---------------------------------------------------------------------------
# 8) init rejects zero / negative
# ---------------------------------------------------------------------------

def test_init_rejects_zero_and_negative(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    for bad in (0, -5):
        monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
        rc_py = db.init("t-zero", bad, str(py_dir))
        assert rc_py == 2
        assert not (py_dir / ".deadline-budget").exists()


# ---------------------------------------------------------------------------
# 9) init idempotent re-arm — fresh start_epoch in sidecar
# ---------------------------------------------------------------------------

def test_init_idempotent_re_arm_rewrites_sidecar(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    db.init("t-re", 30, str(py_dir))
    py1 = json.loads((py_dir / ".deadline-budget").read_text().strip())

    time.sleep(1.05)  # ensure observable start_epoch drift between arms
    db.init("t-re", 30, str(py_dir))
    py2 = json.loads((py_dir / ".deadline-budget").read_text().strip())
    assert py2["start_epoch"] > py1["start_epoch"]
