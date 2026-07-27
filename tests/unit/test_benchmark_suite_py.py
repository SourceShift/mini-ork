"""Unit tests for mini_ork.learning.benchmark_suite.

Schema bootstrap: the canonical schema comes from the migrations applied by
the native ``mini_ork.stores.migrate.init_db`` (migration 0010 gives
benchmark_tasks / benchmark_results / epics / runs). The lib's private
``_bench_ensure_tables`` DDL had the wrong column names and is deliberately
NOT used — only the migrated schema is correct.

Cases:
  (a) add happy path                    → returns bench_id; row queryable
  (b) add with id collision             → ON CONFLICT update (re-add same id, new task_class)
  (c) add missing benchmark_id + class  → ValueError
  (d) add with invalid JSON             → ValueError
  (e) list_ returns all added tasks     → 3 rows, ORDER BY task_class, benchmark_id
  (f) list_ with task_class filter      → 2 of 3 (filters correctly)
  (g) run with no runner                → skipped summary, util = baseline, all_pass=False
  (h) results returns rows for a cand.  → bench_results rows present
  (i) run with fake runner_fn           → util_score=0.5 + passed=True (all_pass=True)
  (j) run on empty task table           → total_tasks=0 (no crash)
  (k) native runner shapes              → dict / (rc, stdout) tuple / exception
  (l) string "utility_score" runner     → staged mini_ork.learning.utility_function
  (m) legacy shell-snippet runner_fn    → graceful per-task error
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.learning import benchmark_suite as bench  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402

_FLOAT_TOL = 1e-6


@pytest.fixture
def db(tmp_path_factory):
    """Bootstrap a fresh DB via the native init_db (full migration 0010
    schema — benchmark_tasks, benchmark_results, epics, runs)."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    return dbp


# ─────────────────────────────────────────────────────────────────────────────
# (a) add happy path — returns bench_id, row queryable
# ─────────────────────────────────────────────────────────────────────────────
def test_add_happy_path(db):
    payload = (
        '{"id":"bt-001","task_class":"unit-test","input":{"x":1},'
        '"baseline_utility_score":0.5}'
    )
    assert bench.add(payload, db=db) == "bt-001"

    listed = bench.list_(db=db)
    assert len(listed) == 1
    assert listed[0]["benchmark_id"] == "bt-001"
    assert listed[0]["task_class"] == "unit-test"
    assert abs(listed[0]["baseline_utility_score"] - 0.5) <= _FLOAT_TOL


# ─────────────────────────────────────────────────────────────────────────────
# (b) add with id collision — ON CONFLICT update path
# ─────────────────────────────────────────────────────────────────────────────
def test_add_id_collision_updates(db):
    first = '{"id":"bt-collide","task_class":"first","baseline_utility_score":0.4}'
    second = '{"id":"bt-collide","task_class":"second","baseline_utility_score":0.9}'

    assert bench.add(first, db=db) == "bt-collide"
    # ON CONFLICT update — same id is returned both times, but task_class flips
    assert bench.add(second, db=db) == "bt-collide"

    listed = bench.list_(db=db)
    assert len(listed) == 1
    assert listed[0]["task_class"] == "second"
    assert abs(listed[0]["baseline_utility_score"] - 0.9) <= _FLOAT_TOL


# ─────────────────────────────────────────────────────────────────────────────
# (c) add missing benchmark_id + task_class → ValueError
# ─────────────────────────────────────────────────────────────────────────────
def test_add_missing_fields_raises(db):
    with pytest.raises(ValueError):
        bench.add('{"input":"no-id-no-class"}', db=db)


# ─────────────────────────────────────────────────────────────────────────────
# (d) add with invalid JSON → ValueError
# ─────────────────────────────────────────────────────────────────────────────
def test_add_invalid_json_raises(db):
    with pytest.raises(ValueError):
        bench.add("bad-json", db=db)


# ─────────────────────────────────────────────────────────────────────────────
# (e) list_ returns all added tasks (3 rows, sorted by task_class, benchmark_id)
# ─────────────────────────────────────────────────────────────────────────────
def test_list_all(db):
    seed = [
        '{"id":"bt-a","task_class":"alpha","baseline_utility_score":0.1}',
        '{"id":"bt-b","task_class":"beta","baseline_utility_score":0.2}',
        '{"id":"bt-c","task_class":"alpha","baseline_utility_score":0.3}',
    ]
    for s in seed:
        bench.add(s, db=db)

    listed = bench.list_(db=db)
    assert [r["benchmark_id"] for r in listed] == ["bt-a", "bt-c", "bt-b"]
    assert [r["task_class"] for r in listed] == ["alpha", "alpha", "beta"]
    assert [r["baseline_utility_score"] for r in listed] == [0.1, 0.3, 0.2]


# ─────────────────────────────────────────────────────────────────────────────
# (f) list_ with task_class filter → subset (2 of 3)
# ─────────────────────────────────────────────────────────────────────────────
def test_list_task_class_filter(db):
    seed = [
        '{"id":"bt-f1","task_class":"alpha","baseline_utility_score":0.1}',
        '{"id":"bt-f2","task_class":"beta","baseline_utility_score":0.2}',
        '{"id":"bt-f3","task_class":"alpha","baseline_utility_score":0.3}',
    ]
    for s in seed:
        bench.add(s, db=db)

    filtered = bench.list_(task_class="alpha", db=db)
    assert [r["benchmark_id"] for r in filtered] == ["bt-f1", "bt-f3"]


# ─────────────────────────────────────────────────────────────────────────────
# (g) run with no runner → skipped summary, all_pass=False, avg=baseline_mean
# ─────────────────────────────────────────────────────────────────────────────
def test_run_no_runner_summary(db):
    seed = [
        '{"id":"bt-r1","task_class":"u","baseline_utility_score":0.4}',
        '{"id":"bt-r2","task_class":"u","baseline_utility_score":0.6}',
        '{"id":"bt-r3","task_class":"u","baseline_utility_score":0.8}',
    ]
    for s in seed:
        bench.add(s, db=db)

    summary = bench.run("cand-skip", db=db)

    assert summary["candidate_id"] == "cand-skip"
    assert summary["total_tasks"] == 3
    assert summary["passed"] == 0
    assert summary["failed"] == 3
    assert summary["all_pass"] is False
    # baseline mean = (0.4 + 0.6 + 0.8) / 3 = 0.6
    assert abs(summary["avg_utility_score"] - 0.6) <= _FLOAT_TOL
    # each task: skipped, util = its baseline, error recorded
    for r, baseline in zip(summary["results"], (0.4, 0.6, 0.8)):
        assert r["passed"] is False
        assert abs(r["utility_score"] - baseline) <= _FLOAT_TOL
        assert r["error"] == "runner not configured"


# ─────────────────────────────────────────────────────────────────────────────
# (h) results returns rows for a candidate after a run
# ─────────────────────────────────────────────────────────────────────────────
def test_results_returns_rows(db):
    bench.add('{"id":"bt-rs1","task_class":"u","baseline_utility_score":0.5}', db=db)
    bench.run("cand-rs", db=db)

    # stable columns land in benchmark_results
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT benchmark_id, candidate_id, pass, utility_score "
        "FROM benchmark_results ORDER BY benchmark_id"
    ).fetchall()
    con.close()
    assert len(rows) == 1
    benchmark_id, candidate_id, pass_int, util = rows[0]
    assert benchmark_id == "bt-rs1"
    assert candidate_id == "cand-rs"
    assert pass_int in (0, 1)
    assert isinstance(util, float)

    res = bench.results("cand-rs", db=db)
    assert len(res) == 1
    assert res[0]["benchmark_id"] == "bt-rs1"
    assert res[0]["candidate_id"] == "cand-rs"


# ─────────────────────────────────────────────────────────────────────────────
# (i) run with a fake runner_fn — util_score=0.5 + passed=True (all_pass=True)
# ─────────────────────────────────────────────────────────────────────────────
def test_run_with_runner_fn(db):
    seed = [
        '{"id":"bt-rn1","task_class":"u","baseline_utility_score":0.1}',
        '{"id":"bt-rn2","task_class":"u","baseline_utility_score":0.2}',
    ]
    for s in seed:
        bench.add(s, db=db)

    # Native runner contract: a callable returning a string is the
    # stdout-equivalent payload; the parse pipeline
    # (json.loads → utility_score/passed) is the shared code path.
    summary = bench.run(
        "cand-runner",
        runner_fn=lambda t: '{"utility_score":0.5,"passed":true}',
        db=db,
    )

    assert summary["total_tasks"] == 2
    assert summary["passed"] == 2
    assert summary["all_pass"] is True
    assert abs(summary["avg_utility_score"] - 0.5) <= _FLOAT_TOL
    for r in summary["results"]:
        assert r["passed"] is True
        assert abs(r["utility_score"] - 0.5) <= _FLOAT_TOL


# ─────────────────────────────────────────────────────────────────────────────
# (j) run on an EMPTY task table → total_tasks=0 (no crash)
# ─────────────────────────────────────────────────────────────────────────────
def test_run_empty_task_table(db):
    summary = bench.run("cand-empty", db=db)
    assert summary["total_tasks"] == 0
    assert summary["passed"] == 0
    assert summary["failed"] == 0
    assert summary["avg_utility_score"] == 0.0
    assert summary["results"] == []


# ─────────────────────────────────────────────────────────────────────────────
# (k) [WS5] native runner shapes — dict / (rc, stdout) tuple / exception
# ─────────────────────────────────────────────────────────────────────────────
def test_run_native_callable_runner_shapes(db):
    bench.add('{"id":"bt-nc1","task_class":"u","baseline_utility_score":0.1}',
              db=db)
    bench.add('{"id":"bt-nc2","task_class":"u","baseline_utility_score":0.2}',
              db=db)

    seen = []

    def dict_runner(t):
        seen.append(t["benchmark_id"])
        return {"utility_score": 0.7, "passed": True}

    summary = bench.run("cand-nc-dict", runner_fn=dict_runner, db=db)
    assert sorted(seen) == ["bt-nc1", "bt-nc2"]
    assert summary["all_pass"] is True
    assert abs(summary["avg_utility_score"] - 0.7) <= _FLOAT_TOL
    for r in summary["results"]:
        assert r["passed"] is True
        assert r["error"] is None

    def tuple_runner(t):
        # (rc, stdout) — subprocess semantics
        return (0, '{"utility_score":0.3,"passed":false}')

    summary2 = bench.run("cand-nc-tuple", runner_fn=tuple_runner, db=db)
    assert summary2["passed"] == 0
    assert all(abs(r["utility_score"] - 0.3) <= _FLOAT_TOL
               for r in summary2["results"])

    def boom(t):
        raise RuntimeError("runner exploded")

    summary3 = bench.run("cand-nc-boom", runner_fn=boom, db=db)
    assert summary3["passed"] == 0
    assert all(r["error"] == "runner exploded" for r in summary3["results"])
    assert all(r["passed"] is False for r in summary3["results"])


# ─────────────────────────────────────────────────────────────────────────────
# (l) [WS5] string "utility_score" → staged mini_ork.learning.utility_function
# ─────────────────────────────────────────────────────────────────────────────
def test_run_utility_score_string_native(db):
    from mini_ork.learning import utility_function as uf

    bench.add(
        '{"id":"bt-us1","task_class":"u","success":true,"verifier_score":0.8,'
        '"quality_score":0.6,"cost_usd":0.1,"max_cost_usd":1.0,'
        '"duration_ms":100,"max_duration_ms":1000}',
        db=db,
    )
    summary = bench.run("cand-us", runner_fn="utility_score", db=db)
    r = summary["results"][0]
    # The emulated stdout is the bare float f"{U:.6f}" — the shared parse
    # path then behaves exactly like the legacy one: json.loads succeeds
    # (float), data.get raises, so util falls back to 1.0 when passed.
    assert r["passed"] is True
    assert r["error"] is None
    assert abs(r["utility_score"] - 1.0) <= _FLOAT_TOL
    # Cross-check the score the native port computed on the task payload —
    # the DB row carries no flat success/verifier fields, so the default
    # formula applies: 0.45*0 + 0.20*0 + 0.15*0.5 (quality default) = 0.075.
    task = [t for t in bench.list_(db=db) if t["benchmark_id"] == "bt-us1"][0]
    expected = uf.score(json.dumps(task))
    assert 0.0 <= expected <= 1.0
    assert abs(expected - 0.075) <= 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (m) [WS5] legacy shell-snippet runner_fn → graceful per-task error
# ─────────────────────────────────────────────────────────────────────────────
def test_run_legacy_snippet_rejected(db):
    bench.add('{"id":"bt-lg1","task_class":"u","baseline_utility_score":0.1}',
              db=db)
    summary = bench.run("cand-lg", runner_fn="echo hi", db=db)
    assert summary["total_tasks"] == 1
    assert summary["passed"] == 0
    r = summary["results"][0]
    assert r["passed"] is False
    assert "shell snippet" in r["error"]
    assert r["utility_score"] == 0.0
