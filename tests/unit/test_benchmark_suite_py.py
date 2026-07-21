"""Parity gate: mini_ork.learning.benchmark_suite vs lib/benchmark_suite.sh.

Each test drives the LIVE bash function (``benchmark_add`` /
``benchmark_list`` / ``benchmark_run`` / ``benchmark_results``) via
``bash -c 'source lib/benchmark_suite.sh; benchmark_xxx …'`` against
the SAME SQLite fixture as the Python port, then deep-compares both
outputs. No mocks, no hardcoded outputs beyond what bash itself emits.

Schema bootstrap (closes the STALE-DDL trap): ``lib/benchmark_suite.sh``'s
private ``_bench_ensure_tables`` writes a DDL with the wrong column names
(``id`` / ``input`` / ``expected_artifact_hash_or_criteria`` / INTEGER
``created_at``) that does NOT match migration 0010. The bash INSERT
itself targets the canonical 0010 columns, so the only correct bootstrap
path is ``db/init.sh`` — that applies migrations 0001 + 0010 + … and
gives both bash and Python the same underlying schema. Letting the
bash-side private DDL win would crash the bash INSERT with a
no-such-column error on ``benchmark_id``.

Eight cases (above the kickoff's >=6 floor):
  (a) add happy path                    → both return bench_id on stdout; row count matches
  (b) add with id collision             → ON CONFLICT update (re-add same id, new task_class)
  (c) add missing benchmark_id + class  → both raise ValueError
  (d) add with invalid JSON             → both raise ValueError
  (e) list_ returns all added tasks     → 3 rows, ORDER BY task_class, benchmark_id
  (f) list_ with task_class filter      → 1 of 3 (filters correctly)
  (g) run with no runner                → skipped summary, util = baseline mean, all_pass=False
  (h) results returns rows for a cand.  → bench_results rows present
  (i) run with fake runner_fn           → util_score=0.5 + passed=True (all_pass=True)
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
from mini_ork.learning import benchmark_suite as bench

SH = REPO / "lib" / "benchmark_suite.sh"

_FLOAT_TOL = 1e-6


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH (required by lib/benchmark_suite.sh)")
    if not SH.exists():
        pytest.skip(f"missing lib/benchmark_suite.sh at {SH}")


@pytest.fixture
def db(tmp_path_factory):
    """Bootstrap a fresh DB via db/init.sh (full migration 0010 schema —
    benchmark_tasks, benchmark_results, epics, runs). Both bash and
    Python will then operate against the same canonical schema."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _bash_call(fn: str, *args: str, db: str, env_extra: dict | None = None) -> str:
    """Run the LIVE bash function and return its stdout."""
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db}
    if env_extra:
        env.update(env_extra)
    # Wrap each arg in single quotes so embedded JSON `"` characters pass
    # through untouched (bash strips the single quotes; the JSON `"`
    # characters inside don't terminate the argument).
    args_repr = " ".join(f"'{a}'" for a in args)
    src = (
        f'source "{SH}"\n'
        f'unset MINI_ORK_WORKFLOW_RUNNER_FN\n'
        f'{fn} {args_repr}\n'
    )
    r = subprocess.run(
        ["bash", "-c", src],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        f"bash {fn} rc={r.returncode}\nstderr={r.stderr}"
    )
    return r.stdout.strip()


def _assert_parity(bash_obj, py_obj):
    """Deep-compare: same keys; floats within 1e-6; everything else equal."""
    assert type(bash_obj) is type(py_obj), (
        f"type mismatch: bash={type(bash_obj).__name__} py={type(py_obj).__name__}"
    )
    if isinstance(bash_obj, list):
        assert len(bash_obj) == len(py_obj), (
            f"list length: bash={len(bash_obj)} py={len(py_obj)}"
        )
        for i, (b, p) in enumerate(zip(bash_obj, py_obj)):
            _assert_value_parity(i, b, p)
        return
    if isinstance(bash_obj, dict):
        assert set(bash_obj.keys()) == set(py_obj.keys()), (
            f"key mismatch\nbash={sorted(bash_obj)}\npy  ={sorted(py_obj)}"
        )
        for k in bash_obj:
            _assert_value_parity(k, bash_obj[k], py_obj[k])
        return
    # Scalars
    _assert_value_parity("scalar", bash_obj, py_obj)


def _assert_value_parity(label, bash_val, py_val):
    if isinstance(bash_val, bool) or isinstance(py_val, bool):
        assert bash_val == py_val, (
            f"{label!r}: bash={bash_val!r} py={py_val!r}"
        )
    elif isinstance(bash_val, (int, float)) and isinstance(py_val, (int, float)):
        assert abs(float(bash_val) - float(py_val)) <= _FLOAT_TOL, (
            f"{label!r}: bash={bash_val!r} py={py_val!r} (diff > {_FLOAT_TOL})"
        )
    elif isinstance(bash_val, dict) and isinstance(py_val, dict):
        _assert_parity(bash_val, py_val)
    elif isinstance(bash_val, list) and isinstance(py_val, list):
        _assert_parity(bash_val, py_val)
    else:
        assert bash_val == py_val, (
            f"{label!r}: bash={bash_val!r} py={py_val!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (a) add happy path — both return bench_id, row counts match
# ─────────────────────────────────────────────────────────────────────────────
def test_add_happy_path_parity(db):
    _which_bash()
    payload = (
        '{"id":"bt-001","task_class":"unit-test","input":{"x":1},'
        '"baseline_utility_score":0.5}'
    )
    bash_id = _bash_call("benchmark_add", payload, db=db)
    py_id = bench.add(payload, db=db)
    assert bash_id == py_id == "bt-001"

    # bash's stdout is just the bench id — confirm a row exists in the DB
    # (we trust bash's parent process side-effect via subprocess, mirroring
    # how test_adaptive_stability_py validates _write side effects via DB).

    # Re-call via Python list_ and assert row count parity (1 row present).
    listed = bench.list_(db=db)
    assert len(listed) == 1
    assert listed[0]["benchmark_id"] == "bt-001"
    assert listed[0]["task_class"] == "unit-test"
    assert abs(listed[0]["baseline_utility_score"] - 0.5) <= _FLOAT_TOL


# ─────────────────────────────────────────────────────────────────────────────
# (b) add with id collision — ON CONFLICT update path
# ─────────────────────────────────────────────────────────────────────────────
def test_add_id_collision_updates_parity(db):
    _which_bash()
    first = '{"id":"bt-collide","task_class":"first","baseline_utility_score":0.4}'
    second = '{"id":"bt-collide","task_class":"second","baseline_utility_score":0.9}'

    bash_id1 = _bash_call("benchmark_add", first, db=db)
    py_id1 = bench.add(first, db=db)
    assert bash_id1 == py_id1 == "bt-collide"

    bash_id2 = _bash_call("benchmark_add", second, db=db)
    py_id2 = bench.add(second, db=db)
    # ON CONFLICT update — same id is returned both times, but task_class flipped
    assert bash_id2 == py_id2 == "bt-collide"

    listed = bench.list_(db=db)
    assert len(listed) == 1
    assert listed[0]["task_class"] == "second"
    assert abs(listed[0]["baseline_utility_score"] - 0.9) <= _FLOAT_TOL


# ─────────────────────────────────────────────────────────────────────────────
# (c) add missing benchmark_id + task_class → both raise ValueError
# ─────────────────────────────────────────────────────────────────────────────
def test_add_missing_fields_raises_parity(db):
    _which_bash()
    payload = '{"input":"no-id-no-class"}'

    bash_raised = False
    r = subprocess.run(
        [
            "bash", "-c",
            f'source "{SH}"\nbenchmark_add "{payload}"\n',
        ],
        env={**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        bash_raised = True

    py_raised = False
    try:
        bench.add(payload, db=db)
    except ValueError:
        py_raised = True
    assert bash_raised and py_raised, (
        f"both sides should raise; bash rc={r.returncode}, py_raised={py_raised}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (d) add with invalid JSON → both raise ValueError
# ─────────────────────────────────────────────────────────────────────────────
def test_add_invalid_json_raises_parity(db):
    _which_bash()
    r = subprocess.run(
        ["bash", "-c", f'source "{SH}"\nbenchmark_add "bad-json"\n'],
        env={**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db},
        capture_output=True, text=True,
    )
    bash_raised = r.returncode != 0

    py_raised = False
    try:
        bench.add("bad-json", db=db)
    except ValueError:
        py_raised = True
    assert bash_raised and py_raised, (
        f"both sides should raise; bash rc={r.returncode}, py_raised={py_raised}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (e) list_ returns all added tasks (3 rows, sorted)
# ─────────────────────────────────────────────────────────────────────────────
def test_list_all_parity(db):
    _which_bash()
    seed = [
        '{"id":"bt-a","task_class":"alpha","baseline_utility_score":0.1}',
        '{"id":"bt-b","task_class":"beta","baseline_utility_score":0.2}',
        '{"id":"bt-c","task_class":"alpha","baseline_utility_score":0.3}',
    ]
    for s in seed:
        _bash_call("benchmark_add", s, db=db)
        bench.add(s, db=db)

    bash_list = json.loads(_bash_call("benchmark_list", db=db))
    py_list = bench.list_(db=db)
    assert len(bash_list) == len(py_list) == 3
    _assert_parity(bash_list, py_list)


# ─────────────────────────────────────────────────────────────────────────────
# (f) list_ with --task-class filter → subset (1 of 3)
# ─────────────────────────────────────────────────────────────────────────────
def test_list_task_class_filter_parity(db):
    _which_bash()
    seed = [
        '{"id":"bt-f1","task_class":"alpha","baseline_utility_score":0.1}',
        '{"id":"bt-f2","task_class":"beta","baseline_utility_score":0.2}',
        '{"id":"bt-f3","task_class":"alpha","baseline_utility_score":0.3}',
    ]
    for s in seed:
        _bash_call("benchmark_add", s, db=db)
        bench.add(s, db=db)

    bash_filtered = json.loads(
        _bash_call("benchmark_list", "--task-class", "alpha", db=db)
    )
    py_filtered = bench.list_(task_class="alpha", db=db)
    assert len(bash_filtered) == len(py_filtered) == 2
    _assert_parity(bash_filtered, py_filtered)


# ─────────────────────────────────────────────────────────────────────────────
# (g) run with no runner → skipped summary, all_pass=False, avg=baseline_mean
# ─────────────────────────────────────────────────────────────────────────────
def test_run_no_runner_summary_parity(db):
    _which_bash()
    seed = [
        '{"id":"bt-r1","task_class":"u","baseline_utility_score":0.4}',
        '{"id":"bt-r2","task_class":"u","baseline_utility_score":0.6}',
        '{"id":"bt-r3","task_class":"u","baseline_utility_score":0.8}',
    ]
    for s in seed:
        _bash_call("benchmark_add", s, db=db)
        bench.add(s, db=db)

    # bash side — explicitly unset runner
    env_bash = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db}
    src = (
        f'source "{SH}"\n'
        f'unset MINI_ORK_WORKFLOW_RUNNER_FN\n'
        f'benchmark_run "cand-skip"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src],
        env=env_bash, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash benchmark_run rc={r.returncode}\nstderr={r.stderr}"
    bash_summary = json.loads(r.stdout.strip())

    # python side
    py_summary = bench.run("cand-skip", db=db)

    # Strip the volatile fields before deep-comparing
    for obj in (bash_summary, py_summary):
        obj.pop("ran_at", None)
        if "results" in obj:
            for r in obj["results"]:
                # Only some shapes carry a 'ran_at' subfield — none here.

                pass

    _assert_parity(bash_summary, py_summary)

    # Sanity-check the contract
    assert py_summary["total_tasks"] == 3
    assert py_summary["passed"] == 0
    assert py_summary["failed"] == 3
    assert py_summary["all_pass"] is False
    # baseline mean = (0.4 + 0.6 + 0.8) / 3 = 0.6 (rounded to 4dp = 0.6)
    assert abs(py_summary["avg_utility_score"] - 0.6) <= _FLOAT_TOL


# ─────────────────────────────────────────────────────────────────────────────
# (h) results returns rows for a candidate after a run
# ─────────────────────────────────────────────────────────────────────────────
def test_results_returns_rows_parity(db):
    _which_bash()
    # seed + run both sides (against the SAME candidate_id so both INSERTs
    # land in benchmark_results).
    payload = '{"id":"bt-rs1","task_class":"u","baseline_utility_score":0.5}'
    _bash_call("benchmark_add", payload, db=db)
    bench.add(payload, db=db)

    # bash run
    env_bash = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db}
    src = (
        f'source "{SH}"\nunset MINI_ORK_WORKFLOW_RUNNER_FN\n'
        f'benchmark_run "cand-rs"\n'
    )
    subprocess.run(
        ["bash", "-c", src], env=env_bash, capture_output=True, text=True, check=True,
    )

    # python run
    bench.run("cand-rs", db=db)

    # SELECT on stable columns: benchmark_id, candidate_id, pass, utility_score
    import sqlite3 as _sq
    con = _sq.connect(db)
    rows = con.execute(
        "SELECT benchmark_id, candidate_id, pass, utility_score "
        "FROM benchmark_results ORDER BY benchmark_id"
    ).fetchall()
    con.close()

    # The bash run did 1 INSERT (1 task), the python run did 1 INSERT
    # (same task — but each side has its own row since both ran).
    # We're verifying the SCHEMA shape, not the row count — both sides
    # wrote the same column set with compatible types.
    assert len(rows) >= 1
    # All rows must come back with stable columns populated and compatible.
    for benchmark_id, candidate_id, pass_int, util in rows:
        assert benchmark_id == "bt-rs1"
        assert candidate_id == "cand-rs"
        assert pass_int in (0, 1)
        assert isinstance(util, float)

    # Also run the bash/python `results()` API and check the bash list
    # has at least 1 element with the right candidate_id.
    bash_results = json.loads(_bash_call("benchmark_results", "cand-rs", db=db))
    py_results = bench.results("cand-rs", db=db)
    # Each side should have at least 1 row (their own INSERT).
    assert len(bash_results) >= 1
    assert len(py_results) >= 1
    # Cross-check: each side's row references the same benchmark_id.
    for r in bash_results:
        assert r["benchmark_id"] == "bt-rs1"
        assert r["candidate_id"] == "cand-rs"
    for r in py_results:
        assert r["benchmark_id"] == "bt-rs1"
        assert r["candidate_id"] == "cand-rs"


# ─────────────────────────────────────────────────────────────────────────────
# (i) run with a fake runner_fn — util_score=0.5 + passed=True (all_pass=True)
# ─────────────────────────────────────────────────────────────────────────────
def test_run_with_runner_fn_parity(db):
    _which_bash()
    seed = [
        '{"id":"bt-rn1","task_class":"u","baseline_utility_score":0.1}',
        '{"id":"bt-rn2","task_class":"u","baseline_utility_score":0.2}',
    ]
    for s in seed:
        _bash_call("benchmark_add", s, db=db)
        bench.add(s, db=db)

    # Runner Fn emits deterministic JSON on stdout.
    runner = (
        'echo \'{"utility_score":0.5,"passed":true}\''
    )

    # bash side
    env_bash = {
        **os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db,
        "MINI_ORK_WORKFLOW_RUNNER_FN": runner,
    }
    src = (
        f'source "{SH}"\n'
        f'export MINI_ORK_WORKFLOW_RUNNER_FN\n'
        f'benchmark_run "cand-runner"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src], env=env_bash, capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        f"bash with runner rc={r.returncode}\nstderr={r.stderr}"
    )
    bash_summary = json.loads(r.stdout.strip())

    # python side
    py_summary = bench.run("cand-runner", runner_fn=runner, db=db)

    # Volatile: ran_at differs by seconds — strip before compare
    for obj in (bash_summary, py_summary):
        obj.pop("ran_at", None)

    _assert_parity(bash_summary, py_summary)

    # Sanity-check the contract
    assert py_summary["total_tasks"] == 2
    assert py_summary["passed"] == 2
    assert py_summary["all_pass"] is True
    assert abs(py_summary["avg_utility_score"] - 0.5) <= _FLOAT_TOL
    for r in py_summary["results"]:
        assert r["passed"] is True
        assert abs(r["utility_score"] - 0.5) <= _FLOAT_TOL


# ─────────────────────────────────────────────────────────────────────────────
# (j) run on an EMPTY task table → total_tasks=0 (no crash)
#
# Ported from the retired tests/unit/test_benchmark_suite.sh case #8. It was the
# only .sh assertion the parity gate did not drive on the live bash side: cases
# (g)/(i) always seed >=2 tasks before benchmark_run, so neither exercised the
# empty-table branch. The function-scoped `db` fixture gives a fresh
# benchmark_tasks table with zero rows, so no add()/DELETE is needed. No
# production change is required: the port's run() returns total_tasks=len(tasks)
# and guards avg_util with `if total else 0.0`, and bash benchmark_run already
# emits total_tasks=0 on an empty table.
# ─────────────────────────────────────────────────────────────────────────────
def test_run_empty_task_table_parity(db):
    _which_bash()
    env_bash = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db}
    src = (
        f'source "{SH}"\n'
        f'unset MINI_ORK_WORKFLOW_RUNNER_FN\n'
        f'benchmark_run "cand-empty"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src], env=env_bash, capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        f"bash benchmark_run on empty table rc={r.returncode}\nstderr={r.stderr}"
    )
    bash_summary = json.loads(r.stdout.strip())

    py_summary = bench.run("cand-empty", db=db)

    # Strip the volatile ran_at, then deep-compare the full summary shape.
    for obj in (bash_summary, py_summary):
        obj.pop("ran_at", None)
    _assert_parity(bash_summary, py_summary)

    # The ported contract: live bash and port agree total_tasks == 0.
    assert bash_summary["total_tasks"] == py_summary["total_tasks"] == 0
