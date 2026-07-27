"""Standalone unit tests for ``mini_ork.gates.promotion_gate``.

Replaces the bash-parity gate (against ``lib/promotion_gate.sh``) as part
of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer drives the LIVE bash function
via ``bash -c 'source lib/promotion_gate.sh; ...'`` — it asserts the
port's behaviour directly. The expected values below are the semantic
contract the bash side used to pin (decisions, persisted-row schema,
approve round-trip, synthesis-gate reasons, rc semantics), now asserted
on the port's output.

This file subsumes the retired tests/unit/test_promotion_gate.sh fixture:
every one of its 7 assertions is covered here. Case (8) was ported from
the .sh error-path assertion (``promotion_evaluate`` with no args exits
non-zero).

Eight cases:

  (1) ``promotion_evaluate`` with no benchmark rows → decision in
      {quarantined, rejected, promoted} AND the decision-field key set.
  (2) ``MINI_ORK_REQUIRE_HUMAN_APPROVAL=true`` → decision=='pending_human_approval'.
  (3) ``promotion_evaluate`` persisted row → migration-0011 schema column
      assertions (promotion_id, candidate_id, from_version_id,
      to_version_id, utility_*, decision, decided_by).
  (4) ``promotion_approve`` round-trip: pre-create pending row via
      evaluate, then approve; verify decision=='promoted', approver
      matches, post-SELECT decided_by=='human'. Negative case: approve
      on missing pending row → SystemExit.
  (5) ``mo_promote_synthesis_gate`` deterministic-class bypass:
      task_class='code_fix' with any panel_score → rc=0, reason='deterministic_class'.
  (6) ``mo_promote_synthesis_gate`` all-conditions-met path:
      panel_score=87.5 + structural signals → rc=0, reason='all_conditions_met'.
  (7) ``mo_promote_synthesis_gate`` rejection paths (3 sub-asserts in one
      test): (a) low_panel_score → rc=1 reason='low_panel_score';
      (b) high panel but no structural signal → rc=1 reason='no_structural_signal';
      (c) bad JSON file → rc=2.
  (8) ``promotion_evaluate`` with no args → the port raises TypeError
      (the Python analog of bash's ${1:?candidate_id required} guard).

Floats: utility_before / utility_after / utility_delta are compared at
1e-6 tolerance where cross-checked.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO))
from mini_ork.gates import promotion_gate as pg  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

_FLOAT_TOL = 1e-6


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    """Initialise a fresh SQLite file via init_db (the Python port of
    db/init.sh). Sets MINI_ORK_HOME + MINI_ORK_DB so the in-process port
    reads the same DB path."""
    home = tmp_path
    db_path = home / "state.db"
    rc, out, err = mig.init_db(db=str(db_path), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_DB"] = str(db_path)
    os.environ["MINI_ORK_ROOT"] = str(REPO)
    return db_path


def _seed_workflow(db_path: Path) -> None:
    """Seed workflow_memory + workflow_candidates rows for every
    candidate this test exercises. Mirrors the retired
    tests/unit/test_promotion_gate.sh lines 60-77 fixtures."""
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("""
            INSERT OR IGNORE INTO workflow_memory
                (workflow_version_id, workflow_name, yaml_hash, yaml_blob)
            VALUES ('test-wf-v1', 'test-wf', 'deadbeef', '# test')
        """)
        for cid in (
            "cand-no-bench", "cand-human", "cand-approve",
            "cand-persist", "cand-approve-flow", "cand-persisted-decision",
        ):
            con.execute("""
                INSERT OR IGNORE INTO workflow_candidates
                    (candidate_id, base_workflow_version_id, created_by)
                VALUES (?, 'test-wf-v1', 'human')
            """, (cid,))
        con.commit()
    finally:
        con.close()


def _seed_bench_all_pass(db_path: Path, candidate_id: str) -> None:
    """Seed 4 benchmark_results rows, all pass=1, utility_score=0.92."""
    con = sqlite3.connect(str(db_path))
    try:
        # Need a benchmark_tasks row + a runs row for the FK.
        con.execute("""
            INSERT OR IGNORE INTO benchmark_tasks
                (benchmark_id, task_class)
            VALUES ('bench-task-1', 'code_fix')
        """)
        con.execute("""
            INSERT OR IGNORE INTO runs (id, started_at)
            VALUES (1, strftime('%s','now'))
        """)
        for bid in ("bench-task-1", "bench-task-2", "bench-task-3", "bench-task-4"):
            con.execute("""
                INSERT OR IGNORE INTO benchmark_tasks
                    (benchmark_id, task_class)
                VALUES (?, 'code_fix')
            """, (bid,))
        for i, bid in enumerate(("bench-task-1", "bench-task-2", "bench-task-3", "bench-task-4"), start=1):
            con.execute("""
                INSERT OR IGNORE INTO benchmark_results
                    (result_id, benchmark_id, candidate_id, run_id,
                     pass, utility_score)
                VALUES (?, ?, ?, 1, 1, 0.92)
            """, (f"res-{candidate_id}-{i}", bid, candidate_id))
        con.commit()
    finally:
        con.close()


# ── python-side helpers (in-process port) ──────────────────────────────────


def _py_evaluate(db_path: Path, candidate_id: str,
                 *, require_human: bool = False) -> dict:
    """Run in-process promotion_evaluate. Returns the JSON dict.

    Mirrors bash's exit-1 contract via SystemExit when the candidate has
    no ``base_workflow_version_id`` row.
    """
    os.environ.pop("MINI_ORK_REQUIRE_HUMAN_APPROVAL", None)
    if require_human:
        os.environ["MINI_ORK_REQUIRE_HUMAN_APPROVAL"] = "true"
    try:
        return pg.promotion_evaluate(str(db_path), candidate_id)
    except SystemExit:
        # Re-raise so callers can distinguish rc=1 from rc=0.
        raise


def _py_approve(db_path: Path, candidate_id: str,
                approver: str, rationale: str) -> dict:
    os.environ.pop("MINI_ORK_REQUIRE_HUMAN_APPROVAL", None)
    return pg.promotion_approve(str(db_path), candidate_id, approver, rationale)


def _py_synthesis(verdict_file: str, task_class: str) -> tuple[dict, int]:
    return pg.mo_promote_synthesis_gate(verdict_file, task_class, mini_ork_root=str(REPO))


def _assert_float_eq(label: str, a, b, tol: float = _FLOAT_TOL) -> None:
    assert abs(float(a) - float(b)) <= tol, f"{label}: {a} vs {b} tol={tol}"


# ───────────────────────────────────────────────────────────────────────────
# (1) promotion_evaluate with no benchmark rows.
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_evaluate_no_benchmark(db):
    _seed_workflow(db)
    # No benchmark_results → brun is None → the port falls through to the
    # no-bench branch. The retired .sh accepted the
    # {quarantined, rejected, promoted} set.
    pobj = _py_evaluate(db, "cand-no-bench")
    assert pobj["decision"] in {"quarantined", "rejected", "promoted"}
    # Decision-field key set.
    for k in (
        "decision", "rationale", "utility_before", "utility_after",
        "utility_delta", "benchmark_run_id", "all_pass", "safety_violations",
    ):
        assert k in pobj, f"missing key: {k}"
    # benchmark_run_id is unset or echoes the candidate_id (the port's
    # documented no-bench shape).
    assert pobj["benchmark_run_id"] in (None, "cand-no-bench")
    _assert_float_eq("utility_delta", pobj["utility_delta"], 0.0)
    _assert_float_eq(
        "utility_consistency",
        pobj["utility_after"] - pobj["utility_before"],
        pobj["utility_delta"],
    )


# ───────────────────────────────────────────────────────────────────────────
# (2) MINI_ORK_REQUIRE_HUMAN_APPROVAL=true → pending_human_approval.
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_evaluate_require_human(db):
    _seed_workflow(db)
    pobj = _py_evaluate(db, "cand-human", require_human=True)
    assert pobj["decision"] == "pending_human_approval"
    assert "Human gate required" in pobj["rationale"]


# ───────────────────────────────────────────────────────────────────────────
# (3) Persisted-row schema — proves the port writes the migration-0011
#     schema (NOT the legacy CREATE-IF-NOT-EXISTS draft).
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_evaluate_persisted_row(db):
    _seed_workflow(db)
    _seed_bench_all_pass(db, "cand-persist")
    pobj = _py_evaluate(db, "cand-persist")
    # All benchmarks pass with utility 0.92 → promoted.
    assert pobj["decision"] == "promoted"

    # Exactly 1 promotion_records row for cand-persist.
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("""
            SELECT promotion_id, candidate_id, from_version_id, to_version_id,
                   utility_before, utility_after, benchmark_run_id,
                   rationale, decision, decided_by
            FROM promotion_records
            WHERE candidate_id=?
        """, ("cand-persist",)).fetchall()
    finally:
        con.close()
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    (prom_id, cid, fv, tv, ub, ua, bri_val, rat, dec, db_) = rows[0]
    assert cid == "cand-persist"
    assert fv == "test-wf-v1"
    assert tv == "test-wf-v1"
    assert dec == "promoted"
    assert db_ == "gate"
    assert prom_id.startswith("pr-")
    # 1e-6 float tolerance on utility_* columns vs the returned payload.
    _assert_float_eq("utility_before", ub, pobj["utility_before"])
    _assert_float_eq("utility_after", ua, pobj["utility_after"])
    # benchmark_run_id is the candidate_id (matches the output key).
    assert bri_val is None or bri_val == "cand-persist"
    # The persisted rationale matches the returned payload.
    assert rat == pobj["rationale"]


# ───────────────────────────────────────────────────────────────────────────
# (4) promotion_approve round-trip + negative path.
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_approve_round_trip(db):
    _seed_workflow(db)
    # Pre-create pending_human_approval row via evaluate.
    _seed_bench_all_pass(db, "cand-approve-flow")
    _py_evaluate(db, "cand-approve-flow", require_human=True)

    pobj = _py_approve(db, "cand-approve-flow",
                       "test-approver", "Approved in parity test")
    assert pobj["decision"] == "promoted"
    assert pobj["approver"] == "test-approver"
    assert pobj["candidate_id"] == "cand-approve-flow"
    assert pobj["approved_at"] is not None

    # Post-approval DB check: decided_by flipped to 'human'.
    con = sqlite3.connect(str(db))
    try:
        decided_by = con.execute(
            "SELECT decided_by FROM promotion_records "
            "WHERE candidate_id=? AND decision='promoted' "
            "ORDER BY decided_at DESC LIMIT 1",
            ("cand-approve-flow",),
        ).fetchone()[0]
    finally:
        con.close()
    assert decided_by == "human"


def test_promotion_approve_no_pending(db):
    _seed_workflow(db)
    # Candidate has no pending row → the port raises SystemExit (the
    # Python analog of bash's rc=1).
    with pytest.raises(SystemExit):
        _py_approve(db, "cand-no-bench", "approver", "rationale")


# ───────────────────────────────────────────────────────────────────────────
# (5) mo_promote_synthesis_gate deterministic-class bypass.
# ───────────────────────────────────────────────────────────────────────────


def test_mo_promote_synthesis_gate_bypass(tmp_path, db):
    _seed_workflow(db)
    verdict = tmp_path / "det.json"
    verdict.write_text('{"panel_score":0,"voters":[],"structural":{}}')
    pobj, prc = _py_synthesis(str(verdict), "code_fix")
    assert prc == 0
    assert pobj["decision"] == "approved"
    assert pobj["reason"] == "deterministic_class"


# ───────────────────────────────────────────────────────────────────────────
# (6) mo_promote_synthesis_gate all-conditions-met path.
# ───────────────────────────────────────────────────────────────────────────


def test_mo_promote_synthesis_gate_all_conditions_met(tmp_path, db):
    _seed_workflow(db)
    verdict = tmp_path / "healthy.json"
    verdict.write_text(json.dumps({
        "panel_score": 87.5,
        "voters": [
            {"voter_id": "glm", "vote": "approve", "confidence": 0.85,
             "ground_truth_match": True},
            {"voter_id": "kimi", "vote": "approve", "confidence": 0.80,
             "ground_truth_match": True},
            {"voter_id": "codex", "vote": "approve", "confidence": 0.75,
             "ground_truth_match": True},
        ],
        "structural": {
            "citation_density_per_lens": 5.2,
            "file_coverage_delta": 3,
            "finding_cardinality": 11,
        },
    }))
    pobj, prc = _py_synthesis(str(verdict), "research_synthesis")
    assert prc == 0
    assert pobj["decision"] == "approved"
    assert pobj["reason"] == "all_conditions_met"
    assert len(pobj["signals"]["structural_signals_met"]) >= 1


# ───────────────────────────────────────────────────────────────────────────
# (7) mo_promote_synthesis_gate rejection paths — three sub-asserts.
# ───────────────────────────────────────────────────────────────────────────


def test_mo_promote_synthesis_gate_rejections(tmp_path, db):
    _seed_workflow(db)

    # (a) low panel_score → rc=1, reason='low_panel_score'.
    low = tmp_path / "low_score.json"
    low.write_text(json.dumps({
        "panel_score": 62.0,
        "voters": [],
        "structural": {
            "citation_density_per_lens": 8.0,
            "file_coverage_delta": 5,
            "finding_cardinality": 20,
        },
    }))
    pobj, prc = _py_synthesis(str(low), "refactor_audit")
    assert prc == 1
    assert pobj["decision"] == "rejected"
    assert pobj["reason"] == "low_panel_score"

    # (b) high panel_score but zero structural signals → rc=1,
    # reason='no_structural_signal'.
    no_sig = tmp_path / "no_signal.json"
    no_sig.write_text(json.dumps({
        "panel_score": 95.0,
        "voters": [],
        "structural": {
            "citation_density_per_lens": 1.0,
            "file_coverage_delta": 0,
            "finding_cardinality": 2,
        },
    }))
    pobj, prc = _py_synthesis(str(no_sig), "blog_post")
    assert prc == 1
    assert pobj["decision"] == "rejected"
    assert pobj["reason"] == "no_structural_signal"

    # (c) bad JSON file → rc=2.
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    pobj, prc = _py_synthesis(str(bad), "ui_audit")
    assert prc == 2
    assert "error" in pobj


# ───────────────────────────────────────────────────────────────────────────
# (8) missing-arg error path — the port's required-positionals raise
#     TypeError (the Python analog of bash's ${1:?candidate_id required}
#     guard). Ports test_promotion_gate.sh's error-path assertion (its
#     line 137): `promotion_evaluate` with no args exits non-zero.
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_evaluate_missing_arg_error():
    """`promotion_evaluate` rejects a no-args call: both positionals
    (db_path, candidate_id) are required, so binding fails before the
    body."""
    with pytest.raises(TypeError):
        pg.promotion_evaluate()  # type: ignore[call-arg]
