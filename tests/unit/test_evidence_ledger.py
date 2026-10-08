"""I5 — evidence ledger in core verify (``mini_ork/verify/evidence_ledger.py``).

Behind ``MO_EVIDENCE_LEDGER=1`` (default OFF). With the flag on, the publisher
backs an approval only with ledger rows whose ``tree`` equals the tree being
published (AC2) and refuses a claimed implementation that left the tree at
``pre-implementer-ref`` (AC3). ``shadow`` writes the ledger + evaluates + records
would-blocks without ever blocking; ``0`` is byte-identical to before (tested).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.cli import publisher  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.verify import evidence_ledger as el  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "target"
    r.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
        _git(r, *args)
    (r / "a.py").write_text("x = 1\n")
    _git(r, "add", "a.py")
    _git(r, "commit", "-qm", "base")
    return r


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _repo(tmp_path)


def _make_db(home: Path, run_id: str = "r") -> str:
    home.mkdir(parents=True, exist_ok=True)
    path = str(home / "state.db")
    rc, _o, err = mig.init_db(db=path, root=str(REPO))
    assert rc == 0, err
    con = sqlite3.connect(path)
    now = int(time.time())
    con.execute("INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,cost_usd,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, "code_fix", "v1", "k", "executing", 0, now, now))
    con.commit()
    con.close()
    return path


@pytest.fixture
def db(tmp_path: Path) -> str:
    return _make_db(tmp_path / ".mini-ork")


# ── mode parsing ────────────────────────────────────────────────────────────

def test_mode_parsing() -> None:
    assert el.mode({}) == "off" and el.mode({"MO_EVIDENCE_LEDGER": "0"}) == "off"
    assert el.mode({"MO_EVIDENCE_LEDGER": "1"}) == "enforce"
    assert el.mode({"MO_EVIDENCE_LEDGER": "shadow"}) == "shadow"
    assert el.mode({"MO_EVIDENCE_LEDGER": "yes"}) == "off"
    assert el.enabled({"MO_EVIDENCE_LEDGER": "1"}) and not el.enabled({"MO_EVIDENCE_LEDGER": "shadow"})


# ── AC1: the tree hash ──────────────────────────────────────────────────────

def test_tree_hash_matches_manual_temp_index(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    (r / "a.py").write_text("x = 2\n")
    (r / "untracked.txt").write_text("new\n")
    h = el.tree_hash(str(r))
    tmpdir = tempfile.mkdtemp(prefix="manual-index-")
    try:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmpdir, "index"))
        subprocess.run(["git", "-C", str(r), "add", "-A"], env=env, check=True, capture_output=True)
        manual = subprocess.run(["git", "-C", str(r), "write-tree"], env=env, check=True,
                                capture_output=True, text=True).stdout.strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    assert h == manual and len(h) == 40


def test_tree_hash_changes_on_edit_and_untracked(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    h1 = el.tree_hash(str(r))
    (r / "a.py").write_text("x = 2\n")          # in-place edit of a tracked file
    h2 = el.tree_hash(str(r))
    (r / "untracked.txt").write_text("new\n")   # an untracked file appears
    h3 = el.tree_hash(str(r))
    assert h1 != h2 != h3
    assert h1 == _git(r, "rev-parse", "HEAD^{tree}")


def test_tree_hash_leaves_index_and_status_untouched(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    (r / "a.py").write_text("x = 2\n")
    (r / "untracked.txt").write_text("new\n")
    before_status = _git(r, "status", "--porcelain")
    before_index = (r / ".git" / "index").read_bytes()
    assert el.tree_hash(str(r))
    assert _git(r, "status", "--porcelain") == before_status
    assert (r / ".git" / "index").read_bytes() == before_index


def test_valid_rows_void_after_edit(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    h1 = el.tree_hash(str(r))
    el.append_row(str(run_dir), ac_id="AC1", probe="pytest", verdict="pass", log="ev.log", tree=h1)
    assert len(el.valid_rows(el.load_rows(str(run_dir)), h1)) == 1
    (r / "a.py").write_text("x = 2\n")
    h2 = el.tree_hash(str(r))
    assert el.valid_rows(el.load_rows(str(run_dir)), h2) == []


# ── AC2: the gate through publish_gate ─────────────────────────────────────

def _gate(tmp_path: Path, r: Path, rows=(), *, gate_mode: str, implementer=None, base_ref=None):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    for row in rows:
        el.append_row(str(run_dir), **row)
    if implementer is not None:
        (run_dir / "implementer-summary.json").write_text(json.dumps(implementer))
    if base_ref is not None:
        (run_dir / "pre-implementer-ref").write_text(base_ref + "\n")
    return el.publish_gate(run_dir=str(run_dir), target_repo=str(r), gate_mode=gate_mode), run_dir


def test_gate_enforce_no_ledger(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    (ok, reason, report), run_dir = _gate(tmp_path, r, gate_mode="enforce")
    assert not ok and reason == "no_ledger"
    assert report["would_block"] is True and report["reasons"] == ["no_ledger"]
    assert (run_dir / "evidence-ledger-gate.json").is_file()


def test_gate_enforce_void_rows_only(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    stale = "0" * 40
    (ok, reason, report), _ = _gate(tmp_path, r, [{"ac_id": "AC1", "probe": "p", "verdict": "pass",
                                                   "log": "e", "tree": stale}], gate_mode="enforce")
    assert not ok and reason == "no_valid_rows"
    assert report["rows_total"] == 1 and report["rows_valid"] == 0


def test_gate_enforce_failing_rows(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    tree = el.tree_hash(str(r))
    rows = [{"ac_id": "AC1", "probe": "p", "verdict": "pass", "log": "e", "tree": tree},
            {"ac_id": "AC1", "probe": "p", "verdict": "fail", "log": "e", "tree": tree}]
    ok, reason, _ = _gate(tmp_path, r, rows, gate_mode="enforce")[0]
    assert not ok and reason == "failing_rows: AC1"


def test_gate_enforce_valid_pass_ok(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    tree = el.tree_hash(str(r))
    ok, reason, report = _gate(tmp_path, r, [{"ac_id": "AC1", "probe": "p", "verdict": "pass",
                                              "log": "e", "tree": tree}], gate_mode="enforce")[0]
    assert ok and reason == ""
    assert report["would_block"] is False and report["reasons"] == []


def test_gate_shadow_evaluates_but_reports(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    (ok, reason, report), run_dir = _gate(tmp_path, r, gate_mode="shadow")
    assert not ok and reason == "no_ledger"
    assert report["mode"] == "shadow" and report["would_block"] is True
    assert {"mode", "would_block", "reasons", "rows_total", "rows_valid", "tree"} <= set(report)
    assert (run_dir / "evidence-ledger-gate.json").is_file()


# ── AC3: an unbacked implementation claim ──────────────────────────────────

def _claimed_implementer(run_dir: Path, base: str, *, tree: str) -> None:
    el.append_row(str(run_dir), ac_id="AC1", probe="p", verdict="pass", log="e", tree=tree)
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"status": "implemented", "files_changed": ["a.py"]}))
    (run_dir / "pre-implementer-ref").write_text(base + "\n")


def test_gate_ac3_implementer_claim_unbacked(tmp_path: Path) -> None:
    r = _repo(tmp_path)  # clean tree == HEAD tree
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    base = _git(r, "rev-parse", "HEAD")
    tree = el.tree_hash(str(r))
    assert tree == _git(r, "rev-parse", "HEAD^{tree}")
    _claimed_implementer(run_dir, base, tree=tree)
    ok, reason, report = el.publish_gate(run_dir=str(run_dir), target_repo=str(r), gate_mode="enforce")
    assert not ok and reason == "implementer_claim_unbacked"
    assert report["would_block"] is True


def test_gate_shadow_records_implementer_claim_unbacked(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    base = _git(r, "rev-parse", "HEAD")
    tree = el.tree_hash(str(r))
    _claimed_implementer(run_dir, base, tree=tree)
    ok, reason, report = el.publish_gate(run_dir=str(run_dir), target_repo=str(r), gate_mode="shadow")
    assert not ok and reason == "implementer_claim_unbacked"


def test_gate_claimed_implementation_with_real_change_is_ok(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    base = _git(r, "rev-parse", "HEAD")
    (r / "a.py").write_text("x = 2\n")  # the implementer's real change
    tree = el.tree_hash(str(r))
    _claimed_implementer(run_dir, base, tree=tree)
    ok, reason, _ = el.publish_gate(run_dir=str(run_dir), target_repo=str(r), gate_mode="enforce")
    assert ok and reason == ""


# ── timeouts map to tree_hash_unavailable ──────────────────────────────────

def test_tree_hash_timeout_returns_none(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(el.subprocess, "run", boom)
    assert el.tree_hash("/definitely/not/a/repo") is None


def test_gate_tree_hash_unavailable(tmp_path: Path, monkeypatch) -> None:
    r = _repo(tmp_path)

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(el.subprocess, "run", boom)
    ok, reason, report = el.publish_gate(run_dir=str(tmp_path / "run"), target_repo=str(r),
                                         gate_mode="enforce")
    assert not ok and reason == "tree_hash_unavailable"
    assert report["reasons"] == ["tree_hash_unavailable"]


# ── the gate through the publisher ──────────────────────────────────────────

def _publish(tmp_path, monkeypatch, db, r, *, flag, rows=(), implementer=None, base_ref=None, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    plan = {"objective": "o", "task_class": "code_fix"}
    (run_dir / "plan.json").write_text(json.dumps(plan))
    for row in rows:
        el.append_row(str(run_dir), **row)
    if implementer is not None:
        (run_dir / "implementer-summary.json").write_text(json.dumps(implementer))
    if base_ref is not None:
        (run_dir / "pre-implementer-ref").write_text(base_ref + "\n")
    for name, value in {"MO_ORACLE_GATES_AUTO": "0", "MO_LEVEL_VECTOR": "0", "MO_TARGET_CWD": str(r),
                        "MINI_ORK_PLAN_PATH": str(run_dir / "plan.json"),
                        "MINI_ORK_RECIPE_ROOT": str(tmp_path / "no-recipes")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MO_PROBE_VALIDITY", raising=False)
    if flag is None:
        monkeypatch.delenv("MO_EVIDENCE_LEDGER", raising=False)
    else:
        monkeypatch.setenv("MO_EVIDENCE_LEDGER", flag)
    capsys.readouterr()
    rc = publisher.publisher_node(str(REPO), str(run_dir), db, "r", "evidence-test", "code_fix")
    out = capsys.readouterr()
    con = sqlite3.connect(db)
    status, notes = con.execute("SELECT status, coalesce(notes,'') FROM task_runs WHERE id='r'").fetchone()
    con.close()
    gate = run_dir / "evidence-ledger-gate.json"
    return rc, status, notes, (json.loads(gate.read_text()) if gate.exists() else None), out


def test_flag_on_refuses_without_ledger(tmp_path, monkeypatch, db, repo, capsys) -> None:
    r = repo
    rc, status, notes, report, out = _publish(tmp_path, monkeypatch, db, r, flag="1", capsys=capsys)
    assert rc == (1, "verdict_fail") and status == "executing"
    assert "evidence_ledger: no_ledger" in notes and report["reason"] == "no_ledger"
    assert "[BLOCK] evidence-ledger: no_ledger" in out.out


def test_flag_on_passes_with_valid_pass(tmp_path, monkeypatch, db, repo, capsys) -> None:
    r = repo
    tree = el.tree_hash(str(r))
    rc, status, _notes, report, out = _publish(
        tmp_path, monkeypatch, db, r, flag="1", capsys=capsys,
        rows=[{"ac_id": "AC1", "probe": "p", "verdict": "pass", "log": "e", "tree": tree}])
    assert rc == (0, "done") and status == "published"
    assert report["would_block"] is False
    assert "[ok] evidence-ledger: pre-publish pass" in out.out


def test_flag_shadow_never_blocks_and_notes(tmp_path, monkeypatch, db, repo, capsys) -> None:
    r = repo
    rc, status, notes, report, out = _publish(tmp_path, monkeypatch, db, r, flag="shadow", capsys=capsys)
    assert rc == (0, "done") and status == "published"
    assert report["mode"] == "shadow" and report["would_block"] is True
    assert "[shadow] evidence ledger would block: no_ledger" in notes
    assert "evidence-ledger" not in out.out + out.err  # shadow prints nothing


@pytest.mark.parametrize("off", [None, "0"])
def test_flag_off_is_byte_identical(tmp_path, monkeypatch, db, repo, capsys, off) -> None:
    r = repo
    rc, status, notes, report, out = _publish(tmp_path, monkeypatch, db, r, flag=off, capsys=capsys)
    assert rc == (0, "done") and status == "published" and report is None
    assert "evidence-ledger" not in out.out + out.err and "evidence_ledger" not in notes
    assert not (tmp_path / "run" / "evidence-ledger.jsonl").exists()
    assert not (tmp_path / "run" / "evidence-ledger-gate.json").exists()


# ── writer (a): the generic verifier handler ───────────────────────────────

def _writer_arm(tmp_path, monkeypatch, db, *, ac_id=None, flag="shadow", write_exc=None):
    r = _repo(tmp_path)
    rd = tmp_path / "run"
    rd.mkdir(exist_ok=True)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "artifact_contract": {"outputs": [str(rd / "self-migrate.diff")]}}))

    def stub_verifier(script, evidence_path, **_kw):
        payload = {"pass": True}
        if ac_id is not None:
            payload["ac_id"] = ac_id
        Path(evidence_path).write_text(json.dumps(payload) + "\n")
        return 0

    monkeypatch.setattr(ex, "_run_verifier_ref", stub_verifier)
    monkeypatch.setenv("MO_EVIDENCE_LEDGER", flag)
    monkeypatch.setenv("MO_TARGET_CWD", str(r))
    if write_exc is not None:
        monkeypatch.setattr(el, "append_row", write_exc)
    node_id = "pre_retirement_parity"
    rc, fr = ex.dispatch_node(
        (node_id, "verifier", f"do {node_id}", "", "serial", "verifiers/pre-retirement-parity.py", "verifier", ""),
        root=str(REPO), run_dir=str(rd), plan_path=str(plan),
        task_class="self_migrate", db=db, run_id="r1", dispatch_fn=lambda *a: (0, ""),
        recipe="self-migrate", workflow=str(REPO / "recipes" / "self-migrate" / "workflow.yaml"))
    return rc, fr, rd, r


def test_writer_a_appends_one_row_with_ac_id(tmp_path, monkeypatch, db) -> None:
    rc, fr, rd, r = _writer_arm(tmp_path, monkeypatch, db, ac_id="AC1")
    assert (rc, fr) == (0, "done")
    rows = el.load_rows(str(rd))
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"ts", "row_id", "ac_id", "probe", "verdict", "tree", "log"}
    assert row["ac_id"] == "AC1"
    assert row["probe"].endswith(os.path.join("verifiers", "pre-retirement-parity.py"))
    assert row["verdict"] == "pass"
    assert row["log"].endswith(os.path.join("evidence", "pre-retirement-parity.log"))
    assert row["tree"] == el.tree_hash(str(r))


def test_writer_a_falls_back_to_verifier_stem(tmp_path, monkeypatch, db) -> None:
    rc, fr, rd, r = _writer_arm(tmp_path, monkeypatch, db)
    assert (rc, fr) == (0, "done")
    row = el.load_rows(str(rd))[0]
    assert row["ac_id"] == "verifier:pre-retirement-parity"


def test_writer_a_write_exception_warns_and_does_not_fail(tmp_path, monkeypatch, db, capsys) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    rc, fr, rd, _ = _writer_arm(tmp_path, monkeypatch, db, write_exc=boom)
    assert (rc, fr) == (0, "done")
    assert "evidence-ledger write skipped" in capsys.readouterr().err
    assert not (rd / "evidence-ledger.jsonl").exists()


def test_writer_a_flag_off_writes_nothing(tmp_path, monkeypatch, db) -> None:
    rc, fr, rd, _ = _writer_arm(tmp_path, monkeypatch, db, flag="0")
    assert (rc, fr) == (0, "done")
    assert not (rd / "evidence-ledger.jsonl").exists()

