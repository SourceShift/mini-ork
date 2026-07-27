"""Unit tests: mini_ork.vcs.pr_create (bash parity halves removed; formerly vs lib/pr-create.sh).

`gh` and `git push` are the only network ops; a fake `gh` on PATH (printing a
fixed PR URL) + a real bare local origin make the happy path deterministic and
offline. Asserts open_pr's rc + emitted URL + persisted epics.pr_url, plus
the MO_OPEN_PR gate, idempotence, no-gh soft-skip, and title/body builders.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from shutil import which as shutil_which

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.vcs import pr_create as pc

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
URL = "https://github.com/o/r/pull/42"


def _g(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **ENV})
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def _sql(db, stmt):
    return subprocess.run(["sqlite3", db, stmt], capture_output=True, text=True).stdout.strip()


def _fake_gh(bindir: Path):
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1 $2" in\n'
        '  "auth status") exit 0;;\n'
        f'  "pr create") echo "{URL}"; exit 0;;\n'
        f'  "pr view") echo "{URL}"; exit 0;;\n'
        "esac\nexit 0\n")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _scenario(root: Path, with_pr_url=False):
    root.mkdir(parents=True)
    repo = root / "repo"; repo.mkdir()
    origin = root / "origin.git"
    home = root / ".mini-ork"; home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    _g(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("b\n"); _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "base")
    _g(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "f.txt").write_text("x\n"); _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "work")
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _g(repo, "remote", "add", "origin", str(origin))
    _g(repo, "push", "-q", "origin", "feat/x")   # branch already on origin → no push in open_pr
    _g(repo, "checkout", "-q", "main")
    (repo / "kickoffs").mkdir()
    (repo / "kickoffs" / "e.md").write_text("# My Epic Title\nbody line\n")
    pr = "'https://existing/pr/1'" if with_pr_url else "NULL"
    _sql(db, f"INSERT INTO epics (id,title,status,kickoff_path,pr_url) "
             f"VALUES ('e1','Fallback Title','in progress','kickoffs/e.md',{pr});")
    return repo, db


def test_open_pr_happy(tmp_path):
    _fake_gh(tmp_path / "bin")
    prefix = str(tmp_path / "bin")
    rp, db_p = _scenario(tmp_path / "p")
    kick_p = str(rp / "kickoffs" / "e.md")
    os.environ["PATH"] = f"{prefix}:{os.environ['PATH']}"; os.environ["MO_OPEN_PR"] = "1"
    try:
        rc_p, url_p = pc.open_pr("e1", "feat/x", kick_p, repo_root=str(rp), state_db=db_p)
    finally:
        os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]; del os.environ["MO_OPEN_PR"]
    assert rc_p == 0
    assert url_p == URL
    assert _sql(db_p, "SELECT pr_url FROM epics WHERE id='e1';") == URL


def test_open_pr_disabled_gate(tmp_path):
    rp, db_p = _scenario(tmp_path / "p")
    # MO_OPEN_PR unset → gate closed
    assert pc.open_pr("e1", "feat/x", "", repo_root=str(rp), state_db=db_p) == (2, "")


def test_open_pr_idempotent(tmp_path):
    rp, db_p = _scenario(tmp_path / "p", with_pr_url=True)
    os.environ["MO_OPEN_PR"] = "1"
    try:
        rc_p, url_p = pc.open_pr("e1", "feat/x", "", repo_root=str(rp), state_db=db_p)
    finally:
        del os.environ["MO_OPEN_PR"]
    assert rc_p == 0 and url_p == "https://existing/pr/1"


def test_build_title_and_body(tmp_path):
    rp, db_p = _scenario(tmp_path / "p")
    kick = str(rp / "kickoffs" / "e.md")
    assert pc.build_title("e1", kick, db_p) == "My Epic Title"
    # fallback to epics.title when kickoff has no heading
    (Path(kick).parent / "nohead.md").write_text("no heading\n")
    kick2 = str(Path(kick).parent / "nohead.md")
    assert pc.build_title("e1", kick2, db_p) == "Fallback Title"
    body = pc.build_body("e1", kick)
    assert "Auto-opened by mini-ork epic delivery for **e1**" in body
    assert "## Kickoff" in body and "My Epic Title" in body


def test_no_gh_soft_skip(tmp_path):
    rp, db_p = _scenario(tmp_path / "p")
    # minimal PATH: the tools the port needs, but NO gh
    nobin = tmp_path / "nobin"; nobin.mkdir()
    for tool in ("git", "sqlite3"):
        src = shutil_which(tool)
        if src:
            os.symlink(src, nobin / tool)
    # port: gh absent from PATH → (2, "")
    old = os.environ["PATH"]; os.environ["PATH"] = str(nobin); os.environ["MO_OPEN_PR"] = "1"
    try:
        assert pc.open_pr("e1", "feat/x", "", repo_root=str(rp), state_db=db_p) == (2, "")
    finally:
        os.environ["PATH"] = old; del os.environ["MO_OPEN_PR"]
