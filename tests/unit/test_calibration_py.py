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

import hashlib
import json
import math
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


def test_recency_bounds_env_can_only_tighten(monkeypatch):
    """Both recency bounds can only be tightened from the environment.

    Widening either one re-admits rows from before a lane's model or prompt
    changed, which is the drift the bounds exist to exclude. So the env can
    shorten the window and lower the cap, and raising either is refused — the
    same one-directional clamp ``target_error`` and ``min_samples`` use.
    """
    monkeypatch.delenv("MO_UCCI_WINDOW_DAYS", raising=False)
    monkeypatch.delenv("MO_UCCI_MAX_ROWS", raising=False)
    assert cal.window_days() == pytest.approx(cal.DEFAULT_WINDOW_DAYS)
    assert cal.max_rows() == cal.DEFAULT_MAX_ROWS

    monkeypatch.setenv("MO_UCCI_WINDOW_DAYS", "3")
    monkeypatch.setenv("MO_UCCI_MAX_ROWS", "50")
    assert cal.window_days() == pytest.approx(3.0)
    assert cal.max_rows() == 50

    monkeypatch.setenv("MO_UCCI_WINDOW_DAYS", "3650")
    monkeypatch.setenv("MO_UCCI_MAX_ROWS", "1000000")
    assert cal.window_days() == pytest.approx(cal.DEFAULT_WINDOW_DAYS)
    assert cal.max_rows() == cal.DEFAULT_MAX_ROWS

    monkeypatch.setenv("MO_UCCI_WINDOW_DAYS", "garbage")
    monkeypatch.setenv("MO_UCCI_MAX_ROWS", "garbage")
    assert cal.window_days() == pytest.approx(cal.DEFAULT_WINDOW_DAYS)
    assert cal.max_rows() == cal.DEFAULT_MAX_ROWS


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


def _seed_at(db: str, lane: str, margin: float, status: str, days_ago: float) -> str:
    """Seed one margin row whose created_at sits ``days_ago`` behind now.

    Offsets are relative on purpose. A test that wrote an absolute date would
    keep passing until the clock crossed it and then start failing in CI with
    no code change behind it.
    """
    tid = trace_store.trace_write({
        "task_class": "code-fix", "status": status, "agent_version_id": lane,
        "objective_domain": "code-delivery", "verifier_output": {"node_type": "implementer"},
        "route_source": "learned", "route_margin": margin,
    }, db=db)
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE execution_traces SET created_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now',?) WHERE trace_id = ?",
        (f"-{days_ago} days", tid))
    con.commit()
    con.close()
    return tid


def test_rows_outside_the_window_do_not_calibrate(db, monkeypatch):
    """Rows from before the window describe a lane that may no longer exist.

    The stale rows alone would clear ``min_samples``, so excluding them has to
    collapse the fit to an abstention rather than merely thin it — that is the
    whole point of the window, and a fit that quietly keeps answering would
    look identical from the outside.
    """
    monkeypatch.setenv("MO_UCCI_WINDOW_DAYS", "7")
    for _ in range(cal.min_samples()):
        _seed_at(db, "lensW", 0.5, "failure", days_ago=30)
    assert cal.load_margin_rows(db, "code-fix", lane="lensW") == []
    assert cal.should_escalate(db, "code-fix", 0.5, lane="lensW") == (False, None)

    # A single fresh row is inside the window but far under the sample floor.
    _seed_at(db, "lensW", 0.5, "failure", days_ago=1)
    assert len(cal.load_margin_rows(db, "code-fix", lane="lensW")) == 1
    assert cal.should_escalate(db, "code-fix", 0.5, lane="lensW") == (False, None)


def test_the_cap_keeps_the_newest_rows_not_an_arbitrary_slice(db, monkeypatch):
    """The cap must drop the oldest rows, because those are furthest from the
    lane's current model. Which rows survive is the whole contract."""
    monkeypatch.setenv("MO_UCCI_MAX_ROWS", "2")
    _seed_at(db, "lensC", 0.1, "failure", days_ago=5)
    _seed_at(db, "lensC", 0.2, "failure", days_ago=4)
    _seed_at(db, "lensC", 0.3, "failure", days_ago=3)
    _seed_at(db, "lensC", 0.8, "success", days_ago=2)
    _seed_at(db, "lensC", 0.9, "success", days_ago=1)
    rows = cal.load_margin_rows(db, "code-fix", lane="lensC")
    assert [m for m, _ in rows] == [0.9, 0.8]


def test_the_window_applies_to_the_pooled_slice_too(db, monkeypatch):
    """The pooled fallback reads the same table, so a stale-only slice must not
    calibrate through the back door when the per-lane fit abstains."""
    monkeypatch.setenv("MO_UCCI_WINDOW_DAYS", "7")
    for _ in range(cal.min_samples()):
        _seed_at(db, "lensPool", 0.5, "failure", days_ago=90)
    assert cal.calibrated_error(db, "code-fix", 0.5, lane="lensPool") is None


def test_recent_cutoff_compares_against_a_real_created_at(db, monkeypatch):
    """The bound must carry ``created_at``'s format or it compares wrong.

    ``created_at`` is an ISO string with a ``T`` separator. SQLite's own
    ``datetime('now','-N days')`` emits a space separator instead, which sorts
    strictly below every T-formatted row on the same day and would admit an
    extra day of stale rows. So the bound is built in the column's format, and
    the boundary is pinned against real rows rather than assumed.
    """
    monkeypatch.setenv("MO_UCCI_WINDOW_DAYS", "1")
    cutoff = cal.recent_cutoff()
    assert "T" in cutoff and " " not in cutoff
    # The same instant in the space-separated form is NOT an equivalent bound.
    # This is the trap the format exists to avoid, stated as an assertion.
    assert cutoff.replace("T", " ") < cutoff

    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO execution_traces "
        "(trace_id, task_class, status, created_at, route_margin, agent_version_id) "
        "VALUES (?,?,?,?,?,?)",
        ("t-onBoundary", "code-fix", "success", cutoff + ".000Z", 0.5, "onBoundary"))
    con.commit()
    con.close()

    # Exactly on the boundary is inside: the comparison is >=, not >.
    assert len(cal.load_margin_rows(db, "code-fix", lane="onBoundary")) == 1
    _seed_at(db, "ancient", 0.5, "success", days_ago=400)
    assert cal.load_margin_rows(db, "code-fix", lane="ancient") == []


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


# ── vendored reference vectors (varunkotte6/ucci @ 4e61fd73) ─────────────────
#
# The tests above are hand-written: they assert the properties mini-ork needs.
# These assert *agreement with the reference implementation* — the one claim the
# module docstring makes and cannot check by itself. "PAV is twenty lines of
# list arithmetic" is only reassuring if those twenty lines are the paper's
# twenty lines, and a monotone smoother can satisfy every property above while
# still disagreeing with the reference about where the blocks fall.
#
# The fixture is vendored unmodified; see NOTICE for the licence and provenance.

GOLDEN = REPO / "tests" / "fixtures" / "ucci-calibration.json"
# sha256 of the vendored bytes. Re-vendoring from upstream is a deliberate act:
# update this constant in the same commit, so the diff says the vectors moved.
GOLDEN_SHA256 = "f60df2ed94b083735ec3784356400b941d18250abd1560d54360e6878b274838"
GOLDEN_TOLERANCE = 1e-12
# The ``pav`` cases mini-ork's signature can express: equal weight, no error.
GOLDEN_PAV_IDS = {
    "pav_known", "pav_decreasing", "pav_constant", "pav_single",
    "pav_late_violator", "pav_random_300", "pav_binary_500",
}


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _equal_weight_pav_cases() -> list:
    return [c for c in _golden()["cases"]
            if c["fn"] == "pav" and c["input"].get("w") is None and "error" not in c]


def test_the_vendored_vectors_are_the_pinned_file():
    """The fixture is pinned, not floating: bytes, schema, and case corpus.

    If this fails on the hash alone, either upstream vectors were re-vendored
    (update GOLDEN_SHA256 in the same commit) or something normalised the file
    on checkout (there is no .gitattributes today, so that would be new).
    """
    assert GOLDEN.is_file(), f"vendored fixture missing: {GOLDEN}"
    assert hashlib.sha256(GOLDEN.read_bytes()).hexdigest() == GOLDEN_SHA256
    doc = _golden()
    assert doc["schema"] == "ucci-golden/1"
    assert doc["tolerance"] == GOLDEN_TOLERANCE
    assert {c["id"] for c in _equal_weight_pav_cases()} == GOLDEN_PAV_IDS


def test_pav_matches_the_reference_on_equal_weight_cases():
    """Every equal-weight reference vector, to the file's own 1e-12 tolerance."""
    cases = _equal_weight_pav_cases()
    assert cases, "no equal-weight pav cases selected — the filter is wrong"
    for case in cases:
        got = cal.pav(list(case["input"]["y"]))
        want = case["expected"]["fit"]
        assert got == pytest.approx(want, abs=GOLDEN_TOLERANCE), case["id"]


def test_the_equal_weight_corpus_actually_exercises_ties():
    """Ties are the point of the exercise, so assert the corpus contains them.

    Without this, a later edit could narrow the selection to ``pav_single`` and
    leave the conformance test passing over a corpus that tests nothing.
    """
    cases = {c["id"]: c for c in _equal_weight_pav_cases()}
    # pav_constant: every input equal — the degenerate tie.
    assert cases["pav_constant"]["input"]["y"] == [0.3] * 5
    # pav_known: [1,3,2,4] pools the middle pair onto a shared 2.5.
    assert cases["pav_known"]["expected"]["fit"] == [1.0, 2.5, 2.5, 4.0]
    # pav_binary_500: 0/1 inputs, so the fit is long runs of equal values.
    fit = cases["pav_binary_500"]["expected"]["fit"]
    run = longest = 1
    for a, b in zip(fit, fit[1:]):
        run = run + 1 if a == b else 1
        longest = max(longest, run)
    assert longest > 10, longest


def test_the_reference_validates_where_mini_ork_does_not():
    """Six of the reference's fifteen ``pav`` cases assert errors mini-ork's port
    cannot produce — two on values (empty, NaN), four on the weight argument it
    does not take. Recording the gap here keeps the vendored file honest.

    The gap is safe because neither invalid *value* is reachable from mini-ork's
    own call path, and the assertions below are what makes that a fact rather
    than a hope.
    """
    errors = {c["id"]: c["error"] for c in _golden()["cases"]
              if c["fn"] == "pav" and c["input"].get("w") is None and "error" in c}
    assert set(errors) == {"pav_error_empty", "pav_error_nan"}
    assert all(e["type"] == "ValueError" for e in errors.values())

    # mini-ork returns instead of raising.
    assert cal.pav([]) == []
    poisoned = cal.pav([1.0, float("nan")])
    assert len(poisoned) == 2 and all(math.isnan(v) for v in poisoned)

    # Unreachable #1: fit_error_map short-circuits an empty slice before PAV.
    assert cal.fit_error_map([]) == ([], [])

    # Unreachable #2: a NaN margin cannot survive the query. SQLite has no NaN
    # in REAL — it stores one as NULL, which `route_margin IS NOT NULL` drops.
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (m REAL)")
    con.execute("INSERT INTO t VALUES (?)", (float("nan"),))
    assert con.execute("SELECT m FROM t").fetchone()[0] is None
    con.close()
