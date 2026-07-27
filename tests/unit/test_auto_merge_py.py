"""Unit tests: mini_ork.vcs.auto_merge (bash parity halves removed; formerly vs lib/auto-merge.sh).

Builds a full scenario (repo with main + an APPROVE epic branch ahead, a real
state.db via db/init.sh, orch run dirs with verdict.json, kickoff with a
Branch marker), runs the port, then asserts the merged main tree, epics
status, runs verdict, branch deletion, and merged/skipped/failed counts.
Plus focused tests on branch-resolution, the mutex, and untracked-stash.
All git/DB in throwaway dirs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.vcs import auto_merge as am

JOB = "job-test-1"
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


def _g(cwd, *args, date=None, check=True):
    env = {**os.environ, **ENV}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def _sql(db, stmt):
    return subprocess.run(["sqlite3", db, stmt], capture_output=True, text=True).stdout.strip()


def _build(root: Path):
    root.mkdir(parents=True)
    repo = root / "repo"; repo.mkdir()
    home = root / "home" / ".mini-ork"; home.mkdir(parents=True)
    orch = root / "orch"
    db = str(home / "state.db")

    # real schema
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)

    # repo: base commit incl. kickoff, then an APPROVE epic branch ahead of main
    _g(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    (repo / "kickoffs").mkdir()
    (repo / "kickoffs" / "epicOK.md").write_text("# Epic OK\n**Branch:** `feat/ok`\n")
    _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "base", date="2026-01-01T00:00:00Z")
    _g(repo, "checkout", "-q", "-b", "feat/ok")
    (repo / "feature.txt").write_text("the feature\n")
    _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "feat: add feature", date="2026-02-01T00:00:00Z")
    _g(repo, "checkout", "-q", "main")

    # epic row for the APPROVE epic
    _sql(db, "INSERT INTO epics (id,title,status,lane,worker_default,group_id,kickoff_path) "
             "VALUES ('epicOK','Epic OK','in progress','mini-ork','mini-ork','g1','kickoffs/epicOK.md');")

    # orch run dirs + verdicts
    for epic, verdict in (("epicOK", "APPROVE"), ("epicNO", "REQUEST_CHANGES")):
        d = orch / "runs" / JOB / epic / "iter-1"
        d.mkdir(parents=True)
        (d / "verdict.json").write_text(json.dumps({"verdict": verdict}))
    return repo, home, orch, db


def _snapshot(root: Path):
    repo = root / "repo"
    db = str(root / "home" / ".mini-ork" / "state.db")
    return {
        "main_tree": _g(repo, "rev-parse", "main^{tree}"),
        "epic_status": _sql(db, "SELECT status FROM epics WHERE id='epicOK';"),
        "run_verdict": _sql(db, "SELECT final_verdict FROM runs WHERE epic_id='epicOK';"),
        "branch_gone": _g(repo, "rev-parse", "--verify", "-q", "feat/ok", check=False) == "",
    }


def test_auto_merge_approve_and_skip(tmp_path):
    _build(tmp_path / "p")
    rp = tmp_path / "p"

    counts_p = am.auto_merge(str(rp / "repo"), str(rp / "orch"), JOB,
                             mini_ork_home=str(rp / "home" / ".mini-ork"),
                             state_db=str(rp / "home" / ".mini-ork" / "state.db"),
                             now_iso="2026-07-05T00:00:00.000Z")
    assert counts_p == (1, 1, 0)
    snap_p = _snapshot(rp)
    assert snap_p["main_tree"] and snap_p["epic_status"] == "done"
    assert snap_p["run_verdict"] == "MERGED" and snap_p["branch_gone"] is True
    # merged main actually contains the feature file
    assert _g(rp / "repo", "cat-file", "-p", "main:feature.txt") == "the feature"


def test_resolve_branch(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    (repo / "k.md").write_text("intro\n> **Branch:** `fix/some-thing_42`\nmore\n")
    assert am._resolve_branch(str(repo), "k.md") == "fix/some-thing_42"
    # no branch marker → empty
    (repo / "none.md").write_text("no marker here\n")
    assert am._resolve_branch(str(repo), "none.md") == ""


def test_mutex_acquire_release(tmp_path):
    home = str(tmp_path / ".mini-ork")
    assert am.acquire_main_mutex(home, JOB) is True
    lock = tmp_path / ".mini-ork" / "locks" / "main-merge.lock"
    assert lock.is_dir() and (lock / "pid").read_text().strip() == str(os.getpid())
    am.release_main_mutex(home)
    assert not lock.exists()
    # re-acquire after release
    assert am.acquire_main_mutex(home, JOB) is True
    am.release_main_mutex(home)


def test_stash_colliding_untracked(tmp_path):
    # build repo where feat branch ADDS a file that main has untracked
    src = tmp_path / "src"; src.mkdir()
    _g(src, "init", "-q", "-b", "main")
    (src / "base.txt").write_text("b\n"); _g(src, "add", "-A"); _g(src, "commit", "-qm", "base")
    _g(src, "checkout", "-q", "-b", "feat/x")
    (src / "new.txt").write_text("branch version\n")
    _g(src, "add", "-A"); _g(src, "commit", "-qm", "add new.txt")
    _g(src, "checkout", "-q", "main")
    # main has new.txt UNTRACKED (collides with branch's added file)
    (src / "new.txt").write_text("untracked on main\n")

    hp = str(tmp_path / "hp")
    moved_p = am.stash_colliding_untracked(str(src), "feat/x", "epicX", JOB, hp, "20260101-000000")
    assert moved_p == 1
    # new.txt moved out of the working tree
    assert not (src / "new.txt").exists()
