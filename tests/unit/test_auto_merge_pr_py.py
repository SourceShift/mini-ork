"""Parity gate: mini_ork.vcs.auto_merge_pr vs lib/auto-merge-pr.sh.

A configurable fake `gh` on PATH (checks/review/createdAt/merge responses via
env) makes the gate ladder deterministic and offline. Compares
mo_auto_merge_pr_one's rc + resulting epics.status across the happy path,
failing/pending checks, not-approved, not-soaked, disabled gate, and the sweep.
"""
from __future__ import annotations

import datetime
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.vcs import auto_merge_pr as amp

SH = REPO / "lib" / "auto-merge-pr.sh"


def _sql(db, stmt):
    return subprocess.run(["sqlite3", db, stmt], capture_output=True, text=True).stdout.strip()


def _fake_gh(bindir: Path):
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(
        '#!/usr/bin/env bash\n'
        'case "$1 $2" in\n'
        '  "auth status") exit 0;;\n'
        '  "pr checks") printf "%s\\n" "$GH_CHECKS"; exit 0;;\n'
        '  "pr view")\n'
        '    case "$*" in\n'
        '      *reviewDecision*) printf "%s\\n" "$GH_REVIEW";;\n'
        '      *createdAt*) printf "%s\\n" "$GH_CREATED";;\n'
        '    esac; exit 0;;\n'
        '  "pr merge") exit "${GH_MERGE_RC:-0}";;\n'
        'esac\nexit 0\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def db(tmp_path):
    home = tmp_path / ".mini-ork"; home.mkdir()
    d = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": d},
                   capture_output=True, text=True, check=True)
    return d


@pytest.fixture
def ghbin(tmp_path):
    b = tmp_path / "bin"; _fake_gh(b)
    return str(b)


def _seed(db, epic="e1", pr_url="https://github.com/o/r/pull/1", status="in review"):
    _sql(db, f"INSERT INTO epics (id,title,status,pr_url) VALUES ('{epic}','T','{status}','{pr_url}');")


SOAKED = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)) \
    .strftime("%Y-%m-%dT%H:%M:%SZ")
FRESH = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)) \
    .strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_both(db_b, db_p, epic, ghbin, gh_env):
    """Run bash + port under identical fake-gh env. Returns (rc_b, rc_p)."""
    benv = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db_b,
            "MO_AUTO_MERGE": "1", "PATH": f"{ghbin}:{os.environ['PATH']}", **gh_env}
    rc_b = subprocess.run(["bash", "-c", f'. "{SH}" && mo_auto_merge_pr_one "{epic}"'],
                          capture_output=True, text=True, env=benv).returncode
    saved = {k: os.environ.get(k) for k in ("PATH", "MO_AUTO_MERGE", "MINI_ORK_DB", *gh_env)}
    os.environ["PATH"] = f"{ghbin}:{os.environ['PATH']}"
    os.environ["MO_AUTO_MERGE"] = "1"
    os.environ.update(gh_env)
    try:
        rc_p = amp.auto_merge_pr_one(epic, state_db=db_p)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc_b, rc_p


@pytest.mark.parametrize("gh_env,exp_rc,exp_status", [
    ({"GH_CHECKS": "pass", "GH_REVIEW": "APPROVED", "GH_CREATED": SOAKED}, 0, "done"),
    ({"GH_CHECKS": "fail", "GH_REVIEW": "APPROVED", "GH_CREATED": SOAKED}, 1, "in review"),
    ({"GH_CHECKS": "pending", "GH_REVIEW": "APPROVED", "GH_CREATED": SOAKED}, 2, "in review"),
    ({"GH_CHECKS": "pass", "GH_REVIEW": "CHANGES_REQUESTED", "GH_CREATED": SOAKED}, 2, "in review"),
    ({"GH_CHECKS": "pass", "GH_REVIEW": "APPROVED", "GH_CREATED": FRESH}, 2, "in review"),
    ({"GH_CHECKS": "", "GH_REVIEW": "APPROVED", "GH_CREATED": SOAKED}, 0, "done"),  # no checks → pass
])
def test_auto_merge_pr_one_parity(tmp_path, ghbin, gh_env, exp_rc, exp_status):
    # two DBs (identical seed) — offline, gh mocked
    dbs = []
    for side in ("b", "p"):
        home = tmp_path / side / ".mini-ork"; home.mkdir(parents=True)
        d = str(home / "state.db")
        subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                       env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": d},
                       capture_output=True, text=True, check=True)
        _seed(d)
        dbs.append(d)
    rc_b, rc_p = _run_both(dbs[0], dbs[1], "e1", ghbin, gh_env)
    assert rc_b == rc_p == exp_rc
    assert _sql(dbs[0], "SELECT status FROM epics WHERE id='e1';") == \
        _sql(dbs[1], "SELECT status FROM epics WHERE id='e1';") == exp_status


def test_disabled_gate_and_no_pr(db, ghbin):
    _seed(db, epic="e2", pr_url="NULL")  # note: pr_url literally NULL string won't match IS NOT NULL
    # MO_AUTO_MERGE unset → rc 2 on both
    rc_b = subprocess.run(["bash", "-c", f'. "{SH}" && mo_auto_merge_pr_one e1'],
                          capture_output=True, text=True,
                          env={**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db}).returncode
    assert rc_b == 2
    assert amp.auto_merge_pr_one("e1", state_db=db) == 2  # MO_AUTO_MERGE unset in this env


def test_sweep_parity(tmp_path, ghbin):
    dbs = []
    for side in ("b", "p"):
        home = tmp_path / side / ".mini-ork"; home.mkdir(parents=True)
        d = str(home / "state.db")
        subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                       env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": d},
                       capture_output=True, text=True, check=True)
        _seed(d, epic="ok", pr_url="https://github.com/o/r/pull/1", status="in review")
        _seed(d, epic="fresh", pr_url="https://github.com/o/r/pull/2", status="in review")
        dbs.append(d)
    # both PRs: one soaked+approved (merges), we can't per-PR differ via env, so use
    # all-mergeable → both merge; assert merged/skipped counts match across bash/port.
    env = {"GH_CHECKS": "pass", "GH_REVIEW": "APPROVED", "GH_CREATED": SOAKED,
           "MO_AUTO_MERGE": "1", "PATH": f"{ghbin}:{os.environ['PATH']}",
           "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": dbs[0]}
    out_b = subprocess.run(["bash", "-c", f'. "{SH}" && mo_auto_merge_pr_sweep'],
                           capture_output=True, text=True, env={**os.environ, **env}).stdout
    import re
    m = re.search(r"swept (\d+) merged, (\d+) skipped", out_b)
    saved_path = os.environ["PATH"]
    os.environ["PATH"] = f"{ghbin}:{os.environ['PATH']}"
    os.environ.update({"MO_AUTO_MERGE": "1", "GH_CHECKS": "pass", "GH_REVIEW": "APPROVED",
                       "GH_CREATED": SOAKED})
    try:
        merged_p, skipped_p = amp.auto_merge_pr_sweep(dbs[1])
    finally:
        os.environ["PATH"] = saved_path
        for k in ("MO_AUTO_MERGE", "GH_CHECKS", "GH_REVIEW", "GH_CREATED"):
            os.environ.pop(k, None)
    assert (int(m[1]), int(m[2])) == (merged_p, skipped_p) == (2, 0)
