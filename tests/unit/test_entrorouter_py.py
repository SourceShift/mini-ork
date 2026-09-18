"""Unit tests: EntroRouter entropy regulation, and the router's margin (2606.29424).

EntroRouter names *Trust Region Collapse*: a capable lane takes one bad sample
early, its estimate sinks, and the selector never picks it again, so it can
never recover. The fix is a Soft-Anchored recovery floor — the lane's offline
capability estimate bounds how far its online estimate may sink.

The load-bearing property is that the mechanism is INERT when no capability
estimate exists. A database with no agent_performance_memory rows must route
byte-identically to the plain UCB bandit; otherwise this "improvement" would
silently rewrite every existing routing decision.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import lane_router, trace_store  # noqa: E402
from mini_ork.learning.advantage_store import AdvantageStore  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402

_TC, _NT, _OD, _CR = "code-fix", "implementer", "code-delivery", ""


def _rows(specs):
    """``sqlite3.Row`` candidates shaped exactly like ``_fetch_lane_candidates``
    returns them — the selector reads both ``row[0]`` and ``row["runs_count"]``,
    so a plain tuple would not exercise the real indexing."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (agent_version_id TEXT, adv_str TEXT, "
                "runs_count INTEGER, z_score_advantage REAL)")
    con.executemany("INSERT INTO t VALUES (?,?,?,?)", specs)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT agent_version_id, adv_str, runs_count, z_score_advantage "
        "FROM t ORDER BY z_score_advantage DESC").fetchall()
    con.close()
    return rows


# ── the recovery floor (pure) ────────────────────────────────────────────────

def test_recovery_floor_decays_as_observations_accumulate():
    """Soft supervision: the floor starts high and decays as the lane proves
    itself, so it is a recovery budget, not a permanent subsidy."""
    caps, anchor, tau = 0.8, 0.5, 3.0
    floor_1 = lane_router._recovery_floor(caps, 1, anchor, tau)
    floor_10 = lane_router._recovery_floor(caps, 10, anchor, tau)
    floor_100 = lane_router._recovery_floor(caps, 100, anchor, tau)
    assert floor_1 > floor_10 > floor_100 > 0.0
    # tau/(tau+n): n=tau is the half-way point.
    assert floor_1 == pytest.approx(anchor * caps * 0.75)
    assert floor_10 == pytest.approx(anchor * caps * (3 / 13))


def test_recovery_floor_is_zero_without_a_positive_capability():
    """A lane with no positive offline estimate gets no floor. The floor exists
    to undo an unfair collapse, not to keep a genuinely weak lane alive."""
    assert lane_router._recovery_floor(0.0, 1, 0.5, 3.0) == 0.0
    assert lane_router._recovery_floor(-0.4, 1, 0.5, 3.0) == 0.0


# ── the selector ─────────────────────────────────────────────────────────────

def test_floor_rescues_a_capable_lane_that_sank(monkeypatch):
    """The published failure mode: a capable lane whose observed estimate sank
    below a weaker competitor is never picked again, so it can never recover.
    The anchor lifts it back into contention.

    The regime is the cold slice — one run each, both lanes' z negative. That is
    where the collapse happens and where the floor decides: it decays as 1/n and
    the UCB bonus as 1/sqrt(n), so a warm slice's bonus already gives the sunk
    lane a fighting chance on its own and the anchor has nothing left to do."""
    monkeypatch.delenv("MO_ENTROROUTER", raising=False)
    cands = _rows([("sunk", "-1.000", 1, -1.0), ("ok", "-0.500", 1, -0.5)])

    # Without capability knowledge the sunk lane is simply the loser.
    plain, _ = lane_router._select_best_lane_scored(
        cands, True, 0.5, 0, _TC, _NT, _OD, _CR)
    assert plain.startswith("ok|")

    # With the offline estimate the floor lifts it above the incumbent.
    rescued, margin = lane_router._select_best_lane_scored(
        cands, True, 0.5, 0, _TC, _NT, _OD, _CR, capabilities={"sunk": 1.0})
    assert rescued.startswith("sunk|")
    assert margin is not None and margin > 0


def test_empty_capabilities_leaves_the_ranking_untouched(monkeypatch):
    """The byte-parity claim: no capability estimates, no behaviour change."""
    monkeypatch.delenv("MO_ENTROROUTER", raising=False)
    cands = _rows([("sunk", "-1.000", 20, -1.0), ("ok", "0.050", 20, 0.05)])
    base = lane_router._select_best_lane_scored(
        cands, True, 0.5, 0, _TC, _NT, _OD, _CR)
    for caps in (None, {}):
        assert lane_router._select_best_lane_scored(
            cands, True, 0.5, 0, _TC, _NT, _OD, _CR, capabilities=caps) == base


def test_opt_out_restores_the_constant_c_winner(monkeypatch):
    monkeypatch.setenv("MO_ENTROROUTER", "0")
    cands = _rows([("sunk", "-1.000", 20, -1.0), ("ok", "0.050", 20, 0.05)])
    pick, _ = lane_router._select_best_lane_scored(
        cands, True, 0.5, 0, _TC, _NT, _OD, _CR, capabilities={"sunk": 0.9})
    assert pick.startswith("ok|")


def test_the_floor_never_lowers_a_score(monkeypatch):
    """Applied with max(), so it can only lift a lane up to a fighting chance.
    As an additive bonus it would inflate the incumbent that is already
    winning, which is the opposite of the intent."""
    monkeypatch.delenv("MO_ENTROROUTER", raising=False)
    cands = _rows([("strong", "2.000", 20, 2.0), ("sunk", "-1.000", 20, -1.0)])
    pick, margin = lane_router._select_best_lane_scored(
        cands, True, 0.5, 0, _TC, _NT, _OD, _CR,
        capabilities={"strong": 1.0, "sunk": 0.05})
    assert pick.startswith("strong|")
    assert margin is not None and margin > 0


# ── the margin the selector now returns ──────────────────────────────────────

def test_margin_is_winner_minus_runner_up(monkeypatch):
    monkeypatch.delenv("MO_ENTROROUTER", raising=False)
    cands = _rows([("a", "1.000", 10, 1.0), ("b", "0.000", 10, 0.0)])
    pick, margin = lane_router._select_best_lane_scored(
        cands, True, 0.5, 0, _TC, _NT, _OD, _CR)
    assert pick.startswith("a|")
    # Equal run counts give equal UCB bonuses, so the gap is the z-score gap.
    assert margin == pytest.approx(1.0)


def test_margin_is_none_when_there_is_no_comparison(monkeypatch):
    """None, never 0.0. A calibration fitted on zeros would read every unknown
    as maximal disagreement."""
    monkeypatch.delenv("MO_ENTROROUTER", raising=False)
    one = _rows([("solo", "1.000", 10, 1.0)])
    assert lane_router._select_best_lane_scored(
        one, True, 0.5, 0, _TC, _NT, _OD, _CR)[1] is None
    # Bandit off is the legacy first-row face and carries no comparison either.
    assert lane_router._select_best_lane_scored(
        one, False, 0.5, 0, _TC, _NT, _OD, _CR)[1] is None
    assert lane_router._select_best_lane_scored(
        [], True, 0.5, 0, _TC, _NT, _OD, _CR) == ("", None)


def test_signature_preserving_wrapper_returns_the_lane(monkeypatch):
    monkeypatch.delenv("MO_ENTROROUTER", raising=False)
    cands = _rows([("a", "1.000", 10, 1.0), ("b", "0.000", 10, 0.0)])
    assert lane_router._select_best_lane(
        cands, True, 0.5, 0, _TC, _NT, _OD, _CR) == "a|1.000|10"


# ── the capability source ────────────────────────────────────────────────────

def test_fetch_lane_capabilities_reads_the_global_face(tmp_path):
    """The anchor must be OFFLINE — a stable prior, not the collapsing online
    estimate. agent_performance_memory is the global (task_class-wide) face, so
    it barely moves when one narrow slice collapses."""
    dbp = str(tmp_path / "state.db")
    con = sqlite3.connect(dbp)
    con.execute("CREATE TABLE agent_performance_memory ("
                " agent_version_id TEXT, role TEXT, model TEXT, task_class TEXT,"
                " runs_count INTEGER, success_count INTEGER, relative_advantage REAL,"
                " last_updated TEXT)")
    con.execute("INSERT INTO agent_performance_memory VALUES "
                "('lensA','implementer','lensA','code-fix',10,8,0.75,'now')")
    con.execute("INSERT INTO agent_performance_memory VALUES "
                "('lensB','implementer','lensB','book-gen',10,8,0.42,'now')")
    con.commit()
    con.close()
    store = AdvantageStore(dbp).open()
    try:
        caps = store.fetch_lane_capabilities("code-fix")
    finally:
        store.close()
    # Scoped to the task class: book-gen's lane must not anchor code-fix.
    assert caps == {"lensA": pytest.approx(0.75)}


def test_fetch_lane_capabilities_absent_table_is_empty(tmp_path):
    """A router with no capability estimates must degrade to the plain bandit,
    never raise."""
    dbp = str(tmp_path / "bare.db")
    sqlite3.connect(dbp).close()
    store = AdvantageStore(dbp).open()
    try:
        assert store.fetch_lane_capabilities("code-fix") == {}
    finally:
        store.close()


# ── end to end: the router publishes a margin ────────────────────────────────

def test_preferred_lane_detail_publishes_the_margin(tmp_path, monkeypatch):
    """The margin is computed at the decision point and used to be discarded.
    UCCI consumes it, so it has to survive the return trip."""
    monkeypatch.setenv("MO_ENTROROUTER", "1")
    monkeypatch.setenv("MO_LEARNING_MIN_SAMPLES", "1")
    monkeypatch.delenv("MO_ROUTER_CONTEXTUAL", raising=False)
    monkeypatch.setenv("MO_LEARNING_HALFLIFE_DAYS", "0")
    dbp = str(tmp_path / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    for lane, rv in (("laneA", 1.0), ("laneB", 0.0)):
        for _ in range(3):
            trace_store.trace_write(
                {"task_class": "code-fix", "status": "success",
                 "agent_version_id": lane, "objective_domain": "code-delivery",
                 "verifier_output": {"node_type": "implementer"},
                 "reward_value": rv, "reward_anchor": 0.5,
                 "reward_direction": "higher_is_better"}, db=dbp)
    lane_router.recompute_advantages(db=dbp)

    detail = lane_router.preferred_lane_detail("code-fix", "implementer",
                                               "code-delivery", db=dbp)
    assert detail["lane"] == "laneA"
    assert detail["source"] == "domain"
    assert detail["margin"] is not None and detail["margin"] > 0
    # The string face is unchanged for every existing caller.
    assert lane_router.preferred_lane("code-fix", "implementer",
                                      "code-delivery", db=dbp) == detail["pick"]
