"""Parity gate: mini_ork.ported.adaptive_stability vs lib/adaptive_stability.sh.

Each test drives the LIVE bash function ``mo_check_panel_stability`` via
``bash -c 'source lib/adaptive_stability.sh; mo_check_panel_stability ...'``
against the SAME SQLite fixture as the Python port, then deep-compares the
two JSON objects: floats within 1e-6, everything else exact-equal. No mocks,
no hardcoded outputs beyond what bash itself emits.

The bash function reads its knobs from the environment
(MO_PANEL_STABILITY_THRESHOLD / MO_PANEL_MIN_ROUNDS / MO_PANEL_MAX_ROUNDS);
the Python port takes them as explicit kwargs. ``_compare`` exports the same
values to the bash subprocess that it passes to the Python call, so both
sides honour identical knobs.

Seven cases (above the kickoff's >=6 floor):
  (a) 3-round stable panel, round 3      → HALT, drift_below_threshold
  (b) current_round=1                    → CONTINUE, below_min_rounds
  (c) MO_PANEL_MAX_ROUNDS=3 at round 3   → HALT, max_rounds_reached
  (d) unencoded trace_ids                → CONTINUE, round_unencoded_default_continue
  (e) 2-round panel, threshold=0.5, rd 2 → HALT, verdict_drift ≈ 0.3333
  (f) disjoint-panel rounds              → drift_per_round = [1.0]
  (g) no traces at all                   → CONTINUE, no_traces_default_continue
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import trace_store  # noqa: E402
from mini_ork.ported import adaptive_stability as ast  # noqa: E402

SH = REPO / "lib" / "adaptive_stability.sh"

_FLOAT_TOL = 1e-6


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH (required by lib/adaptive_stability.sh)")
    if not SH.exists():
        pytest.skip(f"missing lib/adaptive_stability.sh at {SH}")


@pytest.fixture
def db(tmp_path_factory):
    """Bootstrap a fresh DB via db/init.sh (full execution_traces schema)."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _seed(db, rows):
    """rows = list of (trace_id, agent_version_id, reviewer_verdict)."""
    for trace_id, agent, verdict in rows:
        trace_store.trace_write(
            {
                "trace_id": trace_id,
                "agent_version_id": agent,
                "task_class": "panel",
                "status": "success",
                "reviewer_verdict": verdict,
            },
            db=db,
        )


def _seed_stable_panel(db, prun="run-stable"):
    """3-round panel: round 1 disagrees, round 2 kimi converges, round 3
    identical to round 2 (round 2→3 zero drift). Mirrors the bash
    self-test fixture A exactly."""
    _seed(db, [
        (f"tr-glm-r1-{prun}", "glm", "approve"),
        (f"tr-kimi-r1-{prun}", "kimi", "reject"),
        (f"tr-cdx-r1-{prun}", "codex", "reject"),
        (f"tr-glm-r2-{prun}", "glm", "approve"),
        (f"tr-kimi-r2-{prun}", "kimi", "approve"),
        (f"tr-cdx-r2-{prun}", "codex", "reject"),
        (f"tr-glm-r3-{prun}", "glm", "approve"),
        (f"tr-kimi-r3-{prun}", "kimi", "approve"),
        (f"tr-cdx-r3-{prun}", "codex", "reject"),
    ])


def _bash_check(db, panel_run_id, current_round, *,
                threshold=None, min_rounds=None, max_rounds=None):
    """Run the LIVE bash mo_check_panel_stability, return its parsed JSON."""
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db}
    if threshold is not None:
        env["MO_PANEL_STABILITY_THRESHOLD"] = str(threshold)
    if min_rounds is not None:
        env["MO_PANEL_MIN_ROUNDS"] = str(min_rounds)
    if max_rounds is not None:
        env["MO_PANEL_MAX_ROUNDS"] = str(max_rounds)
    src = (
        f'source "{SH}"\n'
        f'mo_check_panel_stability "$1" "$2"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src, "_", str(panel_run_id), str(current_round)],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        f"bash mo_check_panel_stability rc={r.returncode}\nstderr={r.stderr}"
    )
    line = r.stdout.strip().splitlines()[-1]
    return json.loads(line)


def _assert_parity(bash_obj: dict, py_obj: dict):
    """Deep-compare: same keys; floats within 1e-6; everything else equal."""
    assert set(bash_obj.keys()) == set(py_obj.keys()), (
        f"key mismatch\nbash={sorted(bash_obj)}\npy  ={sorted(py_obj)}"
    )
    for k in bash_obj:
        b, p = bash_obj[k], py_obj[k]
        # bool is a subclass of int — check it before the numeric branch.
        if isinstance(b, bool) or isinstance(p, bool):
            assert b == p, f"key {k!r}: bash={b!r} py={p!r}"
        elif isinstance(b, (int, float)) and isinstance(p, (int, float)):
            assert abs(float(b) - float(p)) <= _FLOAT_TOL, (
                f"key {k!r}: bash={b!r} py={p!r} (diff > {_FLOAT_TOL})"
            )
        elif isinstance(b, list) and isinstance(p, list):
            assert len(b) == len(p), f"key {k!r}: length {len(b)} vs {len(p)}"
            for i, (bi, pi) in enumerate(zip(b, p)):
                assert abs(float(bi) - float(pi)) <= _FLOAT_TOL, (
                    f"key {k!r}[{i}]: bash={bi!r} py={pi!r}"
                )
        else:
            assert b == p, f"key {k!r}: bash={b!r} py={p!r}"


def _compare(db, panel_run_id, current_round, *,
             threshold=None, min_rounds=None, max_rounds=None):
    """Run both sides with matching knobs, assert parity, return bash JSON."""
    bash_obj = _bash_check(
        db, panel_run_id, current_round,
        threshold=threshold, min_rounds=min_rounds, max_rounds=max_rounds,
    )
    kwargs = {}
    if threshold is not None:
        kwargs["threshold"] = float(threshold)
    if min_rounds is not None:
        kwargs["min_rounds"] = int(min_rounds)
    if max_rounds is not None:
        kwargs["max_rounds"] = int(max_rounds)
    py_obj = ast.check_panel_stability(panel_run_id, current_round, db=db, **kwargs)
    _assert_parity(bash_obj, py_obj)
    return bash_obj


# ─────────────────────────────────────────────────────────────────────────────
# (a) 3-round stable panel at round 3 → HALT, drift_below_threshold
# ─────────────────────────────────────────────────────────────────────────────
def test_stable_panel_halts_parity(db):
    _which_bash()
    _seed_stable_panel(db)
    obj = _compare(db, "run-stable", 3)
    assert obj["recommendation"] == "HALT"
    assert obj["reason"] == "drift_below_threshold"
    assert obj["stable"] is True
    # round 1→2 drift = 1/3, round 2→3 = 0.0
    assert obj["drift_history"] == [0.3333, 0.0]
    assert obj["verdict_drift"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# (b) round 1 below min_rounds → CONTINUE, below_min_rounds
# ─────────────────────────────────────────────────────────────────────────────
def test_below_min_rounds_continues_parity(db):
    _which_bash()
    _seed_stable_panel(db)
    obj = _compare(db, "run-stable", 1)
    assert obj["recommendation"] == "CONTINUE"
    assert obj["reason"] == "below_min_rounds"
    assert obj["stable"] is False


# ─────────────────────────────────────────────────────────────────────────────
# (c) max_rounds reached → unconditional HALT
# ─────────────────────────────────────────────────────────────────────────────
def test_max_rounds_halts_parity(db):
    _which_bash()
    _seed_stable_panel(db)
    obj = _compare(db, "run-stable", 3, max_rounds=3)
    assert obj["recommendation"] == "HALT"
    assert obj["reason"] == "max_rounds_reached"


# ─────────────────────────────────────────────────────────────────────────────
# (d) unencoded trace_ids → fail-open CONTINUE, round_unencoded_default_continue
# ─────────────────────────────────────────────────────────────────────────────
def test_round_unencoded_failopen_parity(db):
    _which_bash()
    _seed(db, [("tr-glm-run-noround-123", "glm", "approve")])
    obj = _compare(db, "run-noround", 3)
    assert obj["recommendation"] == "CONTINUE"
    assert obj["reason"] == "round_unencoded_default_continue"
    assert obj["verdict_drift"] == 1.0
    assert obj["rounds_seen"] == 0
    # fail-open branch omits drift_history entirely
    assert "drift_history" not in obj


# ─────────────────────────────────────────────────────────────────────────────
# (e) threshold tunable: 2-round panel, drift=1/3, threshold=0.5 → HALT
#     NOTE: the bash function buckets ALL rounds present in the DB (it does
#     not filter by current_round), so last_drift is the drift of the final
#     bucketed round. To exercise a non-zero HALT drift we seed ONLY rounds
#     1 and 2 (round 1→2 drift = 1 changed / 3 common = 0.3333).
# ─────────────────────────────────────────────────────────────────────────────
def test_threshold_tunable_parity(db):
    _which_bash()
    _seed(db, [
        ("tr-glm-r1-run-two", "glm", "approve"),
        ("tr-kimi-r1-run-two", "kimi", "reject"),
        ("tr-cdx-r1-run-two", "codex", "reject"),
        ("tr-glm-r2-run-two", "glm", "approve"),
        ("tr-kimi-r2-run-two", "kimi", "approve"),
        ("tr-cdx-r2-run-two", "codex", "reject"),
    ])
    obj = _compare(db, "run-two", 2, threshold=0.5)
    assert obj["recommendation"] == "HALT"
    assert obj["reason"] == "drift_below_threshold"
    assert obj["drift_history"] == [0.3333]
    assert abs(obj["verdict_drift"] - 0.3333) <= _FLOAT_TOL


# ─────────────────────────────────────────────────────────────────────────────
# (f) disjoint-panel rounds → drift 1.0 (no common agents between rounds)
# ─────────────────────────────────────────────────────────────────────────────
def test_disjoint_panel_max_drift_parity(db):
    _which_bash()
    # Round 1 = {glm, kimi}; round 2 = {codex, opus} — no shared agent.
    _seed(db, [
        ("tr-glm-r1-run-disjoint", "glm", "approve"),
        ("tr-kimi-r1-run-disjoint", "kimi", "approve"),
        ("tr-cdx-r2-run-disjoint", "codex", "reject"),
        ("tr-opus-r2-run-disjoint", "opus", "reject"),
    ])
    obj = _compare(db, "run-disjoint", 2)
    assert obj["drift_history"] == [1.0]
    assert obj["verdict_drift"] == 1.0
    assert obj["recommendation"] == "CONTINUE"
    assert obj["reason"] == "drift_above_threshold"


# ─────────────────────────────────────────────────────────────────────────────
# (g) no traces at all → fail-open CONTINUE, no_traces_default_continue
# ─────────────────────────────────────────────────────────────────────────────
def test_no_traces_failopen_parity(db):
    _which_bash()
    # Empty DB (nothing seeded for this panel id).
    obj = _compare(db, "run-empty", 1)
    assert obj["recommendation"] == "CONTINUE"
    assert obj["reason"] == "no_traces_default_continue"
    assert obj["rounds_seen"] == 0
    assert obj["verdict_drift"] == 1.0
    assert "drift_history" not in obj
