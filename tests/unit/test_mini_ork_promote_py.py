"""Parity gate: mini_ork.ported.mini_ork_promote vs bin/mini-ork-promote.

The decision logic lives in promotion_gate (already parity-tested), so this
gate tests the CLI's own surface: --help, the candidate-status preflight gates
(not-found / quarantined / already-promoted / not-evaluated-without-force), and
a dry-run end-to-end where bash and the port must agree on the decision + rc +
leave the DB untouched. All against a seeded temp state.db.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_promote as promo  # noqa: E402

BIN = REPO / "bin" / "mini-ork-promote"


def _sql(db, stmt):
    return subprocess.run(["sqlite3", db, stmt], capture_output=True, text=True).stdout.strip()


@pytest.fixture
def db(tmp_path):
    home = tmp_path / ".mini-ork"; home.mkdir()
    d = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": d},
                   capture_output=True, text=True, check=True)
    _sql(d, "INSERT INTO workflow_memory (workflow_version_id, workflow_name, yaml_hash, status) "
            "VALUES ('base-v1','code-fix','h','stable');")
    return d


def _seed_candidate(db, cid, status, delta=0.0):
    _sql(db, f"INSERT INTO workflow_candidates (candidate_id, base_workflow_version_id, status, "
             f"utility_delta, created_by) VALUES ('{cid}','base-v1','{status}',{delta},'evolution_engine');")


def _bash(db, *args, env_extra=None):
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db,
           "MINI_ORK_HOME": os.path.dirname(db)}
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(["bash", str(BIN), *args], capture_output=True, text=True, env=env)
    return r.stdout, r.stderr, r.returncode


def _py(db, *args):
    import io
    from contextlib import redirect_stdout, redirect_stderr
    o, e = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    os.environ.pop("MINI_ORK_PROMOTE_FORCE", None); os.environ.pop("MINI_ORK_DRY_RUN", None)
    try:
        with redirect_stdout(o), redirect_stderr(e):
            rc = promo.main(list(args), db=db, root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    return o.getvalue(), e.getvalue(), rc


def test_help_parity():
    ob, _, rb = _bash("/dev/null", "--help") if False else _bash(":memory:", "--help")
    op, _, rp = _py(":memory:", "--help")
    assert rb == rp == 0 and ob == op


@pytest.mark.parametrize("status,args,exp_rc", [
    (None, ["--candidate", "ghost"], 2),          # not found
    ("quarantined", ["--candidate", "q1"], 2),    # quarantined
    ("promoted", ["--candidate", "p1"], 0),       # already promoted
    ("candidate", ["--candidate", "c1"], 2),      # not evaluated, no --force
])
def test_preflight_gate_parity(db, status, args, exp_rc):
    cid = args[1]
    if status is not None:
        _seed_candidate(db, cid, status)
    rb = _bash(db, *args)[2]
    rp = _py(db, *args)[2]
    assert rb == rp == exp_rc


def test_no_candidate_arg_is_usage_error(db):
    assert _bash(db)[2] == _py(db)[2] == 2


def test_dry_run_decision_parity(db):
    _seed_candidate(db, "s1", "shadow", delta=0.3)
    ob, _, rb = _bash(db, "--candidate", "s1", "--dry-run")
    op, _, rp = _py(db, "--candidate", "s1", "--dry-run")
    assert rb == rp == 0
    db_dec = re.search(r"\[dry-run\] decision=(\w+)", ob)
    py_dec = re.search(r"\[dry-run\] decision=(\w+)", op)
    assert db_dec and py_dec and db_dec.group(1) == py_dec.group(1)   # same gate decision
    # dry-run leaves the candidate untouched on both
    assert _sql(db, "SELECT status FROM workflow_candidates WHERE candidate_id='s1';") == "shadow"
