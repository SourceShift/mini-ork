"""Standalone unit tests for ``mini_ork.gates.adaptive_stability``.

Replaces the bash-parity gate (against ``lib/adaptive_stability.sh``) as
part of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer runs the LIVE bash function
``mo_check_panel_stability`` in a subprocess — it asserts the port's
behaviour directly. The expected values below are the semantic contract
previously asserted on the bash side (drift fractions rounded to 4dp by
the gate), now asserted on the port's output.

Seven cases:
  (a) 3-round stable panel, round 3      → HALT, drift_below_threshold
  (b) current_round=1                    → CONTINUE, below_min_rounds
  (c) max_rounds=3 at round 3            → HALT, max_rounds_reached
  (d) unencoded trace_ids                → CONTINUE, round_unencoded_default_continue
  (e) 2-round panel, threshold=0.5, rd 2 → HALT, verdict_drift ≈ 0.3333
  (f) disjoint-panel rounds              → drift_per_round = [1.0]
  (g) no traces at all                   → CONTINUE, no_traces_default_continue
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import trace_store  # noqa: E402
from mini_ork.gates import adaptive_stability as ast  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

_FLOAT_TOL = 1e-6


@pytest.fixture
def db(tmp_path_factory):
    """Bootstrap a fresh DB via the Python port of db/init.sh."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
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
    identical to round 2 (round 2→3 zero drift)."""
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


# ─────────────────────────────────────────────────────────────────────────────
# (a) 3-round stable panel at round 3 → HALT, drift_below_threshold
# ─────────────────────────────────────────────────────────────────────────────
def test_stable_panel_halts(db):
    _seed_stable_panel(db)
    obj = ast.check_panel_stability("run-stable", 3, db=db)
    assert obj["recommendation"] == "HALT"
    assert obj["reason"] == "drift_below_threshold"
    assert obj["stable"] is True
    # round 1→2 drift = 1/3, round 2→3 = 0.0
    assert obj["drift_history"] == [0.3333, 0.0]
    assert obj["verdict_drift"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# (b) round 1 below min_rounds → CONTINUE, below_min_rounds
# ─────────────────────────────────────────────────────────────────────────────
def test_below_min_rounds_continues(db):
    _seed_stable_panel(db)
    obj = ast.check_panel_stability("run-stable", 1, db=db)
    assert obj["recommendation"] == "CONTINUE"
    assert obj["reason"] == "below_min_rounds"
    assert obj["stable"] is False


# ─────────────────────────────────────────────────────────────────────────────
# (c) max_rounds reached → unconditional HALT
# ─────────────────────────────────────────────────────────────────────────────
def test_max_rounds_halts(db):
    _seed_stable_panel(db)
    obj = ast.check_panel_stability("run-stable", 3, db=db, max_rounds=3)
    assert obj["recommendation"] == "HALT"
    assert obj["reason"] == "max_rounds_reached"


# ─────────────────────────────────────────────────────────────────────────────
# (d) unencoded trace_ids → fail-open CONTINUE, round_unencoded_default_continue
# ─────────────────────────────────────────────────────────────────────────────
def test_round_unencoded_failopen(db):
    _seed(db, [("tr-glm-run-noround-123", "glm", "approve")])
    obj = ast.check_panel_stability("run-noround", 3, db=db)
    assert obj["recommendation"] == "CONTINUE"
    assert obj["reason"] == "round_unencoded_default_continue"
    assert obj["verdict_drift"] == 1.0
    assert obj["rounds_seen"] == 0
    # fail-open branch omits drift_history entirely
    assert "drift_history" not in obj


# ─────────────────────────────────────────────────────────────────────────────
# (e) threshold tunable: 2-round panel, drift=1/3, threshold=0.5 → HALT
#     NOTE: the gate buckets ALL rounds present in the DB (it does not filter
#     by current_round), so last_drift is the drift of the final bucketed
#     round. We seed ONLY rounds 1 and 2 (round 1→2 drift = 1 changed / 3
#     common = 0.3333).
# ─────────────────────────────────────────────────────────────────────────────
def test_threshold_tunable(db):
    _seed(db, [
        ("tr-glm-r1-run-two", "glm", "approve"),
        ("tr-kimi-r1-run-two", "kimi", "reject"),
        ("tr-cdx-r1-run-two", "codex", "reject"),
        ("tr-glm-r2-run-two", "glm", "approve"),
        ("tr-kimi-r2-run-two", "kimi", "approve"),
        ("tr-cdx-r2-run-two", "codex", "reject"),
    ])
    obj = ast.check_panel_stability("run-two", 2, db=db, threshold=0.5)
    assert obj["recommendation"] == "HALT"
    assert obj["reason"] == "drift_below_threshold"
    assert obj["drift_history"] == [0.3333]
    assert abs(obj["verdict_drift"] - 0.3333) <= _FLOAT_TOL


# ─────────────────────────────────────────────────────────────────────────────
# (f) disjoint-panel rounds → drift 1.0 (no common agents between rounds)
# ─────────────────────────────────────────────────────────────────────────────
def test_disjoint_panel_max_drift(db):
    # Round 1 = {glm, kimi}; round 2 = {codex, opus} — no shared agent.
    _seed(db, [
        ("tr-glm-r1-run-disjoint", "glm", "approve"),
        ("tr-kimi-r1-run-disjoint", "kimi", "approve"),
        ("tr-cdx-r2-run-disjoint", "codex", "reject"),
        ("tr-opus-r2-run-disjoint", "opus", "reject"),
    ])
    obj = ast.check_panel_stability("run-disjoint", 2, db=db)
    assert obj["drift_history"] == [1.0]
    assert obj["verdict_drift"] == 1.0
    assert obj["recommendation"] == "CONTINUE"
    assert obj["reason"] == "drift_above_threshold"


# ─────────────────────────────────────────────────────────────────────────────
# (g) no traces at all → fail-open CONTINUE, no_traces_default_continue
# ─────────────────────────────────────────────────────────────────────────────
def test_no_traces_failopen(db):
    # Empty DB (nothing seeded for this panel id).
    obj = ast.check_panel_stability("run-empty", 1, db=db)
    assert obj["recommendation"] == "CONTINUE"
    assert obj["reason"] == "no_traces_default_continue"
    assert obj["rounds_seen"] == 0
    assert obj["verdict_drift"] == 1.0
    assert "drift_history" not in obj
