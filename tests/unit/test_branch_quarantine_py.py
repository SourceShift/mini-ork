"""Parity gate: mini_ork.ported.branch_quarantine vs lib/branch-quarantine.sh.

Builds identical temp git repos (worktree branch clean / contaminated with
auto-revert commits / at merge-base / dirty) and compares detect counts, reset
return codes, resulting branch tip, the preserved quarantine ref, and the audit
JSON. All git ops happen in throwaway repos. ``ts`` is parsed from the bash run
and fed to the port so refs/JSON line up exactly.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import branch_quarantine as bq  # noqa: E402

SH = REPO / "lib" / "branch-quarantine.sh"
_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
_AR = "chore(mini-ork): auto-revert out-of-scope files (2 files)"


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


def _bash_reset(epic, wt, run_dir, env_extra=None):
    rd = str(run_dir).replace("'", "'\\''")
    script = (f'. "{SH}"; mo_run_dir() {{ echo "{rd}"; }}; '
              f'mo_quarantine_reset "{epic}" "{wt}"')
    env = {**os.environ, **_ENV, "MINI_ORK_ROOT": str(REPO)}
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    ts = None
    m = re.search(r"refs/quarantine/[^/]+/(\d{8}T\d{6})", r.stderr)
    if m:
        ts = m.group(1)
    return r.returncode, ts


def _bash_detect(wt):
    r = subprocess.run(["bash", "-c", f'. "{SH}"; mo_quarantine_detect "{wt}"'],
                       capture_output=True, text=True,
                       env={**os.environ, **_ENV, "MINI_ORK_ROOT": str(REPO)})
    return r.stdout.strip(), r.returncode


@pytest.mark.parametrize("kind,contaminated", [
    ("contaminated", True), ("clean", False), ("at_base", False),
])
def test_detect_parity(tmp_path, kind, contaminated):
    _, wt = _scenario(tmp_path, kind)
    out_b, rc_b = _bash_detect(wt)
    count_p = bq.quarantine_detect(str(wt))
    if contaminated:
        assert rc_b == 0 and int(out_b) == count_p > 0
    else:
        assert rc_b == 1 and count_p == 0


def test_reset_contaminated_parity(tmp_path):
    rb, wb = _scenario(tmp_path / "b", "contaminated")
    rp, wp = _scenario(tmp_path / "p", "contaminated")
    base_b = _g(wb, "merge-base", "main", "HEAD")
    tip_b = _g(wb, "rev-parse", "HEAD")
    rc_b, ts = _bash_reset("epicA", wb, tmp_path / "b" / "run")
    assert rc_b == 0 and ts is not None
    rc_p = bq.quarantine_reset("epicA", str(wp), ts=ts, run_dir=str(tmp_path / "p" / "run"))
    assert rc_p == 0
    # both branches now at merge-base
    assert _g(wb, "rev-parse", "HEAD") == base_b
    assert _g(wp, "rev-parse", "HEAD") == _g(wp, "merge-base", "main", "HEAD")
    # quarantine ref preserved at the old tip on both
    ref = f"refs/quarantine/epicA/{ts}"
    assert _g(wb, "rev-parse", ref) == tip_b
    assert _g(wp, "rev-parse", ref) == _g(wp, "rev-parse", "refs/quarantine/epicA/" + ts)
    # audit JSON parity (identical structure; SHAs equal since same content graph)
    jb = json.loads((tmp_path / "b" / "run" / "quarantine-decision.json").read_text())
    jp = json.loads((tmp_path / "p" / "run" / "quarantine-decision.json").read_text())
    assert jb.keys() == jp.keys()
    assert jb["action"] == jp["action"] == "reset_to_merge_base"
    assert jb["ts"] == jp["ts"] == ts
    assert jb["branch"] == jp["branch"] == "feat"


def test_reset_at_base_noop(tmp_path):
    rb, wb = _scenario(tmp_path / "b", "at_base")
    rp, wp = _scenario(tmp_path / "p", "at_base")
    rc_b, _ = _bash_reset("e", wb, tmp_path / "b" / "run")
    rc_p = bq.quarantine_reset("e", str(wp), ts="20260101T000000", run_dir=str(tmp_path / "p" / "run"))
    assert rc_b == rc_p == 0
    # no quarantine-decision written (no-op path)
    assert not (tmp_path / "b" / "run" / "quarantine-decision.json").exists()
    assert not (tmp_path / "p" / "run" / "quarantine-decision.json").exists()


def test_reset_dirty_aborts(tmp_path):
    rb, wb = _scenario(tmp_path / "b", "dirty")
    rp, wp = _scenario(tmp_path / "p", "dirty")
    rc_b, _ = _bash_reset("e", wb, tmp_path / "b" / "run")
    rc_p = bq.quarantine_reset("e", str(wp), ts="20260101T000000", run_dir=str(tmp_path / "p" / "run"))
    assert rc_b == rc_p == 1


def test_reset_env_skip(tmp_path):
    rb, wb = _scenario(tmp_path / "b", "contaminated")
    rp, wp = _scenario(tmp_path / "p", "contaminated")
    tip_b = _g(wb, "rev-parse", "HEAD")
    rc_b, _ = _bash_reset("e", wb, tmp_path / "b" / "run", env_extra={"MO_QUARANTINE_ON_AUTO_REVERT": "0"})
    os.environ["MO_QUARANTINE_ON_AUTO_REVERT"] = "0"
    try:
        rc_p = bq.quarantine_reset("e", str(wp), ts="x", run_dir=str(tmp_path / "p" / "run"))
    finally:
        del os.environ["MO_QUARANTINE_ON_AUTO_REVERT"]
    assert rc_b == rc_p == 0
    # branch unchanged (skip → no reset)
    assert _g(wb, "rev-parse", "HEAD") == tip_b
