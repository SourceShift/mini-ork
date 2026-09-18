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
every one of its 7 assertions is covered here. Case (7) was ported from
the .sh error-path assertion (``promotion_evaluate`` with no args exits
non-zero).

Seven cases:

  (1) ``promotion_evaluate`` with no benchmark rows → decision=='rejected'
      (no measurement ⇒ no promote) AND the decision-field key set.
  (2) ``promotion_evaluate`` persisted row → migration-0011 schema column
      assertions (promotion_id, candidate_id, from_version_id,
      to_version_id, utility_*, decision, decided_by).
  (3) ``promotion_approve`` round-trip against a directly-seeded legacy
      ``pending_human_approval`` row; verify decision=='promoted', approver
      matches, post-SELECT decided_by=='human'. Negative case: approve
      on missing pending row → SystemExit. The gate no longer produces
      that decision (the human approval gate was removed), so the row is
      seeded by hand — this covers resolving rows that predate the removal.
  (4) ``mo_promote_synthesis_gate`` deterministic-class bypass:
      task_class='code_fix' with any panel_score → rc=0, reason='deterministic_class'.
  (5) ``mo_promote_synthesis_gate`` all-conditions-met path:
      panel_score=87.5 + structural signals → rc=0, reason='all_conditions_met'.
  (6) ``mo_promote_synthesis_gate`` rejection paths (3 sub-asserts in one
      test): (a) low_panel_score → rc=1 reason='low_panel_score';
      (b) high panel but no structural signal → rc=1 reason='no_structural_signal';
      (c) bad JSON file → rc=2.
  (7) ``promotion_evaluate`` with no args → the port raises TypeError
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
            "cand-no-bench", "cand-approve",
            "cand-persist", "cand-approve-flow", "cand-persisted-decision",
            "cand-one-run", "cand-floor",
        ):
            con.execute("""
                INSERT OR IGNORE INTO workflow_candidates
                    (candidate_id, base_workflow_version_id, created_by)
                VALUES (?, 'test-wf-v1', 'human')
            """, (cid,))
        con.commit()
    finally:
        con.close()


def _seed_legacy_pending(db_path: Path, candidate_id: str) -> None:
    """Write a ``pending_human_approval`` row directly.

    The human approval gate was removed, so ``promotion_evaluate`` never emits
    this decision. Rows carrying it still exist in live DBs from before the
    removal, and ``promotion_approve`` is kept to resolve them — seeding the
    row by hand is the only way to exercise that path.
    """
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("""
            INSERT INTO promotion_records
                (promotion_id, candidate_id, from_version_id, to_version_id,
                 utility_before, utility_after, rationale, decision, decided_by)
            VALUES (?, ?, 'test-wf-v1', 'test-wf-v1', 0.0, 0.0,
                    'legacy pending row', 'pending_human_approval', 'gate')
        """, (f"pr-legacy-{candidate_id}", candidate_id))
        con.commit()
    finally:
        con.close()


def _seed_bench(db_path: Path, candidate_id: str, rows) -> None:
    """Seed benchmark_results rows. ``rows``: list of (benchmark_id, run_id, pass).

    ``run_id`` is NOT NULL and references ``runs(id)``, so the referenced runs
    rows are created first.
    """
    con = sqlite3.connect(str(db_path))
    try:
        for bid in {r[0] for r in rows}:
            con.execute("""
                INSERT OR IGNORE INTO benchmark_tasks
                    (benchmark_id, task_class)
                VALUES (?, 'code_fix')
            """, (bid,))
        for rid in {r[1] for r in rows}:
            con.execute("""
                INSERT OR IGNORE INTO runs (id, started_at)
                VALUES (?, strftime('%s','now'))
            """, (rid,))
        for i, (bid, rid, passed) in enumerate(rows, start=1):
            con.execute("""
                INSERT OR IGNORE INTO benchmark_results
                    (result_id, benchmark_id, candidate_id, run_id,
                     pass, utility_score)
                VALUES (?, ?, ?, ?, ?, 0.92)
            """, (f"res-{candidate_id}-{i}", bid, candidate_id, rid, passed))
        con.commit()
    finally:
        con.close()


def _seed_bench_all_pass(db_path: Path, candidate_id: str) -> None:
    """Seed 4 passing benchmark_results rows across 3 INDEPENDENT runs.

    Four samples from one run are one observation, so a helper that seeded them
    all with ``run_id=1`` could not clear the independence floor however well it
    passed. The four rows are spread over runs 1-3 — still 4 samples, now with
    three runs of evidence behind them.
    """
    _seed_bench(db_path, candidate_id, [
        ("bench-task-1", 1, 1),
        ("bench-task-2", 2, 1),
        ("bench-task-3", 3, 1),
        ("bench-task-4", 1, 1),
    ])


# ── python-side helpers (in-process port) ──────────────────────────────────


def _py_evaluate(db_path: Path, candidate_id: str) -> dict:
    """Run in-process promotion_evaluate. Returns the JSON dict.

    Mirrors bash's exit-1 contract via SystemExit when the candidate has
    no ``base_workflow_version_id`` row.
    """
    try:
        return pg.promotion_evaluate(str(db_path), candidate_id)
    except SystemExit:
        # Re-raise so callers can distinguish rc=1 from rc=0.
        raise


def _py_approve(db_path: Path, candidate_id: str,
                approver: str, rationale: str) -> dict:
    return pg.promotion_approve(str(db_path), candidate_id, approver, rationale)


def _py_synthesis(verdict_file: str, task_class: str) -> tuple[dict, int]:
    return pg.mo_promote_synthesis_gate(verdict_file, task_class, mini_ork_root=str(REPO))


def _assert_float_eq(label: str, a, b, tol: float = _FLOAT_TOL) -> None:
    assert abs(float(a) - float(b)) <= tol, f"{label}: {a} vs {b} tol={tol}"


# ───────────────────────────────────────────────────────────────────────────
# (1) promotion_evaluate with no benchmark rows.
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_evaluate_no_benchmark(db):
    """Zero benchmark_results rows must never promote.

    The decision tree used to guard every branch with ``and brun``, so
    ``brun is None`` (nothing measured) slipped through to the final
    ``else: promoted`` — asserting in the rationale that "all benchmark
    tasks passed" when no task had run. No measurement ⇒ no promote.
    """
    _seed_workflow(db)
    pobj = _py_evaluate(db, "cand-no-bench")
    assert pobj["decision"] == "rejected"
    assert "no benchmark measurement" in pobj["rationale"]
    # The fabricated claim that the old fall-through emitted.
    assert "all benchmark tasks passed" not in pobj["rationale"]
    # Decision-field key set.
    for k in (
        "decision", "rationale", "utility_before", "utility_after",
        "utility_delta", "benchmark_run_id", "all_pass", "safety_violations",
    ):
        assert k in pobj, f"missing key: {k}"
    # No measurement ⇒ no evidence runs, and no run to name. The column used to
    # echo the candidate_id here, which asserted a benchmark backing that did
    # not exist.
    assert pobj["benchmark_run_id"] is None
    assert pobj["n_runs"] == 0
    _assert_float_eq("utility_delta", pobj["utility_delta"], 0.0)
    _assert_float_eq(
        "utility_consistency",
        pobj["utility_after"] - pobj["utility_before"],
        pobj["utility_delta"],
    )
    # The persisted row records the refusal, not a promote.
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT decision FROM promotion_records WHERE candidate_id=?",
            ("cand-no-bench",),
        ).fetchone()
    finally:
        con.close()
    assert row is not None and row[0] == "rejected"


# ───────────────────────────────────────────────────────────────────────────
# (2) Persisted-row schema — proves the port writes the migration-0011
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
    # benchmark_run_id names the runs the decision rested on — not the
    # candidate_id the old shape echoed, which said nothing about evidence.
    assert bri_val == "1,2,3"
    assert pobj["n_runs"] == 3
    # The persisted rationale matches the returned payload.
    assert rat == pobj["rationale"]


# ───────────────────────────────────────────────────────────────────────────
# (2b) Evidence independence — N samples from one run are ONE observation.
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_rejects_evidence_from_a_single_run(db):
    """30 passing rows from one run must not promote.

    The gate aggregated every ``benchmark_results`` row for a candidate and
    never read ``run_id``, so one run emitting many tasks was indistinguishable
    from many runs agreeing. Independence is measured, never inferred from the
    sample count."""
    _seed_workflow(db)
    _seed_bench(db, "cand-one-run",
                [("bench-task-1", 1, 1), ("bench-task-1", 1, 1),
                 ("bench-task-1", 1, 1), ("bench-task-1", 1, 1)])

    pobj = _py_evaluate(db, "cand-one-run")

    assert pobj["decision"] == "rejected"
    assert "insufficient independent evidence" in pobj["rationale"]
    assert "1 distinct run(s), 3 required" in pobj["rationale"]
    assert pobj["n_runs"] == 1
    # The refusal is persisted, not cosmetic: the audit trail records a
    # rejection and never claims the candidate was promoted.
    con = sqlite3.connect(str(db))
    try:
        decision, bri = con.execute(
            "SELECT decision, benchmark_run_id FROM promotion_records "
            "WHERE candidate_id='cand-one-run'"
        ).fetchone()
    finally:
        con.close()
    assert decision == "rejected"
    assert bri == "1"


def test_promotion_floor_cannot_be_lowered(db, monkeypatch):
    """MO_PROMOTION_MIN_RUNS may raise the floor but never lower it.

    A safety floor is not a setting: if an env var could restore the forgeable
    bar, one run would promote again by configuration."""
    _seed_workflow(db)
    _seed_bench(db, "cand-floor", [("bench-task-1", 1, 1), ("bench-task-2", 1, 1)])

    monkeypatch.setenv("MO_PROMOTION_MIN_RUNS", "1")
    pobj = _py_evaluate(db, "cand-floor")

    assert pobj["decision"] == "rejected"
    assert "3 required" in pobj["rationale"]


def test_promotion_floor_can_be_raised(db, monkeypatch):
    """The env var raises the bar: 3 independent runs do not clear 5."""
    _seed_workflow(db)
    _seed_bench(db, "cand-persist", [("bench-task-1", 1, 1), ("bench-task-2", 2, 1),
                                     ("bench-task-3", 3, 1)])

    monkeypatch.setenv("MO_PROMOTION_MIN_RUNS", "5")
    pobj = _py_evaluate(db, "cand-persist")

    assert pobj["decision"] == "rejected"
    assert "5 required" in pobj["rationale"]


def test_evidence_run_ids_drops_null_and_blank(tmp_path):
    """A NULL/blank run_id is unproven independence and is dropped, not assumed.

    The migrated schema declares ``run_id INTEGER NOT NULL``, so this cannot
    arise there — but the guard must hold for loose or legacy tables too, and
    a helper that quietly counted a blank id as a run would restore the hole."""
    db_path = tmp_path / "loose.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE benchmark_results (candidate_id TEXT, run_id)")
    con.executemany("INSERT INTO benchmark_results VALUES (?,?)", [
        ("c", None), ("c", ""), ("c", "  "), ("c", "7"), ("c", "7"), ("c", "8"),
    ])
    con.commit()

    assert pg._evidence_run_ids(con, "c") == ["7", "8"]
    con.close()


# ───────────────────────────────────────────────────────────────────────────
# (3) promotion_approve round-trip + negative path.
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_approve_round_trip(db):
    _seed_workflow(db)
    # A legacy pending row — the removed gate wrote these; nothing writes one now.
    _seed_legacy_pending(db, "cand-approve-flow")

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
# (4) mo_promote_synthesis_gate deterministic-class bypass.
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
# (5) mo_promote_synthesis_gate all-conditions-met path.
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
# (6) mo_promote_synthesis_gate rejection paths — three sub-asserts.
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
# (7) missing-arg error path — the port's required-positionals raise
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
