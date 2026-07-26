"""Parity gate: mini_ork.trace_store vs lib/trace_store.sh, on the real schema.

For each payload we write via the LIVE bash `trace_write` (trace_id bash-N) and
via the Python `trace_write` (trace_id py-N) into the same DB, then read back and
assert reward_g (+ core columns) match. No mocking, no hardcoded reward_g.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import trace_store  # noqa: E402

TS_SH = REPO / "lib" / "trace_store.sh"

PAYLOADS = [
    {"task_class": "code-fix", "status": "success", "reward_value": 1.0, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
    {"task_class": "code-fix", "status": "failure", "reward_value": 0.0, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
    {"task_class": "code-fix", "status": "success", "reward_value": 0.5, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
    {"task_class": "book-gen", "status": "success", "reward_value": 2.0, "reward_anchor": 1.0, "reward_direction": "higher_is_better"},
    {"task_class": "book-gen", "status": "success", "reward_value": 1.0, "reward_anchor": 2.0, "reward_direction": "lower_is_better"},
    {"task_class": "code-fix", "status": "success", "reward_value": 1.0, "reward_anchor": 0.0, "reward_direction": "higher_is_better"},
    {"task_class": "code-fix", "status": "success"},
    {"task_class": "eval", "status": "success", "reward_value": 3.0, "reward_anchor": 1.5, "reward_direction": "higher_is_better"},
    {"task_class": "eval", "status": "success", "reward_value": 0.2, "reward_anchor": 0.8, "reward_direction": "higher_is_better"},
]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _bash_trace_write(db, payload_json):
    subprocess.run(
        ["bash", "-c", f'. "{TS_SH}" && trace_write "$1"', "_", payload_json],
        env={**os.environ, "MINI_ORK_DB": db}, capture_output=True, text=True, check=True,
    )


def _reward_g(db, trace_id):
    con = sqlite3.connect(db)
    r = con.execute("SELECT reward_g FROM execution_traces WHERE trace_id=?",
                    (trace_id,)).fetchone()
    con.close()
    return r[0] if r else "MISSING"


def test_compute_reward_g_unit():
    assert trace_store.compute_reward_g(1.0, 0.5, "higher_is_better") == 1.0
    assert trace_store.compute_reward_g(0.0, 0.5, "higher_is_better") == -1.0
    assert trace_store.compute_reward_g(1.0, 2.0, "lower_is_better") == 0.5
    assert trace_store.compute_reward_g(1.0, 0.0, "higher_is_better") is None
    assert trace_store.compute_reward_g(None, 0.5, "higher_is_better") is None


def test_reward_g_parity_bash_vs_python(db):
    for i, base in enumerate(PAYLOADS):
        bash_p = {**base, "trace_id": f"bash-{i}"}
        py_p = {**base, "trace_id": f"py-{i}"}
        _bash_trace_write(db, json.dumps(bash_p))
        trace_store.trace_write(py_p, db=db)
        bg = _reward_g(db, f"bash-{i}")
        pg = _reward_g(db, f"py-{i}")
        if bg is None or pg is None:
            assert bg == pg, f"payload {i}: bash={bg} py={pg}"
        else:
            assert abs(float(bg) - float(pg)) < 1e-9, f"payload {i}: bash={bg} py={pg}"


# ── WS3: full-row A/B parity for the exact call-site payload shapes ──────────
# invoke_prompt.py (running / success / failure upserts) and reflect.py
# (running + success w/ duration_ms + verifier_output) previously wrote through
# `bash -c '. lib/trace_store.sh && trace_write …'`. Both now call the native
# trace_store.trace_write. This gate proves the native-written row is identical
# in shape to the bash-written row for the same inputs — two separate DBs,
# full-column diff, modulo created_at (timestamp default) and the db path.

CALL_SITE_PAYLOADS = [
    # invoke_prompt: 'running' with the payload's prompt_version_hash key (which
    # trace_write ignores — the column fills from MO_NODE_PROMPT_SHA).
    {"trace_id": "ab-invoke", "task_class": "code_fix", "status": "running",
     "prompt_version_hash": "deadbeefcafe1234"},
    # invoke_prompt: success upsert onto the same trace_id.
    {"trace_id": "ab-invoke", "status": "success"},
    # invoke_prompt: failure upsert on a fresh trace_id.
    {"trace_id": "ab-invoke-fail", "status": "failure"},
    # reflect: 'running' then success upsert with duration + verifier_output.
    {"trace_id": "ab-reflect", "task_class": "__reflect__", "status": "running"},
    {"trace_id": "ab-reflect", "task_class": "__reflect__", "status": "success",
     "duration_ms": 42000,
     "verifier_output": {"traces_analyzed": 3, "gradients_written": 2,
                         "since": 1781000000}},
]

LINEAGE_ENV = {
    "MINI_ORK_RUN_ID": "run-42",
    "MINI_ORK_TASK_RUN_ID": "task-run-7",
    "MINI_ORK_WORKFLOW_VERSION_ID": "wfv-9",
    "MO_NODE_PROMPT_SHA": "abcdef0123456789",
}


def _all_rows_by_trace_id(dbp):
    con = sqlite3.connect(dbp)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM execution_traces ORDER BY trace_id").fetchall()
    con.close()
    return {r["trace_id"]: dict(r) for r in rows}


def test_full_row_ab_parity_bash_vs_python(tmp_path, monkeypatch):
    db_a = _init_db(tmp_path / "ab-home-a")  # bash-written
    db_b = _init_db(tmp_path / "ab-home-b")  # native-written
    for k, v in LINEAGE_ENV.items():
        monkeypatch.setenv(k, v)
    for p in CALL_SITE_PAYLOADS:
        payload = json.dumps(p)
        subprocess.run(
            ["bash", "-c", f'. "{TS_SH}" && trace_write "$1"', "_", payload],
            env={**os.environ, **LINEAGE_ENV, "MINI_ORK_DB": db_a},
            capture_output=True, text=True, check=True,
        )
        trace_store.trace_write(payload, db=db_b)
    rows_a = _all_rows_by_trace_id(db_a)
    rows_b = _all_rows_by_trace_id(db_b)
    assert rows_a.keys() == rows_b.keys()
    for tid in rows_a:
        a = {k: v for k, v in rows_a[tid].items() if k != "created_at"}
        b = {k: v for k, v in rows_b[tid].items() if k != "created_at"}
        assert a == b, f"row drift for {tid}: " + ", ".join(
            f"{k}: bash={a[k]!r} py={b[k]!r}" for k in a if a[k] != b[k])
    # sanity: the lineage env fallbacks actually landed (not vacuously empty)
    assert rows_b["ab-invoke"]["run_id"] == "task-run-7"
    assert rows_b["ab-invoke"]["prompt_version_hash"] == "abcdef0123456789"
    assert json.loads(rows_b["ab-reflect"]["verifier_output"])["traces_analyzed"] == 3


def test_roundtrip_get(db):
    tid = trace_store.trace_write(
        {"trace_id": "rt-1", "task_class": "code-fix", "status": "success",
         "reward_value": 1.0, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
        db=db)
    row = trace_store.trace_get(tid, db=db)
    assert row and row["status"] == "success" and abs(float(row["reward_g"]) - 1.0) < 1e-9


def test_objective_domain_passthrough(db):
    # objective_domain must land from the payload, not silently default to
    # code-delivery — the scoping-stamp fix (feature-partition column population).
    tid = trace_store.trace_write(
        {"trace_id": "od-1", "task_class": "book-gen", "status": "success",
         "objective_domain": "book-gen"}, db=db)
    row = trace_store.trace_get(tid, db=db)
    assert row["objective_domain"] == "book-gen"
    # unset → legacy code-delivery fallback preserved
    tid2 = trace_store.trace_write(
        {"trace_id": "od-2", "task_class": "x", "status": "success"}, db=db)
    assert trace_store.trace_get(tid2, db=db)["objective_domain"] == "code-delivery"


def _bash_grade(db, run_dir, run_id):
    subprocess.run(
        ["bash", "-c", f'. "{TS_SH}" && mo_grade_run_reward "$1" "$2"', "_", run_dir, run_id],
        env={**os.environ, "MINI_ORK_DB": db}, capture_output=True, text=True, check=True,
    )


def test_grade_run_reward_bash_vs_python(db, tmp_path):
    # Win #3 graded bridge: rubric.json {score 0-8} → reward_g in [-1,+1] stamped on
    # every trace of the run, overwriting the binary status-map reward. Assert the
    # bash mo_grade_run_reward and python grade_run_reward agree on the graded value.
    rd = tmp_path / "grade-run"; rd.mkdir()
    (rd / "rubric.json").write_text(json.dumps({"score": 6}))
    # seed one trace per side under distinct run_ids, initial reward_g = -1 (status-map fail)
    for tid, rid in (("gb-1", "grade-bash"), ("gp-1", "grade-py")):
        trace_store.trace_write(
            {"trace_id": tid, "run_id": rid, "task_class": "code-fix", "status": "failure",
             "reward_value": 0.0, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
            db=db)
    _bash_grade(db, str(rd), "grade-bash")
    n = trace_store.grade_run_reward(str(rd), "grade-py", db=db)
    assert n == 1
    bg, pg = _reward_g(db, "gb-1"), _reward_g(db, "gp-1")
    graded = (6 / 8 - 0.5) / 0.5   # 0.5
    assert abs(float(bg) - graded) < 1e-9 and abs(float(pg) - graded) < 1e-9
    # missing rubric → no-op (0 rows), leaves status-map reward intact
    empty = tmp_path / "no-rubric"; empty.mkdir()
    assert trace_store.grade_run_reward(str(empty), "grade-py", db=db) == 0


# ── ported from test_trace_store.sh at retirement ──────────────────────────
# The unit fixture's CRUD/query/error surface was disjoint from this gate's
# reward_g/grading focus. These bash-vs-py parity cases port its un-subsumed
# assertions: trace_query --status/--task-class filtering, trace_get unknown-id
# → null, and the two write error-paths (invalid JSON, MINI_ORK_DB unset). The
# fixture's trace_attach_artifact assertions are deliberately NOT ported: that
# lib function has no Python port and stays covered live by
# tests/e2e/test_e2e_trace_lifecycle.sh.

def _init_db(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _bash_query(db, *args):
    r = subprocess.run(
        ["bash", "-c", f'. "{TS_SH}" && trace_query "$@"', "_", *args],
        env={**os.environ, "MINI_ORK_DB": db}, capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout or "[]")


def _bash_get(db, trace_id):
    r = subprocess.run(
        ["bash", "-c", f'. "{TS_SH}" && trace_get "$1"', "_", trace_id],
        env={**os.environ, "MINI_ORK_DB": db}, capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _bash_write_rc(env, payload):
    return subprocess.run(
        ["bash", "-c", f'. "{TS_SH}" && trace_write "$1"', "_", payload],
        env=env, capture_output=True, text=True,
    ).returncode


def test_query_filters_parity(tmp_path):
    db = _init_db(tmp_path / "qhome")
    trace_store.trace_write({"trace_id": "q-1", "task_class": "unit-test", "status": "success"}, db=db)
    trace_store.trace_write({"trace_id": "q-2", "task_class": "unit-test", "status": "failure"}, db=db)
    trace_store.trace_write({"trace_id": "q-3", "task_class": "other-class", "status": "success"}, db=db)
    # status filter: live bash trace_query --status X  ==  python trace_query(status=X)
    assert len(_bash_query(db, "--status", "success")) == len(trace_store.trace_query(status="success", db=db)) == 2
    assert len(_bash_query(db, "--status", "failure")) == len(trace_store.trace_query(status="failure", db=db)) == 1
    # task-class filter
    assert len(_bash_query(db, "--task-class", "unit-test")) == len(trace_store.trace_query(task_class="unit-test", db=db)) == 2
    assert len(_bash_query(db, "--task-class", "other-class")) == len(trace_store.trace_query(task_class="other-class", db=db)) == 1


def test_get_unknown_id_parity(tmp_path):
    db = _init_db(tmp_path / "ghome")
    assert _bash_get(db, "tr-doesnotexist") == "null"
    assert trace_store.trace_get("tr-doesnotexist", db=db) is None


def test_write_fail_closed_parity(tmp_path, monkeypatch):
    db = _init_db(tmp_path / "ehome")
    # invalid JSON → bash exits non-zero, python raises
    assert _bash_write_rc({**os.environ, "MINI_ORK_DB": db}, "not-valid-json") != 0
    with pytest.raises((ValueError, TypeError)):
        trace_store.trace_write("not-valid-json", db=db)
    # MINI_ORK_DB unset → bash exits non-zero, python raises RuntimeError
    env_no_db = {k: v for k, v in os.environ.items() if k != "MINI_ORK_DB"}
    assert _bash_write_rc(env_no_db, '{"task_class":"x"}') != 0
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    with pytest.raises(RuntimeError):
        trace_store.trace_write({"task_class": "x"})
