"""Parity gate: mini_ork.recovery.cleaner vs lib/cleaner.sh.

The claude-worker + gauntlet paths are non-deterministic seams (LLM spawn) and
are an integration concern. Here we parity-test every branch that reaches a
verdict WITHOUT spawning claude — usage, no-brief, not-on-main, lock-held, and
the reuse fast-path (pure git) — against the LIVE bash in throwaway repos.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.recovery import cleaner

BIN = REPO / "lib" / "cleaner.sh"


def _repo(tmp, name, *, on_main=True, reuse_branch=False):
    r = tmp / name; r.mkdir()
    def g(*a, **k):
        return subprocess.run(["git", *a], cwd=r, capture_output=True, text=True, **k)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t.co"); g("config", "user.name", "t")
    (r / "f.txt").write_text("base\n")
    g("add", "."); g("commit", "-qm", "init")
    if reuse_branch:
        g("checkout", "-q", "-b", "chore/cleaner-baseline_rot-20200101T000000Z")
        (r / "fix.txt").write_text("cleaner fix\n")
        g("add", "."); g("commit", "-qm", "fix(baseline): rot")
        g("checkout", "-q", "main")
    if not on_main:
        g("checkout", "-q", "-b", "feature")
    return r


def _det(r, brief="fix the thing"):
    p = r / "det.json"
    p.write_text(json.dumps({"epic_id": "e9", "classification": "baseline_rot",
                             "cleaner_brief": brief, "rationale": "rot detected"}))
    return str(p)


def _run_bash(r, *args):
    env = {**os.environ, "MINI_ORK_ROOT": str(r), "REPO_ROOT": str(r),
           "MINI_ORK_HOME": str(r / ".mini-ork")}
    return subprocess.run(["bash", str(BIN), *args], cwd=r, capture_output=True, text=True, env=env)


def _run_py(r, *args):
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_ROOT": str(r), "REPO_ROOT": str(r),
                       "MINI_ORK_HOME": str(r / ".mini-ork")})
    cwd = os.getcwd(); os.chdir(r)
    try:
        return cleaner.main(list(args), root=str(r), home=str(r / ".mini-ork"))
    finally:
        os.chdir(cwd); os.environ.clear(); os.environ.update(old)


def test_usage_missing_args(tmp_path):
    rb, rp = _repo(tmp_path, "b"), _repo(tmp_path, "p")
    assert _run_bash(rb).returncode == _run_py(rp) == 2


def test_no_brief(tmp_path):
    rb, rp = _repo(tmp_path, "b"), _repo(tmp_path, "p")
    db = _det(rb, brief=""); dp = _det(rp, brief="")
    assert _run_bash(rb, db, str(rb / "out")).returncode == 4
    assert _run_py(rp, dp, str(rp / "out")) == 4


def test_not_on_main(tmp_path):
    rb = _repo(tmp_path, "b", on_main=False); rp = _repo(tmp_path, "p", on_main=False)
    assert _run_bash(rb, _det(rb), str(rb / "out")).returncode == 5
    assert _run_py(rp, _det(rp), str(rp / "out")) == 5


def test_lock_held(tmp_path):
    rb, rp = _repo(tmp_path, "b"), _repo(tmp_path, "p")
    for r in (rb, rp):
        ld = r / ".mini-ork" / "locks" / "cleaner.lock.d"; ld.mkdir(parents=True)
        (ld / "pid").write_text(f"{os.getpid()}\n")   # a live PID → not stale
    assert _run_bash(rb, _det(rb), str(rb / "out")).returncode == 3
    assert _run_py(rp, _det(rp), str(rp / "out")) == 3


def test_reuse_fast_path(tmp_path):
    rb = _repo(tmp_path, "b", reuse_branch=True); rp = _repo(tmp_path, "p", reuse_branch=True)
    cb = _run_bash(rb, _det(rb), str(rb / "out"))
    cp = _run_py(rp, _det(rp), str(rp / "out"))
    assert cb.returncode == cp == 0, cb.stderr
    for r in (rb, rp):
        v = json.load(open(r / "out" / "verdict.json"))
        assert v["verdict"] == "PASS" and v["fast_path"] == "reuse" and v["squashed_commits"] == 1
        # main now carries the cleaner branch's file
        assert (r / "fix.txt").exists()
        log = subprocess.run(["git", "log", "--oneline", "main"], cwd=r,
                             capture_output=True, text=True).stdout
        assert "reuse cleaner branch" in log
