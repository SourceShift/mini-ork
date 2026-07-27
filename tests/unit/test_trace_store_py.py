"""Unit tests for mini_ork.trace_store on the real (migrated) schema.

For each payload we write via the Python `trace_write` into a migrated DB,
then read back and assert reward_g (+ core columns) match the documented
compute_reward_g formula. No mocking, no hardcoded reward_g beyond the
formula itself. DB bootstrap uses the native ``mini_ork.stores.migrate.init_db``
(no bash twin required).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import trace_store  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402

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


def _init_db(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    return dbp


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return _init_db(tmp_path_factory.mktemp("home"))


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


def test_reward_g_matches_formula(db):
    """Stored reward_g must equal compute_reward_g applied to the payload
    (direction-normalised gain; anchor==0 or missing reward → NULL)."""
    for i, base in enumerate(PAYLOADS):
        py_p = {**base, "trace_id": f"py-{i}"}
        trace_store.trace_write(py_p, db=db)
        pg = _reward_g(db, f"py-{i}")
        expected = trace_store.compute_reward_g(
            base.get("reward_value"), base.get("reward_anchor"),
            base.get("reward_direction", "higher_is_better"))
        if expected is None:
            assert pg is None, f"payload {i}: expected NULL reward_g, got {pg}"
        else:
            assert abs(float(pg) - expected) < 1e-9, (
                f"payload {i}: expected {expected}, got {pg}")


# ── WS3: full-row shape for the exact call-site payload shapes ──────────────
# invoke_prompt.py (running / success / failure upserts) and reflect.py
# (running + success w/ duration_ms + verifier_output) write through the
# native trace_store.trace_write. These tests pin the row shape the native
# writer produces for those inputs — including the env-fallback lineage
# columns and upsert semantics.

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


def test_full_row_shape_call_site_payloads(tmp_path, monkeypatch):
    db = _init_db(tmp_path / "ab-home")
    for k, v in LINEAGE_ENV.items():
        monkeypatch.setenv(k, v)
    for p in CALL_SITE_PAYLOADS:
        trace_store.trace_write(json.dumps(p), db=db)
    rows = _all_rows_by_trace_id(db)

    # Three trace ids: the two ab-invoke payloads upsert onto one row.
    assert set(rows) == {"ab-invoke", "ab-invoke-fail", "ab-reflect"}

    inv = rows["ab-invoke"]
    # Upsert semantics: running → success, original task_class preserved.
    assert inv["status"] == "success"
    assert inv["task_class"] == "code_fix"
    # Lineage env fallbacks landed (not vacuously empty).
    assert inv["run_id"] == "task-run-7"
    assert inv["workflow_version_id"] == "wfv-9"
    # prompt_version_hash fills from MO_NODE_PROMPT_SHA, NOT the payload key.
    assert inv["prompt_version_hash"] == "abcdef0123456789"

    assert rows["ab-invoke-fail"]["status"] == "failure"

    ref = rows["ab-reflect"]
    assert ref["status"] == "success"
    assert ref["task_class"] == "__reflect__"
    assert ref["duration_ms"] == 42000
    assert json.loads(ref["verifier_output"])["traces_analyzed"] == 3
    assert json.loads(ref["verifier_output"])["gradients_written"] == 2


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


def test_grade_run_reward(db, tmp_path):
    # Win #3 graded bridge: rubric.json {score 0-8} → reward_g in [-1,+1] stamped on
    # every trace of the run, overwriting the binary status-map reward.
    rd = tmp_path / "grade-run"; rd.mkdir()
    (rd / "rubric.json").write_text(json.dumps({"score": 6}))
    # seed one trace under the run, initial reward_g = -1 (status-map fail)
    trace_store.trace_write(
        {"trace_id": "gp-1", "run_id": "grade-py", "task_class": "code-fix", "status": "failure",
         "reward_value": 0.0, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
        db=db)
    n = trace_store.grade_run_reward(str(rd), "grade-py", db=db)
    assert n == 1
    graded = (6 / 8 - 0.5) / 0.5   # 0.5
    assert abs(float(_reward_g(db, "gp-1")) - graded) < 1e-9
    # missing rubric → no-op (0 rows), leaves status-map reward intact
    empty = tmp_path / "no-rubric"; empty.mkdir()
    assert trace_store.grade_run_reward(str(empty), "grade-py", db=db) == 0
    assert abs(float(_reward_g(db, "gp-1")) - graded) < 1e-9


# ── CRUD/query/error surface (ported from the retired bash fixture) ─────────
# trace_query --status/--task-class filtering, trace_get unknown-id → None,
# and the two write error-paths (invalid JSON, MINI_ORK_DB unset). The
# fixture's trace_attach_artifact assertions are deliberately NOT ported: that
# lib function has no Python port and stays covered live by
# tests/e2e/test_e2e_trace_lifecycle.sh.

def test_query_filters(tmp_path):
    db = _init_db(tmp_path / "qhome")
    trace_store.trace_write({"trace_id": "q-1", "task_class": "unit-test", "status": "success"}, db=db)
    trace_store.trace_write({"trace_id": "q-2", "task_class": "unit-test", "status": "failure"}, db=db)
    trace_store.trace_write({"trace_id": "q-3", "task_class": "other-class", "status": "success"}, db=db)
    assert len(trace_store.trace_query(status="success", db=db)) == 2
    assert len(trace_store.trace_query(status="failure", db=db)) == 1
    assert len(trace_store.trace_query(task_class="unit-test", db=db)) == 2
    assert len(trace_store.trace_query(task_class="other-class", db=db)) == 1


def test_get_unknown_id(tmp_path):
    db = _init_db(tmp_path / "ghome")
    assert trace_store.trace_get("tr-doesnotexist", db=db) is None


def test_write_fail_closed(tmp_path, monkeypatch):
    db = _init_db(tmp_path / "ehome")
    # invalid JSON → python raises
    with pytest.raises((ValueError, TypeError)):
        trace_store.trace_write("not-valid-json", db=db)
    # MINI_ORK_DB unset → python raises RuntimeError
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    with pytest.raises(RuntimeError):
        trace_store.trace_write({"task_class": "x"})
