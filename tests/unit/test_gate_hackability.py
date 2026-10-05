"""Hermetic unit tests for ``mini_ork.gates.hackability`` (G09-T05).

Attacks a registered gate with known-bad ("hollow") inputs, persists
``hackability = passed_bad / trials`` next to the DB, and makes the promotion
gate refuse a promote that depends on a gate above the threshold.

Every test is hermetic: no LLM, no network. The DB is seeded via
``migrate.init_db`` and gates are registered via ``gate_registry.gate_register``.
A hackable stub type is registered by ``monkeypatch.setitem`` on
``GATE_EVALUATORS`` — the same OCP extension path production uses, never an edit
to the registry. The seed helpers mirror ``test_promotion_gate_py.py`` (do not
import that module; copy the pattern).

Cases (kickoff §Tests):

    (1)  a hackable gate audits to 1.0 with the 5 operator ids as exploits
    (2)  a sound scope_gate audits to 0.0
    (3)  crash exclusion: raises + a non-verdict return are UNVERIFIED
    (4)  all trials crash → hackability None → promotion_check no_valid_trials
    (5)  proposer witness: only the hollow document is evaluated
    (6)  lane_proposer argv/cost/parsing + budget_exhausted; parse_proposals
    (7)  is_hollow_document truth table
    (8)  env isolation: a leaked MO_MUTATION_REPORT must not flip the audit
    (9)  production E2E: gate-fuzz --hackability → record → promotion rejects
    (10) below threshold → promoted, gate in within_threshold
    (11) never audited → no_record; audited then stale → still promoted
    (12) knob off → 10 legacy keys; legacy --json byte-identical
    (13) CLI usage errors exit 2 and write nothing
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
from mini_ork.cli import gate_fuzz  # noqa: E402
from mini_ork.gates import (  # noqa: E402
    gate_fuzzer,
    gate_registry,
    hackability,
    promotion_gate,
)
from mini_ork.stores import migrate as mig  # noqa: E402

_OPERATOR_IDS = [
    "empty-context",
    "dangling-evidence",
    "empty-document",
    "hollow-object",
    "zero-leaf-skeleton",
]


# ── fixtures / seed helpers (mirror test_promotion_gate_py.py) ───────────────


@pytest.fixture()
def db(tmp_path):
    """Fresh SQLite DB via init_db; sets the MINI_ORK_* env triple."""
    home = tmp_path
    db_path = home / "state.db"
    rc, out, err = mig.init_db(db=str(db_path), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_DB"] = str(db_path)
    os.environ["MINI_ORK_ROOT"] = str(REPO)
    return db_path


def _register(db_path: Path, gate_type: str, condition: str) -> str:
    """Register a gate and return its id (NULL task_class_filter)."""
    gid = gate_registry.gate_register(str(db_path), gate_type, condition)
    assert gid, f"gate_register returned empty id for {gate_type}"
    return gid


def _seed_workflow(db_path: Path) -> None:
    """Seed workflow_memory + workflow_candidates rows (mirrors the promotion
    test fixture)."""
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("""
            INSERT OR IGNORE INTO workflow_memory
                (workflow_version_id, workflow_name, yaml_hash, yaml_blob)
            VALUES ('test-wf-v1', 'test-wf', 'deadbeef', '# test')
        """)
        for cid in ("cand-e2e", "cand-below", "cand-never", "cand-stale", "cand-off"):
            con.execute("""
                INSERT OR IGNORE INTO workflow_candidates
                    (candidate_id, base_workflow_version_id, created_by)
                VALUES (?, 'test-wf-v1', 'human')
            """, (cid,))
        con.commit()
    finally:
        con.close()


def _seed_bench(db_path: Path, candidate_id: str, rows) -> None:
    """Seed benchmark_results rows. ``rows``: list of (benchmark_id, run_id, pass)."""
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
    """4 passing benchmark rows across 3 independent runs."""
    _seed_bench(db_path, candidate_id, [
        ("bench-task-1", 1, 1),
        ("bench-task-2", 2, 1),
        ("bench-task-3", 3, 1),
        ("bench-task-4", 1, 1),
    ])


# ─────────────────────────────────────────────────────────────────────────────
# (1) hackable gate audits to 1.0
# ─────────────────────────────────────────────────────────────────────────────


def test_hackable_gate_audits_1_0(db, monkeypatch):
    monkeypatch.setitem(gate_registry.GATE_EVALUATORS, "hk_open", lambda *a: "pass")
    gid = _register(db, "hk_open", "stub-condition")

    record = hackability.audit_gate(str(db), gid)
    assert record["hackability"] == 1.0
    assert record["trials"] == 5
    assert record["passed_bad"] == 5
    assert record["exploits"] == _OPERATOR_IDS
    assert record["unverified"] == {"crashed": 0, "not_known_bad": 0}

    # The record survives a write/read round-trip (latest measurement wins).
    assert hackability.read_record(str(db), gid) is None
    hackability.write_record(str(db), record)
    assert hackability.read_record(str(db), gid) == record


# ─────────────────────────────────────────────────────────────────────────────
# (2) a sound scope_gate audits to 0.0
# ─────────────────────────────────────────────────────────────────────────────


def test_scope_gate_sound(db):
    gid = _register(db, "scope_gate", '["code_fix"]')
    record = hackability.audit_gate(str(db), gid)
    assert record["hackability"] == 0.0
    assert record["exploits"] == []
    assert record["trials"] == 5
    assert record["passed_bad"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (3) crash exclusion
# ─────────────────────────────────────────────────────────────────────────────


def test_crash_exclusion(db):
    gid = _register(db, "scope_gate", '["code_fix"]')
    sequence = iter([Exception("boom"), Exception("boom"), "maybe", "pass", "defer"])

    def evaluate(ctx_json):
        item = next(sequence)
        if isinstance(item, Exception):
            raise item
        return item

    record = hackability.audit_gate(str(db), gid, evaluate=evaluate)
    assert record["trials"] == 2
    assert record["passed_bad"] == 1
    assert record["hackability"] == 0.5
    assert record["unverified"]["crashed"] == 3
    assert record["unverified"]["not_known_bad"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (4) all trials crash → hackability None → no_valid_trials
# ─────────────────────────────────────────────────────────────────────────────


def test_all_crash_hackability_none(db):
    gid = _register(db, "scope_gate", '["code_fix"]')

    def evaluate(ctx_json):
        raise RuntimeError("boom")

    record = hackability.audit_gate(str(db), gid, evaluate=evaluate)
    assert record["hackability"] is None
    assert record["trials"] == 0
    assert record["unverified"]["crashed"] == 5

    hackability.write_record(str(db), record)
    check = hackability.promotion_check(str(db), "code_fix")
    assert check["unmeasured"] == [{"gate_id": gid, "reason": "no_valid_trials"}]
    assert check["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# (5) proposer witness
# ─────────────────────────────────────────────────────────────────────────────


def test_proposer_witness(db, monkeypatch):
    monkeypatch.setitem(gate_registry.GATE_EVALUATORS, "hk_open", lambda *a: "pass")
    gid = _register(db, "hk_open", "stub-condition")

    calls = []

    def evaluate(ctx_json):
        calls.append(ctx_json)
        return "defer"

    def stub_proposer(context, n):
        return {
            "documents": [
                '{"voters":[{}]}',
                '{"panel_score": 95}',
                "not json",
                '{"outcome": "PROVEN"}',
            ],
            "cost_usd": 0.0,
            "status": "ok",
        }

    record = hackability.audit_gate(
        str(db), gid, proposer=stub_proposer, n=4, evaluate=evaluate,
    )
    # 5 operators + the one hollow proposer document.
    assert len(calls) == 6

    by_id = {r["id"]: r for r in record["results"]}
    assert by_id["proposer-1"]["outcome"] == "REFUTED"
    for tid in ("proposer-2", "proposer-3", "proposer-4"):
        assert by_id[tid]["outcome"] == "UNVERIFIED"
        assert by_id[tid]["reason"] == "not_known_bad"
        assert by_id[tid]["verdict"] is None
    assert record["unverified"]["not_known_bad"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# (6) lane_proposer
# ─────────────────────────────────────────────────────────────────────────────


def test_lane_proposer(db, monkeypatch):
    calls = []

    def fake_llm_dispatch(argv=None, *, root=None, dispatch_fn=None):
        assert argv is not None
        calls.append(list(argv))
        out = argv[argv.index("--out") + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write('noise [[1, 2], "s", 5] trailing')
        with open(f"{out}.cost", "w", encoding="utf-8") as f:
            f.write("0.42")
        return 0

    monkeypatch.setattr("mini_ork.dispatch.llm_dispatch.llm_dispatch", fake_llm_dispatch)

    proposer = hackability.lane_proposer("glm", budget_usd=1.0)
    ctx = {"gate_type": "hk_open", "condition": "stub"}
    result = proposer(ctx, 4)

    assert len(calls) == 1
    argv = calls[0]
    assert argv[argv.index("--model") + 1] == "glm"
    assert argv[argv.index("--node-type") + 1] == "gate-hackability-propose"
    # Non-string items are json.dumps'd (strings pass through); first n kept.
    assert result["documents"] == [json.dumps([1, 2]), "s", json.dumps(5)]
    assert result["cost_usd"] == pytest.approx(0.42)
    assert result["status"] == "ok"

    # budget_usd=0 ⇒ budget_exhausted with no call.
    p0 = hackability.lane_proposer("glm", budget_usd=0.0)
    r0 = p0(ctx, 4)
    assert r0["status"] == "budget_exhausted"
    assert r0["documents"] == []
    assert len(calls) == 1

    # parse_proposals returns None on text with no array.
    assert hackability.parse_proposals("no array here", 4) is None
    assert hackability.parse_proposals(None, 4) is None


# ─────────────────────────────────────────────────────────────────────────────
# (7) is_hollow_document truth table
# ─────────────────────────────────────────────────────────────────────────────


def test_is_hollow_document_table():
    hollow = [
        None,
        "",
        "   ",
        "{}",
        "[]",
        '{"a":[{}],"b":0,"c":null,"d":false,"e":" "}',
    ]
    non_hollow = [
        '{"a":1}',
        '{"a":"x"}',
        "true",
        "garbage",
        "NaN",
    ]
    for text in hollow:
        assert hackability.is_hollow_document(text) is True, repr(text)
    for text in non_hollow:
        assert hackability.is_hollow_document(text) is False, repr(text)


# ─────────────────────────────────────────────────────────────────────────────
# (8) env isolation
# ─────────────────────────────────────────────────────────────────────────────


def test_env_isolation(db, monkeypatch, tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"kill_rate": 0.95, "total": 10}))
    monkeypatch.setenv("MO_MUTATION_REPORT", str(report))

    gid = _register(db, "custom", "native:mutation-adversary")
    record = hackability.audit_gate(str(db), gid)
    assert record["hackability"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# (9) production E2E: gate-fuzz --hackability → record → promotion rejects
# ─────────────────────────────────────────────────────────────────────────────


def test_production_e2e(db, monkeypatch, capsys):
    monkeypatch.setitem(gate_registry.GATE_EVALUATORS, "hk_open", lambda *a: "pass")
    gid = _register(db, "hk_open", "stub-condition")

    rc = gate_fuzz.main(["--hackability", "--gate", gid, "--db", str(db), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["records"][0]["hackability"] == 1.0
    assert os.path.isfile(hackability.record_path(str(db), gid))

    _seed_workflow(db)
    _seed_bench_all_pass(db, "cand-e2e")
    pobj = promotion_gate.promotion_evaluate(str(db), "cand-e2e")
    assert pobj["decision"] == "rejected"
    assert f"gate-hackability:{gid}=1.000>0.25" in pobj["rationale"]

    con = sqlite3.connect(str(db))
    try:
        decision = con.execute(
            "SELECT decision FROM promotion_records WHERE candidate_id=?",
            ("cand-e2e",),
        ).fetchone()[0]
    finally:
        con.close()
    assert decision == "rejected"


# ─────────────────────────────────────────────────────────────────────────────
# (10) below threshold → promoted, gate in within_threshold
# ─────────────────────────────────────────────────────────────────────────────


def test_below_threshold(db, capsys):
    gid = _register(db, "scope_gate", '["code_fix"]')
    rc = gate_fuzz.main(["--hackability", "--gate", gid, "--db", str(db), "--json"])
    assert rc == 0

    _seed_workflow(db)
    _seed_bench_all_pass(db, "cand-below")
    pobj = promotion_gate.promotion_evaluate(str(db), "cand-below")
    assert pobj["decision"] == "promoted"
    assert {"gate_id": gid, "hackability": 0.0} in pobj["gate_hackability"]["within_threshold"]


# ─────────────────────────────────────────────────────────────────────────────
# (11) never audited → no_record; audited then stale → still promoted
# ─────────────────────────────────────────────────────────────────────────────


def test_unmeasured_and_stale(db, capsys):
    gid = _register(db, "scope_gate", '["code_fix"]')

    _seed_workflow(db)
    _seed_bench_all_pass(db, "cand-never")
    pobj = promotion_gate.promotion_evaluate(str(db), "cand-never")
    assert pobj["decision"] == "promoted"
    assert {"gate_id": gid, "reason": "no_record"} in pobj["gate_hackability"]["unmeasured"]

    # Audit, then UPDATE the condition → the record is now stale.
    rc = gate_fuzz.main(["--hackability", "--gate", gid, "--db", str(db), "--json"])
    assert rc == 0
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "UPDATE gate_registry SET condition='[\"other_class\"]' WHERE gate_id=?",
            (gid,),
        )
        con.commit()
    finally:
        con.close()

    _seed_bench_all_pass(db, "cand-stale")
    pobj2 = promotion_gate.promotion_evaluate(str(db), "cand-stale")
    assert pobj2["decision"] == "promoted"
    assert {"gate_id": gid, "reason": "stale"} in pobj2["gate_hackability"]["unmeasured"]


# ─────────────────────────────────────────────────────────────────────────────
# (12) knob off → 10 legacy keys; legacy --json byte-identical
# ─────────────────────────────────────────────────────────────────────────────

_LEGACY_RESULT_KEYS = {
    "decision", "rationale", "utility_before", "utility_after", "utility_delta",
    "benchmark_run_id", "n_runs", "all_pass", "safety_violations", "verifier_audit",
}


def test_knobs_off(db, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MO_PROMOTION_GATE_HACKABILITY", "0")
    monkeypatch.setitem(gate_registry.GATE_EVALUATORS, "hk_open", lambda *a: "pass")
    gid = _register(db, "hk_open", "stub-condition")
    hackability.write_record(str(db), hackability.audit_gate(str(db), gid))

    _seed_workflow(db)
    _seed_bench_all_pass(db, "cand-off")
    pobj = promotion_gate.promotion_evaluate(str(db), "cand-off")
    assert pobj["decision"] == "promoted"
    assert set(pobj) == _LEGACY_RESULT_KEYS

    # Legacy --json path: byte-identical, and no gate-hackability dir is created.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    mig.init_db(db=str(fresh / "state.db"), root=str(REPO))
    monkeypatch.setenv("MINI_ORK_DB", str(fresh / "state.db"))

    expected = json.dumps(
        gate_fuzzer.fuzz_gate(
            gate_fuzzer.artifact_contract_evaluator(str(tmp_path / "w")),
            gate_fuzzer.load_corpus(gate_fuzzer.DEFAULT_CORPUS),
        ),
        indent=2,
        sort_keys=True,
    ) + "\n"
    rc = gate_fuzz.main(["--json"])
    assert rc == 0
    assert capsys.readouterr().out == expected
    assert not os.path.isdir(os.path.join(str(fresh), "gate-hackability"))


# ─────────────────────────────────────────────────────────────────────────────
# (13) CLI usage errors exit 2 and write nothing
# ─────────────────────────────────────────────────────────────────────────────


def test_cli_usage_errors(db, capsys):
    # --gate / --db / --proposer-lane without --hackability.
    assert gate_fuzz.main(["--gate", "x"]) == 2
    assert gate_fuzz.main(["--db", "x"]) == 2
    assert gate_fuzz.main(["--proposer-lane", "glm"]) == 2
    # --hackability without --gate.
    assert gate_fuzz.main(["--hackability", "--db", str(db)]) == 2
    # --hackability with --corpus.
    assert gate_fuzz.main(["--hackability", "--gate", "x", "--corpus", "c"]) == 2
    # An unknown gate exits 2 and writes no record.
    assert gate_fuzz.main(["--hackability", "--gate", "nope", "--db", str(db)]) == 2
    assert not os.path.isdir(hackability.records_dir(str(db)))
