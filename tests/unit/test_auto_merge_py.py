"""Parity gate: mini_ork.vcs.auto_merge vs lib/auto-merge.sh.

Builds a full scenario (repo with main + an APPROVE epic branch ahead, a real
state.db via db/init.sh, orch run dirs with verdict.json, kickoff with a Branch
marker) ONCE, copies it for the live-bash run and the port run, then compares
the merged main tree, epics status, runs verdict, branch deletion, and
merged/skipped/failed counts. Plus focused parity on branch-resolution, the
mutex, and untracked-stash. All git/DB in throwaway dirs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.vcs import auto_merge as am

SH = REPO / "lib" / "auto-merge.sh"
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


def _run_bash(root: Path):
    repo, home, orch, db = root / "repo", root / "home" / ".mini-ork", root / "orch", \
        str(root / "home" / ".mini-ork" / "state.db")
    script = f'. "{SH}" && mo_auto_merge'
    env = {**os.environ, **ENV, "MINI_ORK_ROOT": str(REPO), "REPO_ROOT": str(repo),
           "MINI_ORCH_DIR": str(orch), "JOB_ID": JOB, "MINI_ORK_HOME": str(home),
           "MINI_ORK_DB": db}
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    log = (orch / "runs" / JOB / "merge.log").read_text()
    m = re.search(r"merged=(\d+) skipped=(\d+) failed=(\d+)", log)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _snapshot(root: Path):
    repo = root / "repo"
    db = str(root / "home" / ".mini-ork" / "state.db")
    return {
        "main_tree": _g(repo, "rev-parse", "main^{tree}"),
        "epic_status": _sql(db, "SELECT status FROM epics WHERE id='epicOK';"),
        "run_verdict": _sql(db, "SELECT final_verdict FROM runs WHERE epic_id='epicOK';"),
        "branch_gone": _g(repo, "rev-parse", "--verify", "-q", "feat/ok", check=False) == "",
    }


def test_auto_merge_approve_and_skip_parity(tmp_path):
    _build(tmp_path / "src")
    rb, rp = tmp_path / "b", tmp_path / "p"
    shutil.copytree(tmp_path / "src", rb)
    shutil.copytree(tmp_path / "src", rp)

    counts_b = _run_bash(rb)
    counts_p = am.auto_merge(str(rp / "repo"), str(rp / "orch"), JOB,
                             mini_ork_home=str(rp / "home" / ".mini-ork"),
                             state_db=str(rp / "home" / ".mini-ork" / "state.db"),
                             now_iso="2026-07-05T00:00:00.000Z")
    assert counts_b == counts_p == (1, 1, 0)
    snap_b, snap_p = _snapshot(rb), _snapshot(rp)
    assert snap_b == snap_p
    assert snap_p["main_tree"] and snap_p["epic_status"] == "done"
    assert snap_p["run_verdict"] == "MERGED" and snap_p["branch_gone"] is True
    # merged main actually contains the feature file
    assert _g(rp / "repo", "cat-file", "-p", "main:feature.txt") == "the feature"


def test_resolve_branch_parity(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    (repo / "k.md").write_text("intro\n> **Branch:** `fix/some-thing_42`\nmore\n")
    out_b = subprocess.run(
        ["bash", "-c", f'. "{SH}"; grep -E "^>?[[:space:]]*\\*\\*Branch:\\*\\*" "{repo}/k.md" '
         '| head -1 | sed -E "s/^[^\\`]*\\`([^\\`]+)\\`.*/\\1/"'],
        capture_output=True, text=True).stdout.strip()
    assert out_b == am._resolve_branch(str(repo), "k.md") == "fix/some-thing_42"
    # no branch marker → empty on both
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


def test_stash_colliding_untracked_parity(tmp_path):
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

    rb, rp = tmp_path / "b", tmp_path / "p"
    shutil.copytree(src, rb); shutil.copytree(src, rp)
    hb, hp = str(tmp_path / "hb"), str(tmp_path / "hp")

    out_b = subprocess.run(
        ["bash", "-c", f'. "{SH}"; REPO_ROOT="{rb}"; JOB_ID="{JOB}"; MINI_ORK_HOME="{hb}"; '
         '_mo_stash_colliding_untracked "feat/x" "epicX" "/dev/null"'],
        capture_output=True, text=True, env={**os.environ, **ENV}).returncode
    moved_p = am.stash_colliding_untracked(str(rp), "feat/x", "epicX", JOB, hp, "20260101-000000")
    assert out_b == 0 and moved_p == 1
    # both: new.txt moved out of the working tree
    assert not (rp / "new.txt").exists()
    assert not (rb / "new.txt").exists()
