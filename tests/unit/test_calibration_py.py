"""Unit tests: mini_ork.dispatch.calibration (UCCI, 2605.18796).

The calibrated error probability is what turns escalation from a guess into a
*rule*, so the invariants asserted here are the ones that make it a rule: the
fitted map never rises as the margin grows (otherwise "escalate past the
threshold" would not describe a region), and the threshold can only be tightened
from the environment, never loosened.

Every abstention path is asserted too. A calibration that cannot fit must return
"do not escalate" — the router's pre-existing behaviour — because the failure
mode of a calibration layer is not a wrong number, it is an invented one.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import trace_store  # noqa: E402
from mini_ork.dispatch import calibration as cal  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402


# ── isotonic fit (pure) ──────────────────────────────────────────────────────

def test_pav_is_non_decreasing_and_pools_violations():
    """PAV returns a non-decreasing sequence, and a violation is pooled to the
    weighted mean of the merged block rather than smoothed away."""
    # 0.5 then 0.2 violates; both pool to their mean.
    assert cal.pav([0.5, 0.2]) == pytest.approx([0.35, 0.35])
    # Already monotone: untouched.
    assert cal.pav([0.1, 0.4, 0.9]) == pytest.approx([0.1, 0.4, 0.9])
    # A whole non-monotone run collapses to one block.
    assert cal.pav([0.5, 0.2, 0.3, 0.1]) == pytest.approx([0.275] * 4)
    # Output is always non-decreasing, whatever the input.
    for values in ([3, 1, 2], [1, 1, 1], [0.0], [5, 4, 3, 2, 1]):
        fitted = cal.pav(values)
        assert all(a <= b for a, b in zip(fitted, fitted[1:])), values


def test_fit_error_map_is_non_increasing_in_margin():
    """A larger margin is a more decisive win, so the fitted error rate must
    never rise with it. An error probability that increased with confidence
    would make the escalation threshold incoherent."""
    # Deliberately noisy and non-monotone: errors at low margin, successes high.
    rows = [(0.1, True), (0.2, True), (0.15, True), (0.3, False),
            (0.25, True), (0.4, False), (0.35, False), (0.5, False)]
    margins, fitted = cal.fit_error_map(rows)
    assert margins == sorted(margins)
    assert all(a >= b for a, b in zip(fitted, fitted[1:])), fitted
    # And the fit is a probability.
    assert all(0.0 <= v <= 1.0 for v in fitted)


def test_fit_error_map_empty():
    assert cal.fit_error_map([]) == ([], [])


def test_error_probability_clamps_and_interpolates():
    margins = [0.0, 1.0]
    fitted = [0.8, 0.2]  # non-increasing, as a real fit would be
    # Below the first bucket clamps to the first value, not extrapolates.
    assert cal.error_probability(margins, fitted, -5.0) == pytest.approx(0.8)
    # Above the last clamps to the last.
    assert cal.error_probability(margins, fitted, 99.0) == pytest.approx(0.2)
    # Between buckets interpolates linearly.
    assert cal.error_probability(margins, fitted, 0.5) == pytest.approx(0.5)
    assert cal.error_probability(margins, fitted, 0.25) == pytest.approx(0.65)
    # Monotone in the margin: a bigger margin never predicts more error.
    vals = [cal.error_probability(margins, fitted, m / 10.0) for m in range(-10, 30)]
    assert all(a >= b for a, b in zip(vals, vals[1:])), vals


def test_error_probability_abstains_on_an_empty_map():
    assert cal.error_probability([], [], 0.5) is None
    # A mismatched pair is a broken fit, not a fit at zero.
    assert cal.error_probability([0.0, 1.0], [0.5], 0.5) is None


# ── thresholds can only be tightened ─────────────────────────────────────────

def test_target_error_env_can_only_tighten(monkeypatch):
    """A higher threshold tolerates more predicted error, i.e. escalates less.
    That is the dangerous direction, so the environment cannot reach it."""
    monkeypatch.delenv("MO_UCCI_TARGET_ERROR", raising=False)
    assert cal.target_error() == pytest.approx(cal.DEFAULT_TARGET_ERROR)

    monkeypatch.setenv("MO_UCCI_TARGET_ERROR", "0.01")
    assert cal.target_error() == pytest.approx(0.01)

    monkeypatch.setenv("MO_UCCI_TARGET_ERROR", "0.95")
    assert cal.target_error() == pytest.approx(cal.DEFAULT_TARGET_ERROR)

    monkeypatch.setenv("MO_UCCI_TARGET_ERROR", "garbage")
    assert cal.target_error() == pytest.approx(cal.DEFAULT_TARGET_ERROR)


def test_min_samples_env_can_only_raise(monkeypatch):
    """An env that could lower the floor to 1 would let a single observation
    define a monotone map that always agrees with itself."""
    monkeypatch.delenv("MO_UCCI_MIN_SAMPLES", raising=False)
    assert cal.min_samples() == cal.DEFAULT_MIN_SAMPLES

    monkeypatch.setenv("MO_UCCI_MIN_SAMPLES", "50")
    assert cal.min_samples() == 50

    monkeypatch.setenv("MO_UCCI_MIN_SAMPLES", "1")
    assert cal.min_samples() == cal.DEFAULT_MIN_SAMPLES


# ── the escalation decision (DB-backed) ──────────────────────────────────────

def _init_db(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    return dbp


def _seed(db: str, lane: str, margins, status: str, task_class: str = "code-fix"):
    for m in margins:
        trace_store.trace_write({
            "task_class": task_class, "status": status,
            "agent_version_id": lane, "objective_domain": "code-delivery",
            "verifier_output": {"node_type": "implementer"},
            "route_source": "learned", "route_margin": m,
        }, db=db)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_UCCI_CACHE_TTL", "0")
    monkeypatch.delenv("MO_UCCI", raising=False)
    monkeypatch.delenv("MO_UCCI_TARGET_ERROR", raising=False)
    cal.clear_cache()
    return _init_db(tmp_path / "home")


def test_load_margin_rows_requires_a_margin(db):
    """A row with no margin is not a low-confidence row, it is an
    uncalibratable one — the static route and the exploration swap both leave
    the margin NULL by design and must not enter the fit."""
    _seed(db, "lensA", [0.4, 0.5], "success")
    trace_store.trace_write({
        "task_class": "code-fix", "status": "success", "agent_version_id": "lensA",
        "objective_domain": "code-delivery", "route_source": "explore",
    }, db=db)  # exploration row: no margin
    rows = cal.load_margin_rows(db, "code-fix", lane="lensA")
    assert len(rows) == 2
    assert all(m in (0.4, 0.5) for m, _ in rows)
    assert all(is_error is False for _, is_error in rows)


def test_load_margin_rows_maps_non_success_to_error(db):
    _seed(db, "lensA", [0.1], "failure")
    _seed(db, "lensA", [0.9], "success")
    rows = dict(cal.load_margin_rows(db, "code-fix", lane="lensA"))
    assert rows[0.1] is True
    assert rows[0.9] is False


def test_escalates_when_the_lane_is_predicted_wrong(db):
    """The lane the router picked has a history of failing, so the margin it
    won by is not reassurance — escalate."""
    _seed(db, "lensA", [0.5] * cal.min_samples(), "failure")
    escalate, p = cal.should_escalate(db, "code-fix", 0.5, lane="lensA")
    assert escalate is True
    assert p == pytest.approx(1.0)


def test_does_not_escalate_when_the_lane_is_predicted_right(db):
    _seed(db, "lensA", [0.5] * cal.min_samples(), "success")
    escalate, p = cal.should_escalate(db, "code-fix", 0.5, lane="lensA")
    assert escalate is False
    assert p == pytest.approx(0.0)


def test_a_bigger_margin_reduces_predicted_error(db):
    """The whole premise: the margin reads as confidence. One lane, one
    threshold, opposite decisions depending only on how decisively it won."""
    # Failures won narrowly, successes won decisively — a real lane profile.
    for m, st in [(0.1, "failure")] * 8 + [(0.9, "success")] * 8:
        trace_store.trace_write({
            "task_class": "code-fix", "status": st, "agent_version_id": "lensB",
            "objective_domain": "code-delivery", "route_source": "learned",
            "route_margin": m,
        }, db=db)
    low, p_low = cal.should_escalate(db, "code-fix", 0.1, lane="lensB")
    high, p_high = cal.should_escalate(db, "code-fix", 0.9, lane="lensB")
    assert p_low is not None and p_high is not None
    assert p_low > p_high
    assert low is True
    assert high is False


def test_abstains_without_a_margin(db):
    _seed(db, "lensA", [0.5] * cal.min_samples(), "failure")
    assert cal.should_escalate(db, "code-fix", None, lane="lensA") == (False, None)


def test_abstains_on_a_thin_fit(db):
    """Fewer observations than the floor is noise, not calibration."""
    _seed(db, "lensA", [0.5] * (cal.min_samples() - 1), "failure")
    assert cal.should_escalate(db, "code-fix", 0.5, lane="lensA") == (False, None)


def test_abstains_on_an_unknown_lane_with_no_pooled_history(db):
    _seed(db, "lensA", [0.5] * cal.min_samples(), "failure")
    # lensZ has no rows of its own; the pooled slice does, so the pooled fit
    # answers. A lane with neither abstains.
    assert cal.should_escalate(db, "code-fix", 0.5, lane="lensZ")[0] is True
    assert cal.should_escalate(db, "no-such-class", 0.5, lane="lensZ") == (False, None)


def test_opt_out_restores_uncalibrated_routing(db, monkeypatch):
    _seed(db, "lensA", [0.5] * cal.min_samples(), "failure")
    assert cal.should_escalate(db, "code-fix", 0.5, lane="lensA")[0] is True
    monkeypatch.setenv("MO_UCCI", "0")
    assert cal.should_escalate(db, "code-fix", 0.5, lane="lensA") == (False, None)


def test_pre_0057_database_fails_open(tmp_path, monkeypatch):
    """A database without route_margin degrades to today's behaviour instead of
    raising: a migration lag must not take routing down."""
    monkeypatch.setenv("MO_UCCI_CACHE_TTL", "0")
    cal.clear_cache()
    dbp = str(tmp_path / "old.db")
    con = sqlite3.connect(dbp)
    con.execute("CREATE TABLE execution_traces ("
                " trace_id TEXT PRIMARY KEY, task_class TEXT, status TEXT,"
                " agent_version_id TEXT)")
    con.execute("INSERT INTO execution_traces VALUES ('t1','code-fix','failure','lensA')")
    con.commit()
    con.close()
    assert cal.load_margin_rows(dbp, "code-fix", lane="lensA") == []
    assert cal.should_escalate(dbp, "code-fix", 0.5, lane="lensA") == (False, None)
