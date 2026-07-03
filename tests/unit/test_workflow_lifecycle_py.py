"""Parity gate: mini_ork.ported.workflow_lifecycle vs lib/workflow_lifecycle.sh.

Same operations through LIVE bash and the Python port on separate DB copies of
the real schema, against a real recipe (code-fix); resulting workflow_memory /
workflow_candidates rows must match.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import workflow_lifecycle as wl  # noqa: E402

WL_SH = REPO / "lib" / "workflow_lifecycle.sh"


def _bash(db, fn, *args):
    r = subprocess.run(
        ["bash", "-c", f'. "{WL_SH}" && {fn} "$@"', "_", *args],
        env={**os.environ, "MINI_ORK_DB": db, "MINI_ORK_ROOT": str(REPO)},
        capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


@pytest.fixture
def dbs(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    base = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": base},
                   capture_output=True, text=True, check=True)
    import shutil
    b, p = base + ".bash", base + ".py"
    shutil.copy(base, b)
    shutil.copy(base, p)
    return b, p


def _rows(db, sql):
    con = sqlite3.connect(db)
    rows = con.execute(sql).fetchall()
    con.close()
    return rows


def test_ensure_baseline_parity_and_idempotence(dbs):
    db_bash, db_py = dbs
    out_bash, rc = _bash(db_bash, "workflow_memory_ensure_baseline", "code-fix")
    vid_py = wl.ensure_baseline("code-fix", db=db_py, root=str(REPO))
    assert rc == 0 and out_bash == vid_py == "code-fix_v0.1.0"
    # Idempotent second call
    assert wl.ensure_baseline("code-fix", db=db_py, root=str(REPO)) == vid_py
    q = ("SELECT workflow_version_id, workflow_name, yaml_hash, status "
         "FROM workflow_memory")
    assert _rows(db_bash, q) == _rows(db_py, q)


def test_candidate_store_parity(dbs):
    db_bash, db_py = dbs
    cand = json.dumps({"task_class": "code_fix", "candidate_id": "wc-parity1",
                       "mutation_applied": {"op": "swap_lane", "node": "reviewer"}})
    out_bash, rc = _bash(db_bash, "workflow_candidate_store", cand)
    cid_py = wl.candidate_store(cand, db=db_py, root=str(REPO))
    assert rc == 0 and out_bash == cid_py == "wc-parity1"
    q = ("SELECT candidate_id, base_workflow_version_id, mutations, status, "
         "utility_delta, created_by FROM workflow_candidates")
    assert _rows(db_bash, q) == _rows(db_py, q)
    # underscore->hyphen recipe resolution created the baseline FK row
    assert _rows(db_py, "SELECT workflow_version_id FROM workflow_memory") == \
        [("code-fix_v0.1.0",)]


def test_missing_recipe_errors(dbs):
    _, db_py = dbs
    with pytest.raises(FileNotFoundError):
        wl.ensure_baseline("no-such-recipe", db=db_py, root=str(REPO))
    with pytest.raises(FileNotFoundError):
        wl.candidate_store({"task_class": "no_such_recipe"}, db=db_py, root=str(REPO))
