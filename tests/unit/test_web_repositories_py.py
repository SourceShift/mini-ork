"""Unit tests for mini_ork.web.repositories.LearningRepository (M9).

Seeds a tmp sqlite db with minimal rows for the learning-loop tables and
asserts each moved query returns exactly what the inline SQL in
run_detail.get_learning used to return — plus the has_table-guarded empty
behaviour for a fresh db. A handler-level test pins the response shape of
get_learning so the refactor stays byte-identical.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.web.db import StateDB  # noqa: E402
from mini_ork.web.repositories import LearningRepository  # noqa: E402

_SCHEMA = """
CREATE TABLE task_runs (
  id TEXT PRIMARY KEY, task_class TEXT, recipe TEXT, status TEXT,
  trace_id TEXT, created_at INTEGER, ended_at INTEGER
);
CREATE TABLE execution_traces (
  trace_id TEXT, run_id TEXT, task_class TEXT, status TEXT,
  cost_usd REAL, duration_ms INTEGER, reviewer_verdict TEXT,
  final_artifact_ref TEXT, created_at TEXT,
  agent_version_id TEXT, verifier_output TEXT
);
CREATE TABLE gradient_records (
  gradient_id TEXT, target TEXT, signal TEXT, suggested_change TEXT,
  evidence TEXT, confidence REAL, created_at TEXT, task_class TEXT
);
CREATE TABLE pattern_records (
  pattern_id TEXT, description TEXT, evidence_trace_ids TEXT, frequency INTEGER,
  first_seen TEXT, last_seen TEXT, output_type TEXT, promoted_to TEXT, status TEXT
);
CREATE TABLE learning_record (
  id TEXT, run_id TEXT, iter INTEGER, rank INTEGER, category TEXT, title TEXT,
  evidence_paths TEXT, arxiv_refs TEXT, patch_summary TEXT, outcome TEXT,
  severity TEXT, confidence REAL, benchmark_delta REAL,
  created_at TEXT, updated_at TEXT
);
"""


def _seed(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO task_runs (id, task_class, recipe, status, trace_id, created_at, ended_at)"
        " VALUES ('run-1', 'code-fix', NULL, 'published', 'tr-classify-1', 100, 200)"
    )
    con.executemany(
        "INSERT INTO execution_traces (trace_id, run_id, task_class, status, cost_usd,"
        " duration_ms, reviewer_verdict, final_artifact_ref, created_at,"
        " agent_version_id, verifier_output) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("tr-impl-1", "run-1", "code-fix", "success", 0.01, 10, "APPROVE",
             "impl-implementer.log", "2026-06-01T00:00:00.000Z", "kimi_lens", "{}"),
            ("tr-verify-1", "run-1", "code-fix", "success", 0.02, 20, "APPROVE",
             None, "2026-06-01T00:01:00.000Z", "glm_lens", "{}"),
            # a PRIOR run's trace with the same task_class
            ("tr-old-1", "run-0", "code-fix", "failure", 0.05, 50, "REJECT",
             None, "2026-05-01T00:00:00.000Z", "kimi_lens", "{}"),
            # different task_class — must not appear in prior_similar_runs
            ("tr-other-1", "run-9", "blog-post", "success", 0.03, 30, "APPROVE",
             None, "2026-05-02T00:00:00.000Z", "opus", "{}"),
        ],
    )
    con.executemany(
        "INSERT INTO gradient_records (gradient_id, target, signal, suggested_change,"
        " evidence, confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,?)",
        [
            # produced by this run (evidence cites this run's node trace)
            ("g-produced", "workflow.node.verify", "low pass rate", "tighten rubric",
             "tr-impl-1", 0.8, "2026-06-01T00:02:00.000Z", "code-fix"),
            # injectable failure mode (task_class match, high confidence)
            ("g-injectable", "workflow.node.plan", "planner drift", "add constraint",
             "tr-old-1", 0.9, "2026-05-01T00:02:00.000Z", "code-fix"),
            # target-LIKE match without task_class match
            ("g-like", "code-fix.prompt", "style", "rephrase",
             "tr-old-2", 0.7, "2026-05-01T00:03:00.000Z", "other-class"),
            # below the 0.6 confidence floor — never injectable
            ("g-lowconf", "workflow.node.verify", "noise", "ignore",
             "tr-old-3", 0.3, "2026-05-01T00:04:00.000Z", "code-fix"),
            # unrelated
            ("g-unrelated", "blog.style", "tone", "warm up",
             "tr-other-1", 0.9, "2026-05-02T00:02:00.000Z", "blog-post"),
        ],
    )
    con.executemany(
        "INSERT INTO pattern_records (pattern_id, description, evidence_trace_ids,"
        " frequency, first_seen, last_seen, output_type, promoted_to, status)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("p-hit", "verify loops on rubric", '["tr-classify-1", "tr-old-1"]', 4,
             "2026-05-01", "2026-06-01", "markdown", None, "active"),
            # LIKE over-match: substring hits but the parsed array does not
            ("p-overmatch", "substring trap", '["tr-classify-1-extra"]', 9,
             "2026-05-01", "2026-06-01", "markdown", None, "active"),
            ("p-miss", "unrelated cluster", '["tr-other-1"]', 2,
             "2026-05-01", "2026-05-02", "markdown", None, "active"),
        ],
    )
    con.executemany(
        "INSERT INTO learning_record (id, run_id, iter, rank, category, title,"
        " evidence_paths, arxiv_refs, patch_summary, outcome, severity, confidence,"
        " benchmark_delta, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("lr-1", "run-1", 1, 1, "fix", "Tighten verify rubric",
             '["a.md"]', '["2601.00001"]', "patch verify", "applied",
             "medium", 0.8, 0.02, "2026-06-01", "2026-06-01"),
        ],
    )
    con.commit()
    con.close()


@pytest.fixture
def seeded(tmp_path: Path) -> LearningRepository:
    db_path = tmp_path / "state.db"
    _seed(db_path)
    return LearningRepository(StateDB(db_path))


@pytest.fixture
def empty(tmp_path: Path) -> LearningRepository:
    """A db with ONLY task_runs — the learning tables are absent, exercising the
    has_table guards. (task_runs itself is queried unguarded, as the handler
    always did: the web app only runs where state.db has task_runs.)"""
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, task_class TEXT, recipe TEXT,"
                " status TEXT, trace_id TEXT, created_at INTEGER, ended_at INTEGER)")
    con.commit()
    con.close()
    return LearningRepository(StateDB(db_path))


def test_fetch_task_run(seeded: LearningRepository) -> None:
    tr = seeded.fetch_task_run("run-1")
    assert tr is not None
    assert tr["task_class"] == "code-fix"
    assert tr["trace_id"] == "tr-classify-1"
    assert seeded.fetch_task_run("nope") is None


def test_fetch_execution_traces_scoped_to_run(seeded: LearningRepository) -> None:
    ids = seeded.fetch_execution_traces("run-1")
    assert sorted(ids) == ["tr-impl-1", "tr-verify-1"]


def test_fetch_gradient_records_by_evidence(seeded: LearningRepository) -> None:
    rows = seeded.fetch_gradient_records(["tr-impl-1", "tr-verify-1", "tr-classify-1"])
    assert [r["gradient_id"] for r in rows] == ["g-produced"]
    assert rows[0]["evidence"] == "tr-impl-1"
    assert seeded.fetch_gradient_records([]) == []


def test_fetch_failure_mode_gradients_mirror_context_assembler(seeded: LearningRepository) -> None:
    rows = seeded.fetch_failure_mode_gradients("code-fix")
    ids = {r["gradient_id"] for r in rows}
    # task_class match OR target LIKE, confidence >= 0.6
    assert ids == {"g-produced", "g-injectable", "g-like"}


def test_fetch_pattern_candidates_is_a_like_prefilter(seeded: LearningRepository) -> None:
    rows = seeded.fetch_pattern_candidates("tr-classify-1")
    ids = {r["pattern_id"] for r in rows}
    # LIKE matches both the true hit and the substring over-match; the handler
    # re-checks membership against the parsed array.
    assert ids == {"p-hit", "p-overmatch"}


def test_fetch_learning_records(seeded: LearningRepository) -> None:
    rows = seeded.fetch_learning_records("run-1")
    assert [r["id"] for r in rows] == ["lr-1"]
    assert rows[0]["evidence_paths"] == '["a.md"]'  # raw; parsing stays in the handler
    assert seeded.fetch_learning_records("run-0") == []


def test_fetch_prior_similar_runs_excludes_own_traces(seeded: LearningRepository) -> None:
    own = ["tr-impl-1", "tr-verify-1", "tr-classify-1"]
    rows = seeded.fetch_prior_similar_runs("code-fix", own, "run-1")
    assert [r["trace_id"] for r in rows] == ["tr-old-1"]


def test_fetch_trace_summaries(seeded: LearningRepository) -> None:
    out = seeded.fetch_trace_summaries(["tr-impl-1", "tr-missing", ""])
    assert set(out) == {"tr-impl-1"}
    assert out["tr-impl-1"]["agent_version_id"] == "kimi_lens"


def test_gradient_count(seeded: LearningRepository) -> None:
    assert seeded.gradient_count() == 5


def test_empty_db_returns_empty_not_error(empty: LearningRepository) -> None:
    assert not empty.has_table("gradient_records")
    assert empty.fetch_task_run("run-1") is None
    assert empty.fetch_execution_traces("run-1") == []
    assert empty.fetch_gradient_records(["tr-1"]) == []
    assert empty.fetch_failure_mode_gradients("code-fix") == []
    assert empty.fetch_pattern_candidates("tr-1") == []
    assert empty.fetch_learning_records("run-1") == []
    assert empty.fetch_prior_similar_runs("code-fix", [""], "run-1") == []
    assert empty.fetch_trace_summaries(["tr-1"]) == {}
    assert empty.gradient_count() == 0


def test_get_learning_handler_shape_preserved(tmp_path: Path) -> None:
    """Handler-level guard: classification + response shaping unchanged."""
    from mini_ork.web.routes.run_detail import get_learning

    db_path = tmp_path / "state.db"
    _seed(db_path)
    out = get_learning(task_run_id="run-1", db=StateDB(db_path))

    assert out["task_run_id"] == "run-1"
    assert out["task_class"] == "code-fix"
    assert out["trace_id"] == "tr-classify-1"
    assert out["summary"] == {
        "gradients_produced": 1,
        "patterns_evidenced": 1,  # p-overmatch filtered out by the membership re-check
        "learning_records": 1,
        "prior_similar_runs_available": 1,
        "known_failure_modes_available": 3,
    }
    assert [g["gradient_id"] for g in out["produced"]["gradients"]] == ["g-produced"]
    assert [p["pattern_id"] for p in out["produced"]["patterns"]] == ["p-hit"]
    assert out["produced"]["patterns"][0]["evidence_trace_ids"] == ["tr-classify-1", "tr-old-1"]
    assert [r["id"] for r in out["self_improve"]["records"]] == ["lr-1"]
    assert out["self_improve"]["records"][0]["evidence_paths"] == ["a.md"]
    assert [r["trace_id"] for r in out["injected_candidates"]["prior_similar_runs"]] == ["tr-old-1"]
    assert len(out["injected_candidates"]["injection_points"]) == 4
    # attribution enrichment still applied
    for row in out["produced"]["gradients"]:
        assert "agent_attribution" in row


def test_get_learning_404_on_unknown_run(tmp_path: Path) -> None:
    from fastapi import HTTPException

    from mini_ork.web.routes.run_detail import get_learning

    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, task_class TEXT, recipe TEXT,"
                " status TEXT, trace_id TEXT, created_at INTEGER, ended_at INTEGER)")
    con.commit()
    con.close()
    with pytest.raises(HTTPException) as exc:
        get_learning(task_run_id="nope", db=StateDB(db_path))
    assert exc.value.status_code == 404
