"""Unit tests: RouteMoA pre-inference dominance screening (rsi-gap-routemoa).

Hermetic: every test that touches routing builds its own sqlite DB under
``tmp_path``; the pure ``screen``/``wilson_bounds`` tests use in-memory rows.
No network, no credentials, no run-local state.
"""
from __future__ import annotations

import math
import sqlite3

import pytest

import mini_ork.lane_router as lane_router
from mini_ork.dispatch import screening
from mini_ork.learning.advantage_store import AdvantageStore

TC = "routemoa-test-task-class"
OD = "routemoa-test-objective-domain"


@pytest.fixture(autouse=True)
def _router_env(monkeypatch):
    """Pin every knob the screen and ``_pick_lane`` read."""
    for var in ("MO_ROUTEMOA", "MO_ROUTEMOA_MIN_RUNS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MO_LEARNING_MIN_SAMPLES", "3")
    monkeypatch.setenv("MO_ROUTER_UCB_C", "0.5")
    monkeypatch.setenv("MO_ROUTER_CONTEXTUAL", "0")
    monkeypatch.setenv("MO_EQUIROUTER", "1")
    monkeypatch.setenv("MO_ENTROROUTER", "1")
    monkeypatch.setenv("MO_ROUTER_ANCHOR", "0.5")
    monkeypatch.setenv("MO_ROUTER_ENTROPY_TAU", "3")


_ROW_CON = sqlite3.connect(":memory:")
_ROW_CON.row_factory = sqlite3.Row


def _cand(lane: str, adv: str = "0.000", runs: int = 0, z: float = 0.0):
    """A candidate row in the shape ``_select_best_lane_scored`` consumes."""
    return _ROW_CON.execute(
        "SELECT ? AS agent_version_id, ? AS adv_str, ? AS runs_count, "
        "? AS z_score_advantage", (lane, adv, runs, z)).fetchone()


def _wilson_ref(s: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = s / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (center - margin, center + margin)


def _make_db(tmp_path, *, with_apm=True, apm_rows=(), domain_rows=(),
             region_rows=()) -> str:
    """Temp sqlite DB seeded through the public AdvantageStore API."""
    path = str(tmp_path / "router.db")
    store = AdvantageStore(path).open()
    store.ensure_advantage_tables()
    if with_apm:
        # ensure_advantage_tables does not create agent_performance_memory;
        # create the production schema so upsert_agent_performance works.
        store.con.execute("""CREATE TABLE IF NOT EXISTS agent_performance_memory (
              agent_version_id TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
              model TEXT NOT NULL DEFAULT '', task_class TEXT NOT NULL,
              runs_count INTEGER NOT NULL DEFAULT 0,
              success_count INTEGER NOT NULL DEFAULT 0,
              relative_advantage REAL NOT NULL DEFAULT 0.0,
              last_updated TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (agent_version_id, task_class))""")
        for lane, succ, runs, adv in apm_rows:
            store.upsert_agent_performance(
                lane, lane, lane, TC, runs_count=runs, success_count=succ,
                relative_advantage=adv)
    for lane, rel, runs, succ, z in domain_rows:
        store.upsert_domain_advantage(
            lane, TC, "", OD, relative_advantage=rel, runs_count=runs,
            success_count=succ, advantage_var=0.0, advantage_std=0.0,
            z_score_advantage=z)
    for lane, region, rel, runs, succ, z in region_rows:
        store.upsert_region_advantage(
            lane, TC, "", OD, region, relative_advantage=rel, runs_count=runs,
            success_count=succ, advantage_var=0.0, advantage_std=0.0,
            z_score_advantage=z)
    store.commit()
    store.close()
    return path


def _seed_traces(path, rows):
    """Seed ``execution_traces`` with ``(lane, successes, runs)`` per lane.

    The RouteMoA screen reads its per-lane outcomes from this table, so the
    pick tests must put the same numbers there that they used to put in
    ``agent_performance_memory``.
    """
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS execution_traces (
          trace_id TEXT, agent_version_id TEXT, task_class TEXT,
          objective_domain TEXT, code_region TEXT, verifier_output TEXT,
          reward_g REAL, cost_usd REAL, status TEXT, created_at TEXT)""")
    trace_id = 0
    for lane, successes, runs in rows:
        for i in range(runs):
            con.execute(
                "INSERT INTO execution_traces (trace_id, agent_version_id,"
                " task_class, objective_domain, code_region, verifier_output,"
                " reward_g, cost_usd, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [f"seed-{trace_id}", lane, TC, OD, "", "{}",
                 1.0 if i < successes else -1.0,
                 0.0, "success" if i < successes else "failure",
                 "2026-10-01T00:00:00.000Z"])
            trace_id += 1
    con.commit()
    con.close()


# ── wilson_bounds ────────────────────────────────────────────────────────────


def test_wilson_bounds_nonpositive_n():
    assert screening.wilson_bounds(0, 0) == (0.0, 1.0)
    assert screening.wilson_bounds(5, 0) == (0.0, 1.0)
    assert screening.wilson_bounds(3, -2) == (0.0, 1.0)


def test_wilson_bounds_match_reference_interval():
    for s, n in [(18, 20), (2, 20), (11, 20), (9, 20), (5, 8), (0, 20),
                 (20, 20)]:
        lo, hi = screening.wilson_bounds(s, n)
        ref_lo, ref_hi = _wilson_ref(s, n)
        assert lo == pytest.approx(ref_lo, abs=1e-9)
        assert hi == pytest.approx(ref_hi, abs=1e-9)
        assert 0.0 <= lo <= hi <= 1.0


def test_wilson_bounds_tighten_as_n_grows():
    widths = []
    for n in (10, 40, 160, 640):
        s = (9 * n) // 10
        lo, hi = screening.wilson_bounds(s, n)
        assert 0.0 <= lo <= 0.9 <= hi <= 1.0
        widths.append(hi - lo)
    assert all(a > b for a, b in zip(widths, widths[1:])), widths


# ── screen() ─────────────────────────────────────────────────────────────────


def test_screen_dominated_lane_is_reported():
    a, b = _cand("lane-a"), _cand("lane-b")
    kept, screened = screening.screen(
        [a, b], {"lane-a": (18, 20), "lane-b": (2, 20)}, min_runs=8)
    assert kept == [a]
    assert kept[0] is a
    assert len(screened) == 1
    entry = screened[0]
    assert entry["lane"] == "lane-b"
    assert entry["dominated_by"] == "lane-a"
    assert entry["upper"] == pytest.approx(_wilson_ref(2, 20)[1], abs=1e-9)
    assert entry["dominator_lower"] == pytest.approx(
        _wilson_ref(18, 20)[0], abs=1e-9)
    assert entry["upper"] < entry["dominator_lower"]


def test_screen_lane_below_min_runs_is_never_screened():
    a, b = _cand("lane-a"), _cand("lane-b")
    kept, screened = screening.screen(
        [a, b], {"lane-a": (18, 20), "lane-b": (2, 3)}, min_runs=8)
    assert kept == [a, b]
    assert screened == []


def test_screen_overlapping_intervals_screen_nothing():
    a, b = _cand("lane-a"), _cand("lane-b")
    kept, screened = screening.screen(
        [a, b], {"lane-a": (11, 20), "lane-b": (9, 20)}, min_runs=8)
    assert kept == [a, b]
    assert screened == []


def test_screen_chain_always_leaves_one_kept():
    a, b, c = _cand("lane-a"), _cand("lane-b"), _cand("lane-c")
    stats = {"lane-a": (19, 20), "lane-b": (10, 20), "lane-c": (1, 20)}
    kept, screened = screening.screen([a, b, c], stats, min_runs=8)
    assert [r[0] for r in kept] == ["lane-a"]
    assert kept[0] is a
    by_lane = {e["lane"]: e for e in screened}
    assert set(by_lane) == {"lane-b", "lane-c"}
    assert by_lane["lane-b"]["dominated_by"] == "lane-a"
    assert by_lane["lane-c"]["dominated_by"] in ("lane-a", "lane-b")
    for entry in screened:
        assert entry["upper"] < entry["dominator_lower"]


def test_screen_dominator_must_be_a_candidate():
    b = _cand("lane-b")
    kept, screened = screening.screen(
        [b], {"lane-b": (2, 20), "lane-super": (20, 20)}, min_runs=8)
    assert kept == [b]
    assert screened == []


def test_screen_preserves_order_and_element_identity():
    a, c, b = _cand("lane-a"), _cand("lane-c"), _cand("lane-b")
    stats = {"lane-a": (18, 20), "lane-c": (2, 3), "lane-b": (2, 20)}
    kept, screened = screening.screen([a, c, b], stats, min_runs=8)
    assert kept == [a, c]
    assert kept[0] is a and kept[1] is c
    assert [e["lane"] for e in screened] == ["lane-b"]


def test_screen_min_runs_is_keyword_only():
    b = _cand("lane-b")
    with pytest.raises(TypeError):
        screening.screen([b], {"lane-b": (2, 20)}, 8)  # type: ignore[misc]


# ── knobs ────────────────────────────────────────────────────────────────────


def test_enabled_defaults_on_and_only_exact_zero_opts_out(monkeypatch):
    assert screening.enabled() is True
    for value, expected in [("0", False), ("1", True), ("false", True),
                            ("", True)]:
        monkeypatch.setenv("MO_ROUTEMOA", value)
        assert screening.enabled() is expected, value


def test_min_runs_defaults_to_8_and_only_tightens(monkeypatch):
    assert screening.min_runs() == 8
    for raw, expected in [("2", 8), ("0", 8), ("-3", 8), ("7", 8),
                          ("8", 8), ("9", 9), ("12", 12)]:
        monkeypatch.setenv("MO_ROUTEMOA_MIN_RUNS", raw)
        assert screening.min_runs() == expected, raw


# ── _pick_lane end to end ────────────────────────────────────────────────────


def test_pick_lane_screens_the_dominated_exploration_pick(tmp_path):
    # lane-bad has only 4 domain runs, so the UCB exploration bonus makes it
    # the unscreened pick; in agent_performance_memory it is 2/20 against
    # lane-good's 18/20, so the screen removes it.
    path = _make_db(
        tmp_path,
        apm_rows=[("lane-good", 18, 20, 0.0), ("lane-bad", 2, 20, 0.0)],
        domain_rows=[
            ("lane-bad", 0.10, 4, 1, 0.0),
            ("lane-good", 0.05, 40, 36, 0.0),
        ],
    )
    _seed_traces(path, [("lane-good", 18, 20), ("lane-bad", 2, 20)])
    detail = lane_router._pick_lane(TC, "", OD, "", path)
    assert detail["lane"] == "lane-good", detail
    assert detail["source"] == "domain"
    assert detail["adv"] == "0.050" and detail["runs"] == "40"
    assert [e["lane"] for e in detail["screened"]] == ["lane-bad"]
    assert detail["screened"][0]["dominated_by"] == "lane-good"
    for key in ("pick", "lane", "adv", "runs", "margin", "source"):
        assert key in detail, key
    assert lane_router.preferred_lane_detail(TC, "", OD, "", path)["screened"] == \
        detail["screened"]


def test_pick_lane_never_maps_stats_onto_a_partially_matching_slice(tmp_path):
    # One real overlap means the ids are current: the unmatched candidate must
    # not inherit the other lane's record and is therefore never screened.
    path = _make_db(
        tmp_path,
        apm_rows=[("lane-good", 18, 20, 0.0), ("lane-b", 2, 20, 0.0)],
        domain_rows=[
            ("lane-bad", 0.10, 4, 1, 0.0),
            ("lane-good", 0.05, 40, 36, 0.0),
        ],
    )
    detail = lane_router._pick_lane(TC, "", OD, "", path)
    assert detail["screened"] == []
    assert detail["lane"] == "lane-bad", detail


def test_pick_lane_opt_out_returns_the_unscreened_pick(tmp_path, monkeypatch):
    path = _make_db(
        tmp_path,
        apm_rows=[("lane-good", 18, 20, 0.0), ("lane-bad", 2, 20, 0.0)],
        domain_rows=[
            ("lane-bad", 0.10, 4, 1, 0.0),
            ("lane-good", 0.05, 40, 36, 0.0),
        ],
    )
    monkeypatch.setenv("MO_ROUTEMOA", "0")
    detail = lane_router._pick_lane(TC, "", OD, "", path)
    assert detail["lane"] == "lane-bad", detail
    assert detail["screened"] == []


def test_pick_lane_stats_read_failure_falls_back_unscreened(tmp_path):
    # No agent_performance_memory table at all: fetch_lane_run_stats raises and
    # the pick must return exactly the unscreened router's answer.
    path = _make_db(
        tmp_path,
        with_apm=False,
        domain_rows=[
            ("lane-bad", 0.10, 4, 1, 0.0),
            ("lane-good", 0.05, 40, 36, 0.0),
        ],
    )
    detail = lane_router._pick_lane(TC, "", OD, "", path)  # must not raise
    assert detail["lane"] == "lane-bad", detail
    assert detail["screened"] == []


def test_pick_lane_cold_db_has_empty_screened(tmp_path):
    path = _make_db(tmp_path)
    detail = lane_router._pick_lane(TC, "", OD, "", path)
    assert detail["pick"] == ""
    assert detail["screened"] == []
    assert lane_router.preferred_lane_detail(TC, "", OD, "", path)["screened"] == []


def test_pick_lane_equirouter_override_only_considers_kept_lanes(tmp_path):
    # The Borda winner is lane-borda-bad in both region slices; the screen
    # removes it (1/20 vs 18/20), so the override must fall back to a kept lane.
    path = _make_db(
        tmp_path,
        apm_rows=[("lane-borda-keep", 18, 20, 0.0),
                  ("lane-borda-bad", 1, 20, 0.0)],
        region_rows=[
            ("lane-borda-bad", "r1", 0.90, 4, 1, 0.0),
            ("lane-borda-keep", "r1", 0.50, 40, 36, 0.0),
            ("lane-borda-bad", "r2", 0.80, 5, 1, 0.0),
            ("lane-borda-keep", "r2", 0.40, 50, 45, 0.0),
        ],
    )
    _seed_traces(path, [("lane-borda-keep", 18, 20), ("lane-borda-bad", 1, 20)])
    detail = lane_router._pick_lane(TC, "", OD, "r1", path)
    assert detail["source"] == "region"
    assert detail["lane"] == "lane-borda-keep", detail
    assert [e["lane"] for e in detail["screened"]] == ["lane-borda-bad"]
    assert detail["screened"][0]["dominated_by"] == "lane-borda-keep"
