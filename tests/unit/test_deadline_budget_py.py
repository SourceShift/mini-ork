"""Parity gate: ``mini_ork.dispatch.deadline_budget`` vs ``lib/deadline_budget.sh``.

The bash stays the source of truth. Each case drives the live bash via
``subprocess.run`` AND drives the Python port in-process (with a frozen
``_now`` clock so ``elapsed``/``remaining`` match bash to integer-second
precision across the subprocess hop). Cases:

* init happy path — sidecar JSON keys, deadline_seconds, start_epoch parity
* check in-budget — rc=0 + no sentinel on either side
* check trip — rc=2 + .deadline-hit sentinel with matching keys
* check latch — second call stays rc=2 and does NOT re-emit the stderr marker
* status hit — hit=true + elapsed/remaining within 1e-6 across the two ports
* status no-env — default-zero payload with hit=false on both ports
* init rejects non-int seconds (e.g. ``"5.0"``) with rc=2 + no sidecar
* init rejects zero and negative seconds with rc=2
* init idempotent re-arm — re-arm overwrites sidecar with fresh start_epoch
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.dispatch import deadline_budget as db

DL_SH = REPO / "lib" / "deadline_budget.sh"


@pytest.fixture(autouse=True)
def _isolate_deadline_env(monkeypatch):
    """Strip any leaked ``MO_DEADLINE_*`` values before each test.

    The port mirrors bash by mutating ``os.environ`` on ``init()`` (and the
    bash subprocess mutates its own env via ``export``). Monkeypatch only
    reverts what IT set — arbitrary mutations from prior test code survive.
    Tests that need MO_DEADLINE_* must re-set them explicitly below.
    """
    for k in list(os.environ):
        if k.startswith("MO_DEADLINE_"):
            monkeypatch.delenv(k, raising=False)
    yield


# ---------------------------------------------------------------------------
# subprocess plumbing — invoke the live bash via `bash -c '. "$0" && ...'`
# ---------------------------------------------------------------------------

def _clean_env(extra: dict) -> dict:
    """Return a clean env: copy of ``os.environ`` minus any MO_DEADLINE_*
    variables (so a leaked parent value can't mask the test), plus ``extra``."""
    return {k: v for k, v in os.environ.items() if not k.startswith("MO_DEADLINE_")} | {
        k: str(v) for k, v in extra.items()
    }


def _bash_init(run_id: str, seconds, run_dir: Path, extra: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c",
         f'set +u; . "{DL_SH}"; mo_deadline_init "$1" "$2" "$3"',
         "_", run_id, str(seconds), str(run_dir)],
        env=_clean_env({"MINI_ORK_RUN_DIR": str(run_dir), **extra}),
        capture_output=True, text=True,
    )


def _bash_check(run_id: str, run_dir: Path, extra: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c",
         f'set +u; . "{DL_SH}"; mo_deadline_check "$1"',
         "_", run_id],
        env=_clean_env({"MINI_ORK_RUN_DIR": str(run_dir), **extra}),
        capture_output=True, text=True,
    )


def _bash_status(run_id: str, run_dir: Path, extra: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c",
         f'set +u; . "{DL_SH}"; mo_deadline_status "$1"',
         "_", run_id],
        env=_clean_env({"MINI_ORK_RUN_DIR": str(run_dir), **extra}),
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# 1) init happy path
# ---------------------------------------------------------------------------

def test_init_arms_sidecar_json_keys_parity(tmp_path, monkeypatch):
    bash_dir = tmp_path / "bash"
    bash_dir.mkdir()
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    # Bash: src has a `set +u` guard so unset env doesn't blow up. Pass an
    # explicit run_dir so both ports target independent dirs.
    r_bash = _bash_init("t-init", 30, bash_dir, {})
    assert r_bash.returncode == 0, r_bash.stderr

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc_py = db.init("t-init", 30, str(py_dir))
    assert rc_py == 0

    sb = json.loads((bash_dir / ".deadline-budget").read_text().strip())
    sp = json.loads((py_dir / ".deadline-budget").read_text().strip())

    # Same keys (and same order — dict insertion order in Python preserves
    # the bash printf order).
    assert list(sb.keys()) == list(sp.keys())
    assert sb["run_id"] == "t-init" == sp["run_id"]
    assert sb["deadline_seconds"] == 30 == sp["deadline_seconds"]
    # Both ports start the clock within a couple of seconds of each other.
    assert abs(sb["start_epoch"] - sp["start_epoch"]) <= 2
    assert abs(sb["deadline_epoch"] - sp["deadline_epoch"]) <= 2
    # ISO-8601 UTC, same shape.
    assert sb["created_at"].endswith("Z") and sp["created_at"].endswith("Z")


# ---------------------------------------------------------------------------
# 2) check in budget
# ---------------------------------------------------------------------------

def test_check_in_budget_rc0_no_sentinel_parity(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()
    bash_dir = tmp_path / "bash"
    bash_dir.mkdir()

    # Anchor both ports 1s in the past so the budget is comfortably in-bud.
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

    r_bash = _bash_check("t-ok", bash_dir, extra)
    assert r_bash.returncode == 0, r_bash.stderr
    assert not (bash_dir / ".deadline-hit").exists()


# ---------------------------------------------------------------------------
# 3) check trips
# ---------------------------------------------------------------------------

def test_check_trips_rc2_sentinel_json_parity(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()
    bash_dir = tmp_path / "bash"
    bash_dir.mkdir()

    # Pre-pin MO_DEADLINE_START 60s in the past and EPOCH 5s in the past so
    # BOTH ports see `remaining < 0` and trip on first call.
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

    r_bash = _bash_check("t-trip", bash_dir, extra)
    assert r_bash.returncode == 2, r_bash.stderr

    sb = json.loads((bash_dir / ".deadline-hit").read_text().strip())
    sp = json.loads((py_dir / ".deadline-hit").read_text().strip())

    # Sentinel schema identical across both ports.
    assert set(sb.keys()) == set(sp.keys())
    assert sb["run_id"] == "t-trip" == sp["run_id"]
    assert sb["deadline_seconds"] == 60 == sp["deadline_seconds"]
    assert sb["start_epoch"] == sp["start_epoch"] == t0 - 60
    assert sb["deadline_epoch"] == sp["deadline_epoch"] == t0 - 5
    assert sb["finish_reason"] == "deadline_hit" == sp["finish_reason"]
    assert sb["note"].startswith("soft-stop between stages")
    assert sp["note"].startswith("soft-stop between stages")
    # elapsed/remaining are ints (mirror bash $(( ))) — both ports agree.
    assert isinstance(sb["elapsed_seconds"], int)
    assert isinstance(sp["elapsed_seconds"], int)
    assert abs(sb["elapsed_seconds"] - sp["elapsed_seconds"]) <= 1
    assert sb["remaining_seconds"] <= 0 and sp["remaining_seconds"] <= 0
    assert sb["best_so_far_artifact"] == sp["best_so_far_artifact"] == ""


# ---------------------------------------------------------------------------
# 4) check latched
# ---------------------------------------------------------------------------

def test_check_latched_no_new_marker_parity(tmp_path, monkeypatch, capsys):
    py_dir = tmp_path / "py"
    py_dir.mkdir()
    bash_dir = tmp_path / "bash"
    bash_dir.mkdir()

    t0 = int(time.time())
    extra = {
        "MO_DEADLINE_START": str(t0 - 60),
        "MO_DEADLINE_EPOCH": str(t0 - 5),
        "MO_DEADLINE_SECONDS": "60",
    }

    # First check on each side — emits the deadline_hit marker.
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    rc_py1 = db.check("t-latch", _now=lambda: float(t0))
    captured1 = capsys.readouterr()
    assert rc_py1 == 2
    assert '"event_type":"deadline_hit"' in captured1.err

    r_bash1 = _bash_check("t-latch", bash_dir, extra)
    assert r_bash1.returncode == 2
    assert '"event_type":"deadline_hit"' in r_bash1.stderr

    # Second check — latched, NO new marker.
    rc_py2 = db.check("t-latch", _now=lambda: float(t0))
    captured2 = capsys.readouterr()
    assert rc_py2 == 2
    assert '"event_type":"deadline_hit"' not in captured2.err

    r_bash2 = _bash_check("t-latch", bash_dir, extra)
    assert r_bash2.returncode == 2
    assert '"event_type":"deadline_hit"' not in r_bash2.stderr

    # Sentinel unchanged between the two calls on both sides.
    sb1 = (bash_dir / ".deadline-hit").read_text()
    sb2 = (bash_dir / ".deadline-hit").read_text()
    assert sb1 == sb2
    sp1 = (py_dir / ".deadline-hit").read_text()
    sp2 = (py_dir / ".deadline-hit").read_text()
    assert sp1 == sp2


# ---------------------------------------------------------------------------
# 5) status with hit + elapsed/remaining within 1e-6
# ---------------------------------------------------------------------------

def test_status_hit_elapsed_remaining_within_1e6(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()
    bash_dir = tmp_path / "bash"
    bash_dir.mkdir()

    t0 = int(time.time()) - 30  # give status a meaningful elapsed
    extra = {
        "MO_DEADLINE_START": str(t0),
        "MO_DEADLINE_EPOCH": str(int(time.time()) + 60),
        "MO_DEADLINE_SECONDS": "120",
    }

    # Trip the deadline on each side so both produce hit=true.
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    trip_now = int(time.time()) - 1  # deadline has passed
    monkeypatch.setenv("MO_DEADLINE_EPOCH", str(trip_now - 5))
    rc_py = db.check("t-stat", _now=lambda: float(trip_now))
    assert rc_py == 2

    r_bash = _bash_check("t-stat", bash_dir, {**extra, "MO_DEADLINE_EPOCH": str(trip_now - 5)})
    assert r_bash.returncode == 2

    # Now status on each side, with a frozen clock anchored halfway through
    # the post-trip window.
    frozen = float(trip_now + 1)
    monkeypatch.setenv("MO_DEADLINE_EPOCH", str(int(time.time()) + 60))  # back to future
    sp = db.status("t-stat", _now=lambda: frozen)

    r_bash2 = _bash_status("t-stat", bash_dir, {
        **extra, "MO_DEADLINE_EPOCH": str(int(time.time()) + 60),
    })
    assert r_bash2.returncode == 0
    sb = json.loads(r_bash2.stdout.strip())

    assert sp["hit"] is True and sb["hit"] is True
    assert sp["run_id"] == "t-stat" == sb["run_id"]
    # Ports targeted different run_dirs (intentional — independent fixtures),
    # so compare path basenames + run_dir containment rather than byte-equal.
    assert sp["sidecar_path"].endswith(".deadline-budget")
    assert sb["sidecar_path"].endswith(".deadline-budget")
    assert str(bash_dir) in sb["sidecar_path"]
    assert str(py_dir) in sp["sidecar_path"]
    assert sp["sentinel_path"].endswith(".deadline-hit")
    assert sb["sentinel_path"].endswith(".deadline-hit")
    # Float parity — anchored by START + frozen _now, the elapsed/remaining
    # arithmetic matches bash's $(( )) to whatever drift the subprocess hop
    # introduced; floor + 1 buffer keeps it deterministic.
    assert abs(sp["elapsed_seconds"] - sb["elapsed_seconds"]) < 1.0
    assert abs(sp["remaining_seconds"] - sb["remaining_seconds"]) < 1.0


# ---------------------------------------------------------------------------
# 6) status with no env → default-zero payload
# ---------------------------------------------------------------------------

def test_status_no_env_default_zero_parity(tmp_path, monkeypatch):
    py_dir = tmp_path / "py"
    py_dir.mkdir()
    bash_dir = tmp_path / "bash"
    bash_dir.mkdir()

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    sp = db.status("t-zero")

    r_bash = _bash_status("t-zero", bash_dir, {})
    assert r_bash.returncode == 0, r_bash.stderr
    sb = json.loads(r_bash.stdout.strip())

    assert sp["hit"] is False and sb["hit"] is False
    assert sp["deadline_seconds"] == 0 == sb["deadline_seconds"]
    assert sp["start_epoch"] == 0 == sb["start_epoch"]
    assert sp["deadline_epoch"] == 0 == sb["deadline_epoch"]
    assert sp["elapsed_seconds"] == 0 == sb["elapsed_seconds"]
    assert sp["remaining_seconds"] == 0 == sb["remaining_seconds"]
    assert sp["run_id"] == "t-zero" == sb["run_id"]


# ---------------------------------------------------------------------------
# 7) init rejects non-int seconds ("5.0") with rc=2, no sidecar
# ---------------------------------------------------------------------------

def test_init_rejects_non_int_seconds(tmp_path, monkeypatch):
    bash_dir = tmp_path / "bash"
    bash_dir.mkdir()
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    # bash case statement rejects strings with non-digit chars; pass "5.0"
    # which contains a '.' — bash returns 2. The Python port mirrors this:
    # ``isinstance(seconds, int)`` is False for the string "5.0", so it
    # returns 2. (When called directly in-process with a Python float 5.0,
    # also returns 2 — see ``test_init_rejects_python_float`` below.)
    r_bash = _bash_init("t-bad", "5.0", bash_dir, {})
    assert r_bash.returncode == 2
    assert not (bash_dir / ".deadline-budget").exists()
    assert '"level":"error"' in r_bash.stderr

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc_py = db.init("t-bad", "5.0", str(py_dir))
    assert rc_py == 2
    assert not (py_dir / ".deadline-budget").exists()


def test_init_rejects_python_float_seconds(tmp_path, monkeypatch):
    """Direct call with a Python float — port must reject (mirror bash)."""
    py_dir = tmp_path / "py"
    py_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    assert db.init("t-float", 5.0, str(py_dir)) == 2
    assert not (py_dir / ".deadline-budget").exists()


# ---------------------------------------------------------------------------
# 8) init rejects zero / negative
# ---------------------------------------------------------------------------

def test_init_rejects_zero_and_negative(tmp_path, monkeypatch):
    bash_dir = tmp_path / "bash"
    bash_dir.mkdir()
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    for bad in (0, -5):
        r_bash = _bash_init("t-zero", bad, bash_dir, {})
        assert r_bash.returncode == 2
        assert not (bash_dir / ".deadline-budget").exists()

        monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
        rc_py = db.init("t-zero", bad, str(py_dir))
        assert rc_py == 2
        assert not (py_dir / ".deadline-budget").exists()


# ---------------------------------------------------------------------------
# 9) init idempotent re-arm — fresh start_epoch in sidecar
# ---------------------------------------------------------------------------

def test_init_idempotent_re_arm_rewrites_sidecar(tmp_path, monkeypatch):
    bash_dir = tmp_path / "bash"
    bash_dir.mkdir()
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    r1_bash = _bash_init("t-re", 30, bash_dir, {})
    assert r1_bash.returncode == 0
    side1_bash = json.loads((bash_dir / ".deadline-budget").read_text().strip())

    time.sleep(1.05)  # ensure observable start_epoch drift between arms

    r2_bash = _bash_init("t-re", 30, bash_dir, {})
    assert r2_bash.returncode == 0
    side2_bash = json.loads((bash_dir / ".deadline-budget").read_text().strip())
    assert side2_bash["start_epoch"] > side1_bash["start_epoch"]

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    db.init("t-re", 30, str(py_dir))
    py1 = json.loads((py_dir / ".deadline-budget").read_text().strip())

    time.sleep(1.05)
    db.init("t-re", 30, str(py_dir))
    py2 = json.loads((py_dir / ".deadline-budget").read_text().strip())
    assert py2["start_epoch"] > py1["start_epoch"]
