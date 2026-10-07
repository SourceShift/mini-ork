"""I1 — probe validity in core verify (``mini_ork/verify/probe_validity.py``).

Behind ``MO_PROBE_VALIDITY=1`` (default OFF). With the flag on, the publisher
refuses a run whose verify proved nothing (AC3), whose probes are aliased
(AC1), or whose probes already pass on the untouched base tree (AC2). With the
flag off, behaviour is byte-identical to before (tested).
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import publisher  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.verify import probe_validity as pv  # noqa: E402

SDD = REPO / "recipes" / "spec-driven-delivery" / "verifiers"


# ── moved helpers (and the SDD delegates) ───────────────────────────────────

def test_pass_definition_and_vacuity_rules() -> None:
    assert pv.expect_matches("exit 0", 0, "") and not pv.expect_matches("exit 0", 1, "")
    assert pv.expect_matches("ok$", 0, "all ok") and not pv.expect_matches("ok$", 0, "nope")
    assert all(pv.is_vacuous_probe(p) for p in ("", "true", ":", "exit 0;", None))
    assert not pv.is_vacuous_probe("pytest -q tests/test_x.py")
    assert pv.vacuous_expect("") == "expect is empty"
    assert pv.vacuous_expect(".*") == "expect is satisfied by any output"
    assert pv.vacuous_expect("exit 0") is None and pv.vacuous_expect("2 passed") is None
    res = pv.run_probe("echo 3 passed", "passed", timeout=10)
    assert res["status"] == "PASSED" and res["exit_code"] == 0


def test_sdd_common_delegates_to_core(monkeypatch) -> None:
    monkeypatch.setenv("MINI_ORK_ENGINE_ROOT", str(REPO))
    monkeypatch.syspath_prepend(str(SDD))
    spec = importlib.util.spec_from_file_location("_sdd_common", SDD / "_sdd_common.py")
    assert spec is not None and spec.loader is not None
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    assert common.probe_validity() is pv
    assert common.is_vacuous_probe("true") and common.vacuous_expect(".*") == pv.vacuous_expect(".*")
    assert common.run_probe("false", "exit 0", timeout=10)["status"] == "FAILED"


# ── AC1: aliasing + coverage ────────────────────────────────────────────────

def test_one_probe_shared_across_criteria_is_aliased() -> None:
    shared = [{"gate_id": "G1", "acceptance_ref": "AC1", "probe": "pytest -q t.py", "expect": "exit 0"},
              {"gate_id": "G2", "acceptance_ref": "AC2", "probe": "pytest  -q t.py", "expect": "exit 0"}]
    out = pv.aliasing_violations(shared)
    assert len(out) == 1 and out[0].startswith("aliased_probe:") and "AC1, AC2" in out[0]
    listed = [{"gate_id": "G3", "acceptance_refs": ["AC1", "AC2"], "probe": "x"}]
    assert pv.aliasing_violations(listed)[0].startswith("aliased_probe: G3 covers 2")
    distinct = [{"gate_id": "G1", "acceptance_ref": "AC1", "probe": "a"},
                {"gate_id": "G2", "acceptance_ref": "AC2", "probe": "b"}]
    assert pv.aliasing_violations(distinct) == []


def test_coverage_needs_exactly_one_probe_per_criterion() -> None:
    probes = [{"acceptance_ref": "AC1"}, {"acceptance_ref": "AC1"}, {"acceptance_ref": "AC9"}]
    out = pv.coverage_violations(["AC1", "AC2"], probes)
    assert "acceptance 'AC1' has 2 probes, need exactly 1" in out
    assert "acceptance 'AC2' has 0 probes, need exactly 1" in out
    assert any("'AC9' is not a declared acceptance criterion" in v for v in out)


@pytest.mark.parametrize("passed, pre, delivered, status, violation", [
    (False, False, False, "FAILS_TODAY", None),
    (True, False, False, "VACUOUS", "probe already passes on the untouched tree (vacuous)"),
    (True, False, True, "DELIVERED_OK", None),
    (True, True, False, "PRECONDITION_OK", None),
    (False, True, False, "PRECONDITION_FAILED", "precondition probe does not pass now (exit 1)"),
])
def test_base_run_classification(passed, pre, delivered, status, violation) -> None:
    got = pv.classify_base_run(passed, precondition=pre, delivered=delivered, run_reason="exit 1")
    assert (got[0], got[2]) == (status, violation)


# ── AC2: the base tree ──────────────────────────────────────────────────────

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    r = tmp_path / "target"
    r.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
        _git(r, *args)
    (r / "a.py").write_text("x = 1\n")
    _git(r, "add", "a.py")
    _git(r, "commit", "-qm", "base")
    base = _git(r, "rev-parse", "HEAD")
    (r / "new.txt").write_text("added by the change\n")  # the implementer's work (uncommitted is fine)
    return r, base


def test_a_probe_that_passes_on_base_is_vacuous(repo) -> None:
    r, base = repo
    rows = pv.run_on_base([
        {"gate_id": "P-new", "acceptance_ref": "AC1", "probe": "test -f new.txt", "expect": "exit 0"},
        {"gate_id": "P-old", "acceptance_ref": "AC2", "probe": "test -f a.py", "expect": "exit 0"},
    ], target_repo=str(r), base_ref=base, timeout=10)
    by = {row["gate_id"]: row for row in rows}
    assert by["P-new"]["status"] == "FAILS_TODAY" and by["P-new"]["violation"] is None
    assert by["P-old"]["status"] == "VACUOUS" and by["P-old"]["violation"]
    assert _git(r, "worktree", "list").count("\n") == 0  # throwaway checkout removed


def test_absolute_target_paths_are_remapped_to_the_base_tree(repo) -> None:
    # Without the remap this probe would read the CHANGED tree and pass on "base".
    r, base = repo
    rows = pv.run_on_base([{"gate_id": "P-abs", "acceptance_ref": "AC1",
                            "probe": f"test -f {r}/new.txt", "expect": "exit 0"}],
                          target_repo=str(r), base_ref=base, timeout=10)
    assert rows[0]["status"] == "FAILS_TODAY"


# ── AC3: did verify prove anything ──────────────────────────────────────────

@pytest.fixture
def db(tmp_path: Path) -> str:
    h = tmp_path / ".mini-ork"
    h.mkdir()
    path = str(h / "state.db")
    rc, _o, err = mig.init_db(db=path, root=str(REPO))
    assert rc == 0, err
    con = sqlite3.connect(path)
    now = int(time.time())
    con.execute("INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,cost_usd,created_at,"
                "updated_at) VALUES ('r','code_fix','v1','k','executing',0,?,?)", (now, now))
    con.commit()
    con.close()
    return path


def _verifier_end(db: str, node: str, ms: int, finish: str) -> None:
    con = sqlite3.connect(db)
    con.execute("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at, finish_reason) "
                "VALUES (?,?,?,?,?,?)", (f"e-{node}", "r", "node_end",
                                         json.dumps({"node_id": node, "node_type": "verifier", "duration_ms": ms,
                                                     "finish_reason": finish}), int(time.time()), finish))
    con.commit()
    con.close()


def test_verify_proven_needs_a_done_verifier_with_passing_evidence(db: str, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert pv.verify_proven(db, "r", str(run_dir))[0] is False           # nothing at all
    _verifier_end(db, "static", 0, "error")
    assert pv.verify_proven(db, "r", str(run_dir))[1]["errored"] == 1    # errored: not proof
    _verifier_end(db, "test", 0, "done")                                 # 0 ms is NORMAL (fallback emitter)
    assert pv.verify_proven(db, "r", str(run_dir))[0] is False           # done but no evidence file
    (run_dir / "verifier_test.json").write_text('[test] running\n{"verifier":"test","pass":false}\n')
    assert pv.verify_proven(db, "r", str(run_dir))[0] is False           # evidence says fail
    (run_dir / "verifier_typecheck.json").write_text('{"verifier":"typecheck","pass":true}')
    proven, detail = pv.verify_proven(db, "r", str(run_dir))
    assert proven and detail["evidence_pass"] == 1 and detail["done"] == 1
    (run_dir / "verifier_typecheck.json").unlink()
    (run_dir / "verifier_lint.log").write_text('[lint] ok\n{"verifier":"lint","pass":true}\n')  # pre-Sept name
    assert pv.verify_proven(db, "r", str(run_dir))[0]


# ── the gate through the publisher ──────────────────────────────────────────

def _publish(tmp_path, monkeypatch, db, repo, base, *, checks=None, flag="1"):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    plan = {"objective": "o", "task_class": "code_fix", "verifier_contract": {"checks": checks or []}}
    (run_dir / "plan.json").write_text(json.dumps(plan))
    (run_dir / "pre-implementer-ref").write_text(base + "\n")
    for name, value in {"MO_ORACLE_GATES_AUTO": "0", "MO_LEVEL_VECTOR": "0", "MO_TARGET_CWD": str(repo),
                        "MINI_ORK_PLAN_PATH": str(run_dir / "plan.json"),
                        "MINI_ORK_RECIPE_ROOT": str(tmp_path / "no-recipes")}.items():
        monkeypatch.setenv(name, value)
    if flag is None:
        monkeypatch.delenv("MO_PROBE_VALIDITY", raising=False)
    else:
        monkeypatch.setenv("MO_PROBE_VALIDITY", flag)
    rc = publisher.publisher_node(str(REPO), str(run_dir), db, "r", "probe-test", "code_fix")
    con = sqlite3.connect(db)
    status, notes = con.execute("SELECT status, coalesce(notes,'') FROM task_runs WHERE id='r'").fetchone()
    con.close()
    report = run_dir / "probe-validity.json"
    return rc, status, notes, (json.loads(report.read_text()) if report.exists() else None)


def _passing_verifier(db: str, tmp_path: Path) -> None:
    """A verifier node that ran and passed: node_end done + its evidence file."""
    _verifier_end(db, "test", 0, "done")
    (tmp_path / "run").mkdir(exist_ok=True)
    (tmp_path / "run" / "verifier_test.json").write_text('{"verifier":"test","pass":true}')


def test_flag_on_refuses_a_verify_that_proved_nothing(tmp_path, monkeypatch, db, repo, capsys) -> None:
    r, base = repo
    rc, status, notes, report = _publish(tmp_path, monkeypatch, db, r, base)
    assert rc == (1, "verdict_fail") and status == "executing"
    assert "probe_validity: verify_vacuous" in notes and report["reason"] == "verify_vacuous"
    assert "[BLOCK] probe-validity: verify_vacuous" in capsys.readouterr().out


def test_flag_on_a_done_verifier_without_evidence_proves_nothing(tmp_path, monkeypatch, db, repo) -> None:
    # e.g. the "[warn] verifier node: no outputs in artifact_contract" path: done, nothing ran
    r, base = repo
    _verifier_end(db, "static", 0, "done")
    assert _publish(tmp_path, monkeypatch, db, r, base)[3]["reason"] == "verify_vacuous"


def test_flag_on_refuses_aliased_probes(tmp_path, monkeypatch, db, repo) -> None:
    r, base = repo
    _passing_verifier(db, tmp_path)
    checks = [{"id": "c1", "acceptance_ref": "AC1", "command": "test -f new.txt"},
              {"id": "c2", "acceptance_ref": "AC2", "command": "test -f new.txt"}]
    rc, _status, notes, report = _publish(tmp_path, monkeypatch, db, r, base, checks=checks)
    assert rc == (1, "verdict_fail") and report["reason"] == "aliased_probe" and "aliased_probe" in notes


def test_flag_on_refuses_a_probe_that_passes_on_the_base_tree(tmp_path, monkeypatch, db, repo) -> None:
    r, base = repo
    _passing_verifier(db, tmp_path)
    checks = [{"id": "c1", "acceptance_ref": "AC1", "command": "test -f a.py"}]
    rc, _status, _notes, report = _publish(tmp_path, monkeypatch, db, r, base, checks=checks)
    assert rc == (1, "verdict_fail") and report["reason"] == "probe_passes_on_base"


def test_flag_on_valid_probes_pass_the_gate(tmp_path, monkeypatch, db, repo, capsys) -> None:
    r, base = repo
    _passing_verifier(db, tmp_path)
    checks = [{"id": "c1", "acceptance_ref": "AC1", "command": "test -f new.txt"}]
    rc, status, _notes, report = _publish(tmp_path, monkeypatch, db, r, base, checks=checks)
    assert report["ok"] is True and rc == (0, "done") and status == "published"
    assert "[ok] probe-validity: pre-publish pass" in capsys.readouterr().out


@pytest.mark.parametrize("off", [None, "0"])
def test_flag_off_is_byte_identical(tmp_path, monkeypatch, db, repo, capsys, off) -> None:
    # Same fixture as the refusing case above (no verifier evidence at all):
    # with the flag off the publisher behaves exactly as before I1.
    r, base = repo
    rc, status, notes, report = _publish(tmp_path, monkeypatch, db, r, base, flag=off)
    out = capsys.readouterr()
    assert rc == (0, "done") and status == "published" and report is None
    assert "probe-validity" not in out.out + out.err and "probe_validity" not in notes


# ── SDD test-validity: aliasing only with the flag on ───────────────────────

def _load_test_validity(monkeypatch):
    monkeypatch.setenv("MINI_ORK_ENGINE_ROOT", str(REPO))
    monkeypatch.syspath_prepend(str(SDD))
    spec = importlib.util.spec_from_file_location("sdd_test_validity", SDD / "test-validity.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("flag, aliased", [(None, False), ("1", True)])
def test_sdd_test_validity_flags_aliasing_only_with_the_flag(tmp_path, monkeypatch, flag, aliased) -> None:
    tv = _load_test_validity(monkeypatch)
    if flag:
        monkeypatch.setenv("MO_PROBE_VALIDITY", flag)
    else:
        monkeypatch.delenv("MO_PROBE_VALIDITY", raising=False)
    (tmp_path / "gates").mkdir()
    card = {"spec_id": "S1", "source_hash": "h",
            "acceptance": [{"id": "AC1", "gate": {"kind": "cmd"}}, {"id": "AC2", "gate": {"kind": "cmd"}}]}
    probe = {"kind": "cmd", "probe": "false", "expect": "exit 0"}
    (tmp_path / "gates" / "S1.json").write_text(json.dumps({"spec_id": "S1", "source_hash": "h", "probes": [
        {**probe, "gate_id": "G1", "acceptance_ref": "AC1"}, {**probe, "gate_id": "G2", "acceptance_ref": "AC2"}]}))
    out, rows = tv.check_spec(tmp_path, card, timeout=10, env=None)
    assert [r["status"] for r in rows] == ["FAILS_TODAY", "FAILS_TODAY"]
    assert any(v.startswith("aliased_probe") for v in out) is aliased
