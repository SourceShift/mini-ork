"""Parity gate: mini_ork.ported.rebase_guard vs lib/rebase-guard.sh.

Builds IDENTICAL temp git scenarios (repo + worktree branch, main advanced with
/ without overlap / with a conflict) for the live bash function and the Python
port, then compares return code + rebase-decision.json + the resulting worktree
tree. All git operations happen inside throwaway temp repos — nothing touches
the real repository.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import rebase_guard as rg  # noqa: E402

SH = REPO / "lib" / "rebase-guard.sh"
_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


def _g(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **_ENV})
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def _scenario(root: Path, kind: str):
    """Build repo + worktree where main has advanced. Returns (repo, wt, run_dir)."""
    repo = root / "repo"; repo.mkdir(parents=True)
    _g(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    (repo / "shared.txt").write_text("l1\nl2\nl3\nl4\nl5\n")
    _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "init")

    wt = root / "wt"
    _g(repo, "worktree", "add", "-q", "-b", "feat", str(wt), "main")

    if kind == "fresh":
        return repo, wt, str(root / "run")

    if kind == "dirty":
        (wt / "shared.txt").write_text("dirty edit\nl2\nl3\nl4\nl5\n")  # uncommitted tracked
        # still advance main so we'd otherwise rebase
        (repo / "main_only.txt").write_text("x\n"); _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "m")
        return repo, wt, str(root / "run")

    # branch commit
    if kind == "conflict":
        (wt / "shared.txt").write_text("BRANCH\nl2\nl3\nl4\nl5\n")       # top line
    elif kind == "overlap_clean":
        (wt / "shared.txt").write_text("l1\nl2\nl3\nl4\nBRANCH5\n")      # bottom line
    else:  # no_overlap
        (wt / "branch.txt").write_text("branch work\n")
    _g(wt, "add", "-A"); _g(wt, "commit", "-qm", "branch work")

    # advance main
    if kind == "no_overlap":
        (repo / "main_only.txt").write_text("main work\n")               # disjoint file
    elif kind == "overlap_clean":
        (repo / "shared.txt").write_text("MAIN1\nl2\nl3\nl4\nl5\n")      # top line — merges w/ branch's bottom
    elif kind == "conflict":
        (repo / "shared.txt").write_text("MAIN\nl2\nl3\nl4\nl5\n")       # same top line as branch → conflict
    _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "main advance")
    return repo, wt, str(root / "run")


def _bash_run(repo, wt, run_dir, env_extra=None):
    run_dir_q = run_dir.replace("'", "'\\''")
    script = (f'. "{SH}"; mo_run_dir() {{ echo "{run_dir_q}"; }}; '
              f'REPO_ROOT="{repo}"; export REPO_ROOT; '
              f'mo_rebase_branch_onto_main "epic1" "{wt}" "dispatch"')
    env = {**os.environ, **_ENV, "MINI_ORK_ROOT": str(REPO)}
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    return r.returncode


def _decision(run_dir):
    p = Path(run_dir) / "rebase-decision.json"
    return json.loads(p.read_text()) if p.exists() else None


def _tree(wt):
    return _g(wt, "rev-parse", "HEAD^{tree}")


@pytest.mark.parametrize("kind,exp_decision,exp_rc", [
    ("no_overlap", "no_overlap_auto", 0),
    ("overlap_clean", "overlap_attempted", 0),
    ("conflict", "conflict_aborted", 1),
])
def test_rebase_parity(tmp_path, kind, exp_decision, exp_rc):
    rb, wb, runb = _scenario(tmp_path / "b", kind)
    rp, wp, runp = _scenario(tmp_path / "p", kind)
    rc_b = _bash_run(rb, wb, runb)
    rc_p = rg.rebase_branch_onto_main("epic1", str(wp), "dispatch", repo_root=str(rp), run_dir=runp)
    assert rc_b == rc_p == exp_rc
    db, dp = _decision(runb), _decision(runp)
    assert db is not None and dp is not None
    assert db == dp
    assert db["decision"] == exp_decision
    # resulting worktree tree content identical (SHAs of commits differ, tree matches)
    assert _tree(wb) == _tree(wp)


def test_fresh_branch_no_rebase(tmp_path):
    rb, wb, runb = _scenario(tmp_path / "b", "fresh")
    rp, wp, runp = _scenario(tmp_path / "p", "fresh")
    rc_b = _bash_run(rb, wb, runb)
    rc_p = rg.rebase_branch_onto_main("e", str(wp), repo_root=str(rp), run_dir=runp)
    assert rc_b == rc_p == 0
    assert _decision(runb) is None and _decision(runp) is None


def test_dirty_worktree_skips(tmp_path):
    rb, wb, runb = _scenario(tmp_path / "b", "dirty")
    rp, wp, runp = _scenario(tmp_path / "p", "dirty")
    assert _bash_run(rb, wb, runb) == rg.rebase_branch_onto_main(
        "e", str(wp), repo_root=str(rp), run_dir=runp) == 2


def test_skip_autorebase_env(tmp_path):
    rb, wb, runb = _scenario(tmp_path / "b", "no_overlap")
    rp, wp, runp = _scenario(tmp_path / "p", "no_overlap")
    rc_b = _bash_run(rb, wb, runb, env_extra={"MO_SKIP_AUTOREBASE": "1"})
    os.environ["MO_SKIP_AUTOREBASE"] = "1"
    try:
        rc_p = rg.rebase_branch_onto_main("e", str(wp), repo_root=str(rp), run_dir=runp)
    finally:
        del os.environ["MO_SKIP_AUTOREBASE"]
    assert rc_b == rc_p == 0


def test_stale_base_marker_parity(tmp_path):
    runb, runp = tmp_path / "b", tmp_path / "p"
    script = (f'. "{SH}"; mo_run_dir() {{ echo "{runb}"; }}; '
              f'mo_write_stale_base_marker "epicX" "2"')
    subprocess.run(["bash", "-c", script], env={**os.environ, **_ENV, "MINI_ORK_ROOT": str(REPO)},
                   capture_output=True, text=True)
    rg.write_stale_base_marker("epicX", "2", run_dir=str(runp))
    bnote = (runb / "iter-2" / "stale-base.note").read_text()
    pnote = (runp / "iter-2" / "stale-base.note").read_text()
    assert bnote == pnote
