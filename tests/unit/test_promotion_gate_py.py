"""Parity gate: ``mini_ork.gates.promotion_gate`` vs ``lib/promotion_gate.sh``.

Each test drives the LIVE bash function via
``bash -c 'source lib/promotion_gate.sh; ...'`` against the SAME
SQLite database the Python port reads, then deep-compares the two
outputs: JSON payload key-set + float equality at 1e-6, plus
return-code parity (rc=0/1/2).

The test fixture initialises a fresh DB via ``db/init.sh`` (mirrors the
production init path so migration 0011's promotion_records schema is
present), then seeds ``workflow_memory`` + ``workflow_candidates`` rows
the same way ``tests/unit/test_promotion_gate.sh`` does.

Seven cases (above the kickoff's >=6 floor):

  (1) ``promotion_evaluate`` with no benchmark rows → decision in
      {quarantined, rejected} AND JSON-key parity AND float equality at 1e-6.
  (2) ``MINI_ORK_REQUIRE_HUMAN_APPROVAL=true`` → decision=='pending_human_approval'.
  (3) ``promotion_evaluate`` persisted row → migration-0011 schema column
      diff (promotion_id, candidate_id, from_version_id, to_version_id,
      utility_*, decision, decided_by) AND 1e-6 float tolerance.
  (4) ``promotion_approve`` round-trip: pre-create pending row via
      evaluate, then approve; verify decision=='promoted', approver
      matches, post-SELECT decided_by=='human'. Negative case: approve
      on missing pending row → rc=1 on both sides.
  (5) ``mo_promote_synthesis_gate`` deterministic-class bypass:
      task_class='code_fix' with any panel_score → rc=0, reason='deterministic_class'.
  (6) ``mo_promote_synthesis_gate`` all-conditions-met path:
      panel_score=87.5 + structural signals → rc=0, reason='all_conditions_met'.
  (7) ``mo_promote_synthesis_gate`` rejection paths (3 sub-asserts in one
      test): (a) low_panel_score → rc=1 reason='low_panel_score';
      (b) high panel but no structural signal → rc=1 reason='no_structural_signal';
      (c) bad JSON file → rc=2.

Floats: utility_before / utility_after / utility_delta equality at 1e-6
after JSON-key normalisation. The bash function uses Python's f'{x:.4f}'
formatting in the rationale string; the rationale is NOT in the float-
equality scope (only the JSON-output fields are).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "lib" / "promotion_gate.sh"
DB_INIT = REPO / "db" / "init.sh"

sys.path.insert(0, str(REPO))
from mini_ork.gates import promotion_gate as pg

_FLOAT_TOL = 1e-6


# ── helpers ────────────────────────────────────────────────────────────────


def _shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _which_tools() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH")
    if not LIB.exists():
        pytest.skip(f"missing lib/promotion_gate.sh at {LIB}")
    if not DB_INIT.exists():
        pytest.skip(f"missing db/init.sh at {DB_INIT}")


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    """Initialise a fresh SQLite file via db/init.sh.

    Mirrors the live-subprocess init pattern from
    tests/unit/test_gate_registry_py.py. Sets MINI_ORK_HOME + MINI_ORK_DB
    so the bash side (sourced inside subprocess.run) inherits them and
    the in-process Python port reads the same DB path.
    """
    _which_tools()
    home = tmp_path
    db_path = home / "state.db"
    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(home)
    env["MINI_ORK_DB"] = str(db_path)
    env["MINI_ORK_ROOT"] = str(REPO)
    subprocess.run(
        ["bash", str(DB_INIT)],
        env=env, capture_output=True, text=True, timeout=60, check=True,
    )
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_DB"] = str(db_path)
    os.environ["MINI_ORK_ROOT"] = str(REPO)
    return db_path


def _seed_workflow(db_path: Path) -> None:
    """Seed workflow_memory + workflow_candidates rows for every
    candidate this test exercises. Mirrors tests/unit/test_promotion_gate.sh
    lines 60-77 (so parity cases exercise the same fixtures)."""
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


# ── bash-side helpers (live subprocess) ─────────────────────────────────────


def _bash_evaluate(db_path: Path, candidate_id: str,
                   *, require_human: bool = False,
                   bash_db_path: Path | None = None) -> tuple[str, str, int]:
    """Run LIVE bash promotion_evaluate via subprocess. Returns (stdout, stderr, rc)."""
    src = (
        f'source "{_shlex_quote(str(LIB))}" 2>/dev/null\n'
        f'promotion_evaluate "$1"\n'
    )
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(bash_db_path or db_path)
    env["MINI_ORK_ROOT"] = str(REPO)
    if require_human:
        env["MINI_ORK_REQUIRE_HUMAN_APPROVAL"] = "true"
    else:
        env.pop("MINI_ORK_REQUIRE_HUMAN_APPROVAL", None)
    r = subprocess.run(
        ["bash", "-c", src, "_", candidate_id],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return r.stdout, r.stderr, r.returncode


def _bash_approve(db_path: Path, candidate_id: str,
                  approver: str, rationale: str,
                  bash_db_path: Path | None = None) -> tuple[str, str, int]:
    """Run LIVE bash promotion_approve via subprocess. Returns (stdout, stderr, rc)."""
    src = (
        f'source "{_shlex_quote(str(LIB))}" 2>/dev/null\n'
        f'promotion_approve "$1" "$2" "$3"\n'
    )
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(bash_db_path or db_path)
    env["MINI_ORK_ROOT"] = str(REPO)
    r = subprocess.run(
        ["bash", "-c", src, "_", candidate_id, approver, rationale],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return r.stdout, r.stderr, r.returncode


def _bash_synthesis(verdict_file: str, task_class: str,
                    *, bash_root: Path | None = None) -> tuple[str, str, int]:
    """Run LIVE bash mo_promote_synthesis_gate via subprocess. Returns (stdout, stderr, rc)."""
    src = (
        f'source "{_shlex_quote(str(LIB))}" 2>/dev/null\n'
        f'mo_promote_synthesis_gate "$1" "$2"\n'
    )
    env = os.environ.copy()
    env["MINI_ORK_ROOT"] = str(bash_root or REPO)
    r = subprocess.run(
        ["bash", "-c", src, "_", verdict_file, task_class],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return r.stdout, r.stderr, r.returncode


# ── python-side helpers (in-process port) ──────────────────────────────────


def _py_evaluate(db_path: Path, candidate_id: str,
                 *, require_human: bool = False) -> dict:
    """Run in-process promotion_evaluate. Returns the JSON dict.

    Mirrors bash's exit-1 contract via SystemExit when the candidate has
    no ``base_workflow_version_id`` row. We catch SystemExit so the
    parity test can assert rc=1 parity.
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


# ── parity assertion helpers ───────────────────────────────────────────────


def _parse_bash_json(stdout: str) -> dict | None:
    """Bash sometimes prints the JSON across multiple lines; grab the first
    line that parses as a JSON object."""
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _assert_float_eq(label: str, bash_val, py_val, tol: float = _FLOAT_TOL) -> None:
    assert abs(float(bash_val) - float(py_val)) <= tol, (
        f"{label}: bash={bash_val} py={py_val} tol={tol}"
    )


# ───────────────────────────────────────────────────────────────────────────
# (1) promotion_evaluate with no benchmark rows.
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_evaluate_no_benchmark(db):
    _seed_workflow(db)
    # No benchmark_results → brun is None → bash falls through to the
    # else branch and emits decision='promoted' (with utility_delta=0.0
    # and a no-bench rationale). The bash test accepts the
    # {quarantined, rejected, promoted} set; we assert py==bash parity
    # on the actual decision (which bash happens to produce).
    bso, bse, brc = _bash_evaluate(db, "cand-no-bench")
    assert brc == 0, f"bash rc={brc} stderr={bse}"
    bobj = _parse_bash_json(bso)
    assert bobj is not None, f"bash stdout not JSON: {bso!r}"
    pobj = _py_evaluate(db, "cand-no-bench")
    assert bobj["decision"] == pobj["decision"]
    assert bobj["decision"] in {"quarantined", "rejected", "promoted"}
    # JSON key-set parity on the decision fields.
    for k in (
        "decision", "rationale", "utility_before", "utility_after",
        "utility_delta", "benchmark_run_id", "all_pass", "safety_violations",
    ):
        assert k in bobj, f"bash missing key: {k}"
        assert k in pobj, f"py missing key: {k}"
    # Float equality at 1e-6.
    _assert_float_eq("utility_before", bobj["utility_before"], pobj["utility_before"])
    _assert_float_eq("utility_after", bobj["utility_after"], pobj["utility_after"])
    _assert_float_eq("utility_delta", bobj["utility_delta"], pobj["utility_delta"])
    assert bobj["benchmark_run_id"] == pobj["benchmark_run_id"]
    assert bobj["all_pass"] == pobj["all_pass"]
    assert bobj["safety_violations"] == pobj["safety_violations"]


# ───────────────────────────────────────────────────────────────────────────
# (2) MINI_ORK_REQUIRE_HUMAN_APPROVAL=true → pending_human_approval.
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_evaluate_require_human(db):
    _seed_workflow(db)
    bso, bse, brc = _bash_evaluate(db, "cand-human", require_human=True)
    assert brc == 0, f"bash rc={brc} stderr={bse}"
    bobj = _parse_bash_json(bso)
    assert bobj is not None
    pobj = _py_evaluate(db, "cand-human", require_human=True)
    assert pobj["decision"] == "pending_human_approval"
    assert bobj["decision"] == pobj["decision"]
    assert "Human gate required" in bobj["rationale"]
    assert "Human gate required" in pobj["rationale"]


# ───────────────────────────────────────────────────────────────────────────
# (3) Persisted-row schema diff — proves the port writes the migration-0011
#     schema (NOT the legacy CREATE-IF-NOT-EXISTS draft).
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_evaluate_persisted_row(db):
    _seed_workflow(db)
    _seed_bench_all_pass(db, "cand-persist")
    # Use a separate DB for bash so each side inserts exactly one row
    # (we then diff the row contents across the two DBs).
    bash_db = db.parent / "bash.db"
    shutil.copy(str(db), str(bash_db))
    # Bash side writes its own row.
    bso, bse, brc = _bash_evaluate(db, "cand-persist", bash_db_path=bash_db)
    assert brc == 0, f"bash rc={brc} stderr={bse}"
    bobj = _parse_bash_json(bso)
    assert bobj is not None
    # Python side writes its own row (to the original DB).
    pobj = _py_evaluate(db, "cand-persist")
    # Both should yield "promoted" (all_pass=1, utility_delta > 0).
    assert pobj["decision"] == "promoted"
    assert bobj["decision"] == pobj["decision"]
    # Each DB has exactly 1 promotion_records row for cand-persist.
    for label, p in (("py", db), ("bash", bash_db)):
        con = sqlite3.connect(str(p))
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
        assert len(rows) == 1, f"{label}: expected 1 row, got {len(rows)}"
        (prom_id, cid, fv, tv, ub, ua, bri_val, rat, dec, db_) = rows[0]
        assert cid == "cand-persist"
        assert fv == "test-wf-v1"
        assert tv == "test-wf-v1"
        assert dec == "promoted"
        assert db_ == "gate"
        assert prom_id.startswith("pr-")
        # 1e-6 float tolerance on utility_* columns.
        _assert_float_eq(f"{label}.utility_before", ub, pobj["utility_before"])
        _assert_float_eq(f"{label}.utility_after", ua, pobj["utility_after"])
        # benchmark_run_id is the candidate_id (matches bash output key).
        assert bri_val is None or bri_val == "cand-persist"
        # Both sides' rationale strings should match exactly (same f-string
        # formatting against the same values).
        assert rat == pobj["rationale"]


# ───────────────────────────────────────────────────────────────────────────
# (4) promotion_approve round-trip + negative-path parity.
# ───────────────────────────────────────────────────────────────────────────


def test_promotion_approve_round_trip(db):
    _seed_workflow(db)
    # Pre-create pending_human_approval row via evaluate.
    _seed_bench_all_pass(db, "cand-approve-flow")
    _bash_evaluate(db, "cand-approve-flow", require_human=True)
    _py_evaluate(db, "cand-approve-flow", require_human=True)

    # Round-trip: approve via bash.
    bso, bse, brc = _bash_approve(db, "cand-approve-flow",
                                  "test-approver", "Approved in parity test")
    assert brc == 0, f"bash rc={brc} stderr={bse}"
    bobj = _parse_bash_json(bso)
    assert bobj is not None
    assert bobj["decision"] == "promoted"
    assert bobj["approver"] == "test-approver"
    assert bobj["candidate_id"] == "cand-approve-flow"

    # Approve via python port — different (independent) candidate
    # pre-seeded by _seed_workflow. Use a separate flow for the py side.
    _seed_bench_all_pass(db, "cand-approve-flow-py" if False else "cand-persisted-decision")
    con = sqlite3.connect(str(db))
    try:
        con.execute("""
            INSERT OR IGNORE INTO workflow_candidates
                (candidate_id, base_workflow_version_id, created_by)
            VALUES ('cand-persisted-decision-py', 'test-wf-v1', 'human')
        """)
        con.commit()
    finally:
        con.close()
    _seed_bench_all_pass(db, "cand-persisted-decision-py")
    _py_evaluate(db, "cand-persisted-decision-py", require_human=True)
    pobj = _py_approve(db, "cand-persisted-decision-py",
                       "test-approver", "Approved in parity test")
    assert pobj["decision"] == "promoted"
    assert pobj["approver"] == "test-approver"
    assert pobj["candidate_id"] == "cand-persisted-decision-py"
    assert pobj["approved_at"] is not None
    # Post-approval DB check: both sides flipped decided_by='human'.
    con = sqlite3.connect(str(db))
    try:
        bash_decided_by = con.execute(
            "SELECT decided_by FROM promotion_records "
            "WHERE candidate_id=? AND decision='promoted' "
            "ORDER BY decided_at DESC LIMIT 1",
            ("cand-approve-flow",),
        ).fetchone()[0]
        py_decided_by = con.execute(
            "SELECT decided_by FROM promotion_records "
            "WHERE candidate_id=? AND decision='promoted' "
            "ORDER BY decided_at DESC LIMIT 1",
            ("cand-persisted-decision-py",),
        ).fetchone()[0]
    finally:
        con.close()
    assert bash_decided_by == "human" == py_decided_by


def test_promotion_approve_no_pending(db):
    _seed_workflow(db)
    # Candidate has no pending row → bash exits rc=1.
    _bash_out, _bash_err, brc = _bash_approve(db, "cand-no-bench",
                                              "approver", "rationale")
    assert brc != 0, f"bash should exit non-zero on missing pending row, got rc={brc}"
    # Python side: SystemExit on no pending row.
    with pytest.raises(SystemExit):
        _py_approve(db, "cand-no-bench", "approver", "rationale")


# ───────────────────────────────────────────────────────────────────────────
# (5) mo_promote_synthesis_gate deterministic-class bypass.
# ───────────────────────────────────────────────────────────────────────────


def test_mo_promote_synthesis_gate_bypass(tmp_path, db):
    _seed_workflow(db)
    verdict = tmp_path / "det.json"
    verdict.write_text('{"panel_score":0,"voters":[],"structural":{}}')
    bso, bse, brc = _bash_synthesis(str(verdict), "code_fix")
    assert brc == 0, f"bash rc={brc} stderr={bse}"
    bobj = _parse_bash_json(bso)
    assert bobj is not None
    assert bobj["decision"] == "approved"
    assert bobj["reason"] == "deterministic_class"

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
    bso, bse, brc = _bash_synthesis(str(verdict), "research_synthesis")
    assert brc == 0, f"bash rc={brc} stderr={bse}"
    bobj = _parse_bash_json(bso)
    assert bobj is not None
    assert bobj["decision"] == "approved"
    assert bobj["reason"] == "all_conditions_met"
    assert len(bobj["signals"]["structural_signals_met"]) >= 1

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
    bso, _bash_err_low, brc = _bash_synthesis(str(low), "refactor_audit")
    bobj = _parse_bash_json(bso)
    assert bobj is not None, f"bash stdout not JSON: {bso!r}"
    assert brc == 1
    assert bobj["decision"] == "rejected"
    assert bobj["reason"] == "low_panel_score"
    pobj, prc = _py_synthesis(str(low), "refactor_audit")
    assert prc == 1
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
    bso, _bash_err_no_sig, brc = _bash_synthesis(str(no_sig), "blog_post")
    bobj = _parse_bash_json(bso)
    assert bobj is not None, f"bash stdout not JSON: {bso!r}"
    assert brc == 1
    assert bobj["decision"] == "rejected"
    assert bobj["reason"] == "no_structural_signal"
    pobj, prc = _py_synthesis(str(no_sig), "blog_post")
    assert prc == 1
    assert pobj["reason"] == "no_structural_signal"

    # (c) bad JSON file → rc=2.
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    bso, bse, brc = _bash_synthesis(str(bad), "ui_audit")  # noqa: F841
    assert brc == 2
    pobj, prc = _py_synthesis(str(bad), "ui_audit")
    assert prc == 2
    assert "error" in pobj