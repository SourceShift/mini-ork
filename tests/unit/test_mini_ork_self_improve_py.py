"""Parity gate: mini_ork.ported.mini_ork_self_improve vs bin/mini-ork-self-improve.

The outer loop creates git worktrees + dispatches LLM runs — integration. Here we
parity the deterministic surface: early-exit flag handling vs live bash, the
outcome-decision cascade (iter-33/34 bug-prone), and the three embedded-DB-python
blocks EXTRACTED from the bash and run as-is vs the ported functions (DB rows
compared).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_self_improve as si  # noqa: E402

BIN = REPO / "bin" / "mini-ork-self-improve"


# ── early-exit flag parity ──

def _bash(*args, tmp):
    return subprocess.run(["bash", str(BIN), *args], capture_output=True, text=True,
                          env={**os.environ, "MINI_ORK_ROOT": str(REPO),
                               "MINI_ORK_HOME": str(tmp / ".mini-ork")})


def test_help_unknown_badcaps_parity(tmp_path):
    assert _bash("--help", tmp=tmp_path).returncode == si.main(["--help"], root=str(REPO)) == 0
    assert _bash("--bogus", tmp=tmp_path).returncode == si.main(["--bogus"], root=str(REPO)) == 2
    # soft > hard → invalid
    rb = _bash("--soft-cap-hours", "9", "--hard-cap-hours", "2", tmp=tmp_path).returncode
    assert rb == si.main(["--soft-cap-hours", "9", "--hard-cap-hours", "2"], root=str(REPO)) == 2
    # hard > 24 → invalid
    rb2 = _bash("--soft-cap-hours", "1", "--hard-cap-hours", "30", tmp=tmp_path).returncode
    assert rb2 == si.main(["--soft-cap-hours", "1", "--hard-cap-hours", "30"], root=str(REPO)) == 2


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


# ── embedded-DB-python block parity ──

def _extract_blocks():
    src = BIN.read_text().splitlines()
    blocks, cur = [], None
    for ln in src:
        if cur is None and re.search(r"<<'PY'", ln):
            cur = []
        elif cur is not None and ln.strip() == "PY":
            blocks.append("\n".join(cur)); cur = None
        elif cur is not None:
            cur.append(ln)
    promote = next(b for b in blocks if "table_rows" in b and "learning_record" in b)
    success = next(b for b in blocks if "json_array" in b and "superseded" in b)
    recrun = next(b for b in blocks if "self_improve_runs" in b and "ON CONFLICT" in b)
    return promote, success, recrun


_PROMOTE, _SUCCESS, _RECRUN = _extract_blocks()


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


def _run_block(block, *args, tmp_path):
    p = tmp_path / "blk.py"; p.write_text(block)
    subprocess.run(["python3", str(p), *map(str, args)], capture_output=True, text=True)


def _rows(db, sql):
    return subprocess.run(["sqlite3", db, sql], capture_output=True, text=True).stdout.strip()


_SYNTH = """# Synthesis

## Ranked patch plan

| Rank | Bottleneck | Category | Patch | Evidence | Confidence |
|------|-----------|----------|-------|----------|------------|
| 1 | slow dispatch | perf | cache lanes | lib/llm-dispatch.sh 2502.12345 | 0.9 |
| 2 | flaky test | correctness | pin seed | tests/x.py | 0.6 |
"""


def test_promote_synthesis_parity(tmp_path):
    db_b, db_p = _fresh_db(tmp_path, "b"), _fresh_db(tmp_path, "p")
    synth = tmp_path / "synthesis.md"; synth.write_text(_SYNTH)
    _run_block(_PROMOTE, db_b, "run-1", "5", str(synth), tmp_path=tmp_path)
    si.promote_synthesis_findings(db_p, "run-1", 5, str(synth))
    q = ("SELECT rank,category,title,severity,confidence,evidence_paths,arxiv_refs "
         "FROM learning_record ORDER BY rank;")
    assert _rows(db_b, q) == _rows(db_p, q) and _rows(db_p, q)


def test_record_run_parity(tmp_path):
    db_b, db_p = _fresh_db(tmp_path, "b"), _fresh_db(tmp_path, "p")
    _run_block(_RECRUN, db_b, "run-9", "2", "success", "all-pass", "/wt", "br", "100", "200", tmp_path=tmp_path)
    si.record_run(db_p, "run-9", 2, "success", "all-pass", "/wt", "br", 100, 200)
    q = ("SELECT run_id,iter,worktree_path,branch_name,soft_deadline_at,hard_deadline_at,outcome,notes "
         "FROM self_improve_runs;")
    assert _rows(db_b, q) == _rows(db_p, q) and _rows(db_p, q)


def test_record_success_parity(tmp_path):
    db_b, db_p = _fresh_db(tmp_path, "b"), _fresh_db(tmp_path, "p")
    for db in (db_b, db_p):   # a pre-existing deferred row to be superseded
        subprocess.run(["sqlite3", db, "INSERT INTO learning_record "
                        "(run_id,iter,rank,category,title,outcome,severity,confidence,created_at,updated_at) "
                        "VALUES ('old',1,0,'meta','old row','deferred','low',0.5,1,1);"], capture_output=True)
    _run_block(_SUCCESS, db_b, "run-7", "3", "self-improve/iter-3", "abc123def456", tmp_path=tmp_path)
    si.record_success(db_p, "run-7", 3, "self-improve/iter-3", "abc123def456")
    q = "SELECT run_id,iter,category,title,outcome,severity,confidence FROM learning_record ORDER BY run_id,outcome;"
    assert _rows(db_b, q) == _rows(db_p, q) and "resolved" in _rows(db_p, q) and "superseded" in _rows(db_p, q)
