"""Unit tests for mini_ork.learning.advantage_store.AdvantageStore (M9).

Seeds a tmp sqlite db with minimal rows and asserts the moved SQL (schema
introspection, prior reads, source-row reads, UPSERTs, preferred_lane
candidate fetches) behaves exactly as the inline lane_router code did — plus
an integration pass through lane_router.recompute_advantages/preferred_lane
and the pure math helpers that stayed in lane_router.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import lane_router  # noqa: E402
from mini_ork.learning.advantage_store import AdvantageStore, resolve_db_path  # noqa: E402

_TRACE_SCHEMA = """
CREATE TABLE execution_traces (
  trace_id TEXT, run_id TEXT, agent_version_id TEXT, task_class TEXT,
  objective_domain TEXT, code_region TEXT, verifier_output TEXT,
  reward_g REAL, cost_usd REAL, status TEXT, created_at TEXT
);
CREATE TABLE agent_performance_memory (
  agent_version_id TEXT NOT NULL, role TEXT, model TEXT, task_class TEXT NOT NULL,
  runs_count INTEGER NOT NULL DEFAULT 0, success_count INTEGER NOT NULL DEFAULT 0,
  relative_advantage REAL NOT NULL DEFAULT 0.0,
  last_updated TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (agent_version_id, task_class)
);
"""


def _seed(db_path: Path, traces: list[tuple]) -> None:
    con = sqlite3.connect(db_path)
    con.executescript(_TRACE_SCHEMA)
    con.executemany(
        "INSERT INTO execution_traces (trace_id, agent_version_id, task_class,"
        " objective_domain, code_region, verifier_output, reward_g, cost_usd,"
        " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        traces,
    )
    con.commit()
    con.close()


def _two_lane_traces(region: str = "") -> list[tuple]:
    ts = "2026-06-01T00:00:00.000Z"
    out = []
    for i in range(3):
        out.append((f"t-a-{i}-{region}", "laneA", "code-fix", "code-delivery", region,
                    '{"node_type":"implementer"}', 1.0, 0.01, "success", ts))
        out.append((f"t-b-{i}-{region}", "laneB", "code-fix", "code-delivery", region,
                    '{"node_type":"implementer"}', 0.0, 0.05, "failure", ts))
    return out


@pytest.fixture
def seeded(tmp_path: Path) -> str:
    db_path = str(tmp_path / "state.db")
    _seed(Path(db_path), _two_lane_traces())
    return db_path


# ── path resolution ──────────────────────────────────────────────────────────


def test_resolve_db_path_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_db_path("/explicit.db") == "/explicit.db"
    monkeypatch.setenv("MINI_ORK_DB", "/env.db")
    monkeypatch.setenv("MO_STORE_DB", "/store.db")
    assert resolve_db_path(None) == "/env.db"  # MINI_ORK_DB wins
    monkeypatch.delenv("MINI_ORK_DB")
    assert resolve_db_path(None) == "/store.db"
    monkeypatch.delenv("MO_STORE_DB")
    monkeypatch.setenv("MINI_ORK_HOME", "/home-x")
    assert resolve_db_path(None) == "/home-x/state.db"


# ── introspection + source reads ─────────────────────────────────────────────


def test_execution_trace_columns_and_source_rows(seeded: str) -> None:
    with AdvantageStore(seeded) as store:
        cols = store.execution_trace_columns()
        assert {"code_region", "cost_usd", "created_at"} <= cols
        rows = store.fetch_source_rows("1970-01-01T00:00:00.000Z")
        assert len(rows) == 6
        assert {r["agent_version_id"] for r in rows} == {"laneA", "laneB"}


def test_source_rows_tolerate_missing_columns(tmp_path: Path) -> None:
    """A legacy execution_traces without code_region/cost_usd still reads."""
    db_path = tmp_path / "legacy.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE execution_traces (trace_id TEXT, agent_version_id TEXT,"
        " task_class TEXT, objective_domain TEXT, verifier_output TEXT,"
        " reward_g REAL, created_at TEXT)"
    )
    con.execute(
        "INSERT INTO execution_traces VALUES ('t-1', 'laneA', 'tc', 'od', '{}',"
        " 1.0, '2026-06-01T00:00:00.000Z')"
    )
    con.commit()
    con.close()
    with AdvantageStore(str(db_path)) as store:
        rows = store.fetch_source_rows("1970-01-01T00:00:00.000Z")
        assert len(rows) == 1
        assert rows[0]["code_region"] is None
        assert rows[0]["cost_usd"] == 0.0


# ── DDL + prior reads + upserts ──────────────────────────────────────────────


def test_ensure_tables_and_prior_roundtrip(seeded: str) -> None:
    with AdvantageStore(seeded) as store:
        store.ensure_advantage_tables()
        assert store.fetch_prior_apm() == {}
        assert store.fetch_prior_domain() == {}
        assert store.fetch_prior_region() == {}
        assert store.fetch_prior_baseline() == {}

        store.upsert_agent_performance("laneA", "implementer", "laneA", "code-fix", 3, 2, 0.5)
        store.upsert_domain_advantage("laneA", "code-fix", "implementer", "code-delivery",
                                      0.5, 3, 2, 0.1, 0.31, 1.25)
        store.upsert_region_advantage("laneA", "code-fix", "implementer", "code-delivery",
                                      "regionA", 0.4, 2, 1, 0.2, 0.44, 0.9)
        store.upsert_slice_baseline("code-delivery", "code-fix", "implementer", "",
                                    0.5, 0.25, 0.5, 2)
        store.commit()

        assert store.fetch_prior_apm() == {("laneA", "code-fix"): 0.5}
        assert store.fetch_prior_domain() == {("laneA", "code-fix", "implementer", "code-delivery"): 0.5}
        assert store.fetch_prior_region() == {
            ("laneA", "code-fix", "implementer", "code-delivery", "regionA"): 0.4
        }
        assert store.fetch_prior_baseline() == {
            ("code-delivery", "code-fix", "implementer", ""): (0.5, 0.25, 2)
        }

        # ON CONFLICT update, not a duplicate insert
        store.upsert_agent_performance("laneA", "implementer", "laneA", "code-fix", 4, 3, 0.6)
        store.commit()
        assert store.fetch_prior_apm() == {("laneA", "code-fix"): 0.6}


# ── defect probes ────────────────────────────────────────────────────────────


def test_defect_attribution_probes(seeded: str) -> None:
    with AdvantageStore(seeded) as store:
        assert store.has_defect_attributions() is False
        store.con.execute(
            "CREATE TABLE defect_attributions (lane TEXT, code_region TEXT,"
            " task_class TEXT, penalty REAL, decay_halflife_days REAL, ts TEXT)"
        )
        store.con.execute(
            "INSERT INTO defect_attributions VALUES ('laneA', 'regionA', 'code-fix',"
            " -0.5, 30.0, '2026-06-01T00:00:00.000Z')"
        )
        store.con.execute(
            "INSERT INTO defect_attributions VALUES ('laneB', 'regionA', 'code-fix',"
            " 0, 30.0, '2026-06-01T00:00:00.000Z')"
        )
        store.commit()
        assert store.has_defect_attributions() is True
        rows = store.fetch_defect_penalties()
        assert len(rows) == 1  # penalty=0 filtered out
        assert rows[0]["lane"] == "laneA"


# ── preferred_lane candidate fetches ─────────────────────────────────────────


def test_lane_candidate_fetches(seeded: str) -> None:
    with AdvantageStore(seeded) as store:
        store.ensure_advantage_tables()
        store.upsert_domain_advantage("laneA", "code-fix", "implementer", "code-delivery",
                                      0.5, 3, 3, 0.0, 0.0, 1.0)
        store.upsert_domain_advantage("laneB", "code-fix", "implementer", "code-delivery",
                                      -0.5, 3, 0, 0.0, 0.0, -1.0)
        store.upsert_domain_advantage("laneC", "code-fix", "implementer", "code-delivery",
                                      0.9, 1, 1, 0.0, 0.0, 2.0)  # below sample floor
        store.upsert_region_advantage("laneA", "code-fix", "implementer", "code-delivery",
                                      "regionA", 0.7, 4, 4, 0.0, 0.0, 1.5)
        store.commit()

        cands = store.fetch_domain_candidates("code-fix", "code-delivery", "implementer", 3)
        assert [c["agent_version_id"] for c in cands] == ["laneA", "laneB"]  # laneC floored
        assert cands[0]["adv_str"] == "0.500"

        rcands = store.fetch_region_candidates("code-fix", "code-delivery", "regionA",
                                               "implementer", 3)
        assert [c["agent_version_id"] for c in rcands] == ["laneA"]

        store.upsert_agent_performance("laneA", "implementer", "laneA", "code-fix", 5, 4, 0.5)
        store.commit()
        best = store.fetch_global_best("code-fix", "implementer", 3)
        assert best is not None and best[0] == "laneA" and best[1] == "0.500"
        assert store.fetch_global_best("code-fix", "nonexistent-role", 3) is None


# ── integration: recompute + preferred_lane via the store ────────────────────


def test_recompute_and_preferred_lane_end_to_end(seeded: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MO_LEARNING_HALFLIFE_DAYS", "0")
    monkeypatch.setenv("MO_LEARNING_MIN_SAMPLES", "1")
    upserted = lane_router.recompute_advantages(db=seeded)
    assert upserted == 2  # laneA + laneB in agent_performance_memory

    con = sqlite3.connect(seeded)
    rows = dict(
        con.execute("SELECT agent_version_id, relative_advantage FROM agent_performance_memory")
        .fetchall()
    )
    con.close()
    assert rows["laneA"] > 0 > rows["laneB"]

    pick = lane_router.preferred_lane("code-fix", "implementer", "code-delivery", db=seeded)
    assert pick.split("|")[0] == "laneA"


# ── pure math helpers that stayed in lane_router ─────────────────────────────


def test_pure_math_helpers() -> None:
    assert lane_router._recency_weight(14.0, 14.0) == pytest.approx(0.5)
    assert lane_router._recency_weight(0.0, 14.0) == pytest.approx(1.0)
    assert lane_router._shrink(0.6, 1, 5) == pytest.approx(0.1)
    assert lane_router._shrink(0.6, 1, 0) == pytest.approx(0.6)  # K<=0 disables
    assert lane_router._ema_blend(None, 0.5, 0.3) == 0.5
    assert lane_router._ema_blend(0.2, 0.5, 1.0) == 0.5
    assert lane_router._ema_blend(0.2, 0.5, 0.0) == 0.2
    assert lane_router._ema_blend(0.2, 0.5, 0.3) == pytest.approx(0.3 * 0.5 + 0.7 * 0.2)
    assert lane_router._ema_blend("junk", 0.5, 0.3) == 0.5
    assert lane_router._zscore(1.0, 0.5, 0.25) == pytest.approx(1.0)
    assert lane_router._zscore(1.0, 0.5, 0.0) == pytest.approx(500.0)  # 1e-3 floor
