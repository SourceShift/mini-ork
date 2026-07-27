"""Unit tests: mini_ork.vcs.branch_quarantine (bash parity halves removed; formerly vs lib/branch-quarantine.sh).

Builds temp git repos (worktree branch clean / contaminated with auto-revert
commits / at merge-base / dirty) and asserts detect counts, reset return codes,
resulting branch tip, the preserved quarantine ref, and the audit JSON. All git
ops happen in throwaway repos.
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
from mini_ork.vcs import branch_quarantine as bq

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
_AR = "chore(mini-ork): auto-revert out-of-scope files (2 files)"
_TS = "20260101T000000"


def _g(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **_ENV})
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def _scenario(root: Path, kind: str):
    repo = root / "repo"; repo.mkdir(parents=True)
    _g(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "init")
    wt = root / "wt"
    _g(repo, "worktree", "add", "-q", "-b", "feat", str(wt), "main")
    if kind == "at_base":
        return repo, wt
    (wt / "work.txt").write_text("worker change\n")
    _g(wt, "add", "-A"); _g(wt, "commit", "-qm", "feat: real work")
    if kind == "contaminated":
        (wt / "reverted.txt").write_text("x\n")
        _g(wt, "add", "-A"); _g(wt, "commit", "-qm", _AR)
    if kind == "dirty":
        (wt / "work.txt").write_text("uncommitted\n")
    return repo, wt


@pytest.mark.parametrize("kind,contaminated", [
    ("contaminated", True), ("clean", False), ("at_base", False),
])
def test_detect(tmp_path, kind, contaminated):
    _, wt = _scenario(tmp_path, kind)
    count = bq.quarantine_detect(str(wt))
    if contaminated:
        assert count > 0
    else:
        assert count == 0


def test_reset_contaminated(tmp_path):
    _, wp = _scenario(tmp_path / "p", "contaminated")
    tip = _g(wp, "rev-parse", "HEAD")
    rc = bq.quarantine_reset("epicA", str(wp), ts=_TS, run_dir=str(tmp_path / "p" / "run"))
    assert rc == 0
    # branch now at merge-base
    assert _g(wp, "rev-parse", "HEAD") == _g(wp, "merge-base", "main", "HEAD")
    # quarantine ref preserved at the old tip
    ref = f"refs/quarantine/epicA/{_TS}"
    assert _g(wp, "rev-parse", ref) == tip
    # audit JSON written with the expected shape
    j = json.loads((tmp_path / "p" / "run" / "quarantine-decision.json").read_text())
    assert j["action"] == "reset_to_merge_base"
    assert j["ts"] == _TS
    assert j["branch"] == "feat"


def test_reset_at_base_noop(tmp_path):
    _, wp = _scenario(tmp_path / "p", "at_base")
    rc = bq.quarantine_reset("e", str(wp), ts=_TS, run_dir=str(tmp_path / "p" / "run"))
    assert rc == 0
    # no quarantine-decision written (no-op path)
    assert not (tmp_path / "p" / "run" / "quarantine-decision.json").exists()


def test_reset_dirty_aborts(tmp_path):
    _, wp = _scenario(tmp_path / "p", "dirty")
    rc = bq.quarantine_reset("e", str(wp), ts=_TS, run_dir=str(tmp_path / "p" / "run"))
    assert rc == 1


def test_reset_env_skip(tmp_path):
    _, wp = _scenario(tmp_path / "p", "contaminated")
    tip = _g(wp, "rev-parse", "HEAD")
    os.environ["MO_QUARANTINE_ON_AUTO_REVERT"] = "0"
    try:
        rc = bq.quarantine_reset("e", str(wp), ts="x", run_dir=str(tmp_path / "p" / "run"))
    finally:
        del os.environ["MO_QUARANTINE_ON_AUTO_REVERT"]
    assert rc == 0
    # branch unchanged (skip → no reset)
    assert _g(wp, "rev-parse", "HEAD") == tip
