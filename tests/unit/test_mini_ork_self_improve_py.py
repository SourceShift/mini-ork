"""Unit tests: mini_ork.cli.self_improve (bash parity halves removed; formerly vs bin/mini-ork-self-improve).

The outer loop creates git worktrees + dispatches LLM runs — integration. Here
we test the deterministic surface: early-exit flag handling, the
outcome-decision cascade (iter-33/34 bug-prone), and the DB-writing helpers
(rows asserted semantically).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import self_improve as si


# ── early-exit flag handling ──

def test_help_unknown_badcaps(tmp_path):
    assert si.main(["--help"], root=str(REPO)) == 0
    assert si.main(["--bogus"], root=str(REPO)) == 2
    # soft > hard → invalid
    assert si.main(["--soft-cap-hours", "9", "--hard-cap-hours", "2"], root=str(REPO)) == 2
    # hard > 24 → invalid
    assert si.main(["--soft-cap-hours", "1", "--hard-cap-hours", "30"], root=str(REPO)) == 2


# ── outcome-decision cascade ──

def test_decide_outcome():
    assert si.decide_outcome(1, 0, 0, 0, 0) == ("converged", "scanner-reported-convergence")
    assert si.decide_outcome(0, 124, 1, 0, 0)[0] == "timed_out"
    assert si.decide_outcome(0, 3, 1, 1, 1)[0] == "rejected"          # exec_rc≠0 beats verifiers
    assert si.decide_outcome(0, 0, 1, 1, 1) == ("success", "all-verifiers-pass")
    assert si.decide_outcome(0, 0, 1, 0, 1)[0] == "rejected"          # patch-failed-verifier
    assert si.decide_outcome(0, 0, 0, 0, 0) == ("failed", "planner-or-synth-failed")
    # backstop: task_runs.failed overrides a would-be success
    o, n = si.decide_outcome(0, 0, 1, 1, 1, tr_status="failed")
    assert o == "rejected" and "overridden-by-task_runs.failed" in n
    # backstop does NOT touch an already-negative outcome
    assert si.decide_outcome(0, 3, 0, 0, 0, tr_status="rolled_back")[0] == "rejected"


def test_seconds_to_hms():
    assert si.seconds_to_hms(3661) == "1h01m01s"
    assert si.seconds_to_hms(0) == "0h00m00s"


# ── DB-writing helpers ──

def _fresh_db(tmp, name):
    home = tmp / name; home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    mig = REPO / "db" / "migrations" / "0017_self_improve_learning.sql"
    if mig.is_file():
        subprocess.run(["sqlite3", db], stdin=open(mig), capture_output=True)
    return db


def _rows(db, sql):
    return subprocess.run(["sqlite3", db, sql], capture_output=True, text=True).stdout.strip()


_SYNTH = """# Synthesis

## Ranked patch plan

| Rank | Bottleneck | Category | Patch | Evidence | Confidence |
|------|-----------|----------|-------|----------|------------|
| 1 | slow dispatch | perf | cache lanes | lib/llm-dispatch.sh 2502.12345 | 0.9 |
| 2 | flaky test | correctness | pin seed | tests/x.py | 0.6 |
"""


def test_promote_synthesis(tmp_path):
    db_p = _fresh_db(tmp_path, "p")
    synth = tmp_path / "synthesis.md"; synth.write_text(_SYNTH)
    si.promote_synthesis_findings(db_p, "run-1", 5, str(synth))
    q = ("SELECT rank,category,title,confidence,evidence_paths,arxiv_refs "
         "FROM learning_record ORDER BY rank;")
    rows = _rows(db_p, q).splitlines()
    assert len(rows) == 2
    r1, r2 = rows
    assert r1.startswith("1|perf|slow dispatch|0.9|")
    assert "lib/llm-dispatch.sh" in r1 and "2502.12345" in r1
    assert r2.startswith("2|correctness|flaky test|0.6|")
    assert "tests/x.py" in r2


def test_record_run(tmp_path):
    db_p = _fresh_db(tmp_path, "p")
    si.record_run(db_p, "run-9", 2, "success", "all-pass", "/wt", "br", 100, 200)
    q = ("SELECT run_id,iter,worktree_path,branch_name,soft_deadline_at,hard_deadline_at,outcome,notes "
         "FROM self_improve_runs;")
    assert _rows(db_p, q) == "run-9|2|/wt|br|100|200|success|all-pass"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def test_restore_root_surface_leak(tmp_path):
    """A rejected iteration must revert files the dispatch leaked into the main
    checkout — but only those, never the operator's own in-flight work or files
    outside the improvable surface."""
    root = tmp_path / "repo"
    (root / "mini_ork").mkdir(parents=True)
    (root / "docs").mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    tracked = root / "mini_ork" / "keep.py"; tracked.write_text("v1\n")
    outside = root / "docs" / "note.md"; outside.write_text("d1\n")
    _git(root, "add", "-A"); _git(root, "commit", "-qm", "init")

    # operator already has an in-flight edit inside the surface before the run
    preexist = root / "mini_ork" / "wip.py"; preexist.write_text("in progress\n")
    pre = si._root_surface_dirty(str(root))
    assert "mini_ork/wip.py" in pre

    # the run leaks: mutate a tracked surface file, create a new untracked one,
    # and (out of scope) touch a file outside the surface
    tracked.write_text("v1\nLEAKED\n")
    leaked_new = root / "mini_ork" / "leaked_new.py"; leaked_new.write_text("leak\n")
    outside.write_text("d1\nalso changed but outside surface\n")

    restored = si._restore_root_surface_leak(str(root), pre)

    assert set(restored) == {"mini_ork/keep.py", "mini_ork/leaked_new.py"}
    assert tracked.read_text() == "v1\n"          # tracked leak reverted to HEAD
    assert not leaked_new.exists()                # untracked leak removed
    assert preexist.read_text() == "in progress\n"  # operator's WIP untouched
    assert "also changed" in outside.read_text()  # outside-surface change untouched


def test_record_success(tmp_path):
    db_p = _fresh_db(tmp_path, "p")
    # a pre-existing deferred row to be superseded
    subprocess.run(["sqlite3", db_p, "INSERT INTO learning_record "
                    "(run_id,iter,rank,category,title,outcome,severity,confidence,created_at,updated_at) "
                    "VALUES ('old',1,0,'meta','old row','deferred','low',0.5,1,1);"], capture_output=True)
    si.record_success(db_p, "run-7", 3, "self-improve/iter-3", "abc123def456")
    q = "SELECT run_id,iter,category,title,outcome FROM learning_record ORDER BY run_id,outcome;"
    got = _rows(db_p, q)
    assert "resolved" in got and "superseded" in got
    # the superseded row is the pre-existing deferred one
    assert "old|1|meta|old row|superseded" in got
    # the new resolved row references the iteration's branch
    assert "run-7|3|" in got and "resolved" in got
