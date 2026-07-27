"""Unit tests: mini_ork.vcs.repo_integrity_guard (bash parity halves removed; formerly vs lib/repo_integrity_guard.sh).

Builds temp git repos with FIXED commit dates (so SHAs are deterministic)
and asserts, after the guard runs: the branch tip (healed or advanced), the
LKG baseline file, and the recovery-log TSV. All git ops in throwaway repos.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.vcs import repo_integrity_guard as rig

DA, DB, DC = "2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z", "2025-01-01T00:00:00Z"
_BASE_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


def _g(cwd, *args, date=None, check=True):
    env = {**os.environ, **_BASE_ENV}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def _commit(repo, fname, content, msg, date):
    (Path(repo) / fname).write_text(content)
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", msg, date=date)
    return _g(repo, "rev-parse", "HEAD")


def _scenario(tmp_path: Path, kind: str):
    """Build the repo. Returns (repo, a, b, c_or_None)."""
    src = tmp_path / "repo"
    src.mkdir(parents=True)
    _g(src, "init", "-q", "-b", "main")
    a = _commit(src, "f.txt", "A\n", "A", DA)
    b = _commit(src, "f.txt", "B\n", "B", DB)   # main at B (newer)
    c = None
    if kind == "clobber":
        _g(src, "checkout", "-q", "-b", "side", a)
        c = _commit(src, "g.txt", "C\n", "C-unrelated-older", DC)  # older, off A
        _g(src, "checkout", "-q", "main")
        _g(src, "reset", "--hard", "-q", c)   # sideways clobber: main → C
    return src, a, b, c


def _write_lkg(repo, sha):
    d = Path(repo) / ".mini-ork"; d.mkdir(exist_ok=True)
    (d / "last-known-good-ref.main").write_text(sha + "\n")


def _lkg(repo):
    p = Path(repo) / ".mini-ork" / "last-known-good-ref.main"
    return p.read_text().strip() if p.exists() else None


def _log_no_ts(repo):
    p = Path(repo) / ".mini-ork" / "repo-integrity-guard.log"
    if not p.exists():
        return None
    return ["\t".join(line.split("\t")[1:]) for line in p.read_text().splitlines()]


def test_clobber_healed(tmp_path):
    rp, a, b, c = _scenario(tmp_path, "clobber")
    _write_lkg(rp, b)
    assert _g(rp, "rev-parse", "HEAD") == c  # clobbered to C before guard
    rig.check_and_heal(cwd=str(rp), now_iso="2026-07-05T00:00:00Z")
    # healed back to B
    assert _g(rp, "rev-parse", "refs/heads/main") == b
    # LKG unchanged (still B)
    assert _lkg(rp) == b
    # recovery log recorded (timestamp-stripped)
    assert _log_no_ts(rp) == [f"{b}\t{c}\trestored-branch-clobbered-from-{c}"]


def test_up_to_date_rerecord(tmp_path):
    rp, a, b, _ = _scenario(tmp_path, "clean")
    _write_lkg(rp, b)   # LKG == tip
    rig.check_and_heal(cwd=str(rp))
    assert _g(rp, "rev-parse", "HEAD") == b
    assert _lkg(rp) == b
    assert _log_no_ts(rp) is None  # no heal


def test_legit_advance_records(tmp_path):
    rp, a, b, _ = _scenario(tmp_path, "clean")
    _write_lkg(rp, a)   # LKG == older ancestor A, tip == B
    rig.check_and_heal(cwd=str(rp))
    # legitimate fast-forward: no heal, LKG advanced to B
    assert _g(rp, "rev-parse", "HEAD") == b
    assert _lkg(rp) == b
    assert _log_no_ts(rp) is None


def test_escape_hatch_noop(tmp_path):
    rp, a, b, c = _scenario(tmp_path, "clobber")
    _write_lkg(rp, b)
    os.environ["MO_REPO_INTEGRITY_GUARD_DISABLED"] = "1"
    try:
        rig.check_and_heal(cwd=str(rp))
    finally:
        del os.environ["MO_REPO_INTEGRITY_GUARD_DISABLED"]
    # disabled → no heal, main still at clobbered C
    assert _g(rp, "rev-parse", "HEAD") == c
