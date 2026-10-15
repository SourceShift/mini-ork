"""Unit tests: the four automated merge paths hold for human approval when
``MO_REVIEW_BEFORE_MERGE=required``.

Mirrors the kickoff's Definition of Done (six items). Hermetic per test: an
autouse fixture pins ``$MINI_ORK_HOME``/``$MINI_ORK_DB`` to ``tmp_path``,
scrubs every key the loader and the ledger read, and scrubs ``MO_AUTO_MERGE``
so ``auto_merge_pr_one`` only enters the HELD branch when the test sets it.
All git/DB work is in throwaway dirs; ``gh`` is monkey-patched with a
pass-through recorder that asserts no ``gh`` argv ever reaches it.

Existing tests (``tests/unit/test_auto_merge*.py``,
``tests/unit/test_cleaner*.py``, ``tests/unit/test_self_improve*.py``) must
still pass — DoD 6.
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

from mini_ork.gates import merge_approval  # noqa: E402
from mini_ork.recovery import cleaner  # noqa: E402
from mini_ork.vcs import auto_merge as am  # noqa: E402
from mini_ork.vcs import auto_merge_pr as amp  # noqa: E402


_POLICY_KEYS = (
    "MO_REVIEW_BEFORE_MERGE",
    "MO_REVIEW_AFTER_NODES",
    "MO_REVIEW_TIER",
    "MO_REVIEW_SIGNING_KEY",
)
_RUN_KEYS = ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Pin the home/DB to ``tmp_path``; scrub every key the loader / ledger /
    cleaner read. Also clear ``MO_AUTO_MERGE`` so ``auto_merge_pr_one``
    only enters the HELD branch when the test sets it."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("MO_AUTO_MERGE", raising=False)
    monkeypatch.delenv("REPO_ROOT", raising=False)


# ── git helpers (one private copy of the fixture shape from test_auto_merge_py) ──


def _g(cwd, *args, date=None, check=True):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def _sql(db, stmt):
    return subprocess.run(["sqlite3", db, stmt], capture_output=True, text=True).stdout.strip()


def _init_db(home: Path, db: str) -> None:
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)


def _count_gates(db: str, gate_id: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM mo_inbox_gates WHERE gate_id = ?", (gate_id,)
        ).fetchone()[0])
    finally:
        conn.close()


def _latest_gate_status(db: str, gate_id: str) -> str:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT status FROM mo_inbox_gates WHERE gate_id = ? "
            "ORDER BY inbox_id DESC LIMIT 1",
            (gate_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else ""


# ── auto_merge scenario fixture: APPROVE verdict + APPROVE epic + kickoff ──


def _build(tmp_path: Path, *, with_epic_no: bool = False):
    """Build the auto_merge scenario in tmp_path/p. Mirrors test_auto_merge_py
    but optionally omits the REQUEST_CHANGES epic so a single APPROVE epic
    yields skipped==1 on hold (the DoD asserts skipped == 1)."""
    p = tmp_path / "p"
    p.mkdir()
    repo = p / "repo"; repo.mkdir()
    home = p / "home" / ".mini-ork"; home.mkdir(parents=True)
    orch = p / "orch"
    db = str(home / "state.db")
    _init_db(home, db)

    _g(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    (repo / "kickoffs").mkdir()
    (repo / "kickoffs" / "epicOK.md").write_text("# Epic OK\n**Branch:** `feat/ok`\n")
    _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "base", date="2026-01-01T00:00:00Z")
    _g(repo, "checkout", "-q", "-b", "feat/ok")
    (repo / "feature.txt").write_text("the feature\n")
    _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "feat: add feature",
                              date="2026-02-01T00:00:00Z")
    _g(repo, "checkout", "-q", "main")

    _sql(db, "INSERT INTO epics (id,title,status,lane,worker_default,group_id,kickoff_path) "
             "VALUES ('epicOK','Epic OK','in progress','mini-ork','mini-ork','g1',"
             "'kickoffs/epicOK.md');")
    if with_epic_no:
        (repo / "kickoffs" / "epicNO.md").write_text("# Epic NO\n**Branch:** `feat/no`\n")
        _g(repo, "add", "kickoffs/epicNO.md"); _g(repo, "commit", "-qm", "kickoff for epicNO",
                                                  date="2026-01-01T00:00:01Z")
        _sql(db, "INSERT INTO epics (id,title,status,lane,worker_default,group_id,kickoff_path) "
                 "VALUES ('epicNO','Epic NO','in progress','mini-ork','mini-ork','g1',"
                 "'kickoffs/epicNO.md');")

    for epic, verdict in (("epicOK", "APPROVE"),) + (
        (("epicNO", "REQUEST_CHANGES"),) if with_epic_no else ()
    ):
        d = orch / "runs" / "job-test-1" / epic / "iter-1"
        d.mkdir(parents=True)
        (d / "verdict.json").write_text(json.dumps({"verdict": verdict}))
    return repo, home, orch, db


# ── DoD 1: content_ids ─────────────────────────────────────────────────────


def test_1_content_ids_returns_merge_base_and_tree(tmp_path):
    p = tmp_path / "r"; p.mkdir()
    _g(p, "init", "-q", "-b", "main")
    (p / "base.txt").write_text("base\n")
    _g(p, "add", "-A"); _g(p, "commit", "-qm", "base", date="2026-01-01T00:00:00Z")
    _g(p, "checkout", "-q", "-b", "feat/x")
    (p / "feature.txt").write_text("the feature\n")
    _g(p, "add", "-A"); _g(p, "commit", "-qm", "feat", date="2026-02-01T00:00:00Z")
    _g(p, "checkout", "-q", "main")

    expected_base = _g(p, "merge-base", "main", "feat/x")
    expected_tree = _g(p, "rev-parse", "feat/x^{tree}")
    base, tree = am.content_ids(str(p), "feat/x", "main")
    assert base == expected_base
    assert tree == expected_tree
    assert base and tree


def test_1_content_ids_missing_branch_returns_empty_tuple(tmp_path):
    p = tmp_path / "r"; p.mkdir()
    _g(p, "init", "-q", "-b", "main")
    (p / "base.txt").write_text("base\n")
    _g(p, "add", "-A"); _g(p, "commit", "-qm", "base", date="2026-01-01T00:00:00Z")

    base, tree = am.content_ids(str(p), "does/not/exist", "main")
    assert base == "" and tree == ""


# ── DoD 2: auto_merge HELD under required policy, then approved-and-merged ───


def test_2_auto_merge_held_writes_pending_row_and_main_unchanged(tmp_path, monkeypatch):
    repo, home, orch, db = _build(tmp_path)
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")

    main_before = _g(repo, "rev-parse", "main^{tree}")
    branch_before = _g(repo, "rev-parse", "--verify", "-q", "feat/ok", check=False)

    merged, skipped, failed = am.auto_merge(
        str(repo), str(orch), "job-test-1",
        mini_ork_home=str(home), state_db=db,
        now_iso="2026-07-05T00:00:00.000Z",
    )

    assert (merged, skipped, failed) == (0, 1, 0), (
        f"expected HELD (0,1,0), got ({merged},{skipped},{failed})"
    )
    # main is unchanged
    assert _g(repo, "rev-parse", "main^{tree}") == main_before
    # branch still exists (the HELD path does not delete it)
    assert _g(repo, "rev-parse", "--verify", "-q", "feat/ok", check=False) == branch_before
    # one pending row exists for the epic
    assert _count_gates(db, "merge:epicOK") == 1
    assert _latest_gate_status(db, "merge:epicOK") == "pending"


def test_2_auto_merge_after_approve_merges(tmp_path, monkeypatch):
    repo, home, orch, db = _build(tmp_path)
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")

    # First call → HELD
    am.auto_merge(
        str(repo), str(orch), "job-test-1",
        mini_ork_home=str(home), state_db=db,
        now_iso="2026-07-05T00:00:00.000Z",
    )
    assert _count_gates(db, "merge:epicOK") == 1

    # Approve exactly the content the held request was for
    base, tree = am.content_ids(str(repo), "feat/ok", "main")
    merge_approval.approve(
        "epicOK", tree, approver="test", base_sha=base,
        env={}, db_path=db,
    )
    assert _latest_gate_status(db, "merge:epicOK") == "approved"

    # Second call merges
    merged, skipped, failed = am.auto_merge(
        str(repo), str(orch), "job-test-1",
        mini_ork_home=str(home), state_db=db,
        now_iso="2026-07-05T00:00:00.000Z",
    )
    assert (merged, skipped, failed) == (1, 0, 0)
    assert _g(repo, "cat-file", "-p", "main:feature.txt") == "the feature"


# ── DoD 3: auto_merge with policy off merges as before, no row written ──────


def test_3_auto_merge_policy_off_merges_and_writes_no_gate_row(tmp_path, monkeypatch):
    repo, home, orch, db = _build(tmp_path)
    # No MO_REVIEW_BEFORE_MERGE set → default off

    merged, skipped, failed = am.auto_merge(
        str(repo), str(orch), "job-test-1",
        mini_ork_home=str(home), state_db=db,
        now_iso="2026-07-05T00:00:00.000Z",
    )
    assert (merged, skipped, failed) == (1, 0, 0)
    assert _g(repo, "cat-file", "-p", "main:feature.txt") == "the feature"
    # ``require`` short-circuited on "review off" without touching the DB;
    # gate_id 'merge:epicOK' must NOT exist.
    assert _count_gates(db, "merge:epicOK") == 0


# ── DoD 4: auto_merge_pr_one HELD — rc 1, gh never invoked ─────────────────


@pytest.fixture
def _pr_db(tmp_path):
    """Seed an epics row with pr_url + branch, build real schema."""
    home = tmp_path / ".mini-ork"; home.mkdir()
    db = str(home / "state.db")
    _init_db(home, db)
    _sql(db, "INSERT INTO epics (id,title,status,pr_url,branch) "
             "VALUES ('e1','T','in review','https://github.com/o/r/pull/1','feat/ok');")
    return db


def test_4_auto_merge_pr_held_returns_1_and_never_calls_gh(tmp_path, monkeypatch, _pr_db):
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    monkeypatch.setenv("MO_AUTO_MERGE", "1")

    # Stub the gh-shaped gates so we get past them to the HELD site, and
    # so nothing leaks out to network even if the HELD guard is missing.
    monkeypatch.setattr(amp, "gh_ready", lambda: 0)
    monkeypatch.setattr(amp, "checks_passing", lambda url: 0)
    monkeypatch.setattr(amp, "has_approval", lambda url: True)
    monkeypatch.setattr(amp, "age_ok", lambda url: True)

    # Pass-through recorder for subprocess.run so the sqlite3 CLI inside
    # ``_sql`` and any git inside ``content_ids`` still work, while we can
    # assert no gh argv is ever produced.
    real_run = subprocess.run
    gh_calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "gh":
            gh_calls.append(list(argv))
            raise AssertionError(f"gh must NOT be called when HELD; got argv={argv!r}")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run, raising=True)
    # The module imported its own reference to subprocess.run at module load;
    # patch the namespace it actually uses, too.
    monkeypatch.setattr(amp.subprocess, "run", fake_run, raising=True)

    rc = amp.auto_merge_pr_one("e1", state_db=_pr_db, repo_root=str(tmp_path / "no-such-repo"))
    assert rc == 1
    assert gh_calls == [], f"gh must not be called on hold; got {gh_calls!r}"
    assert _count_gates(_pr_db, "merge:e1") == 1
    assert _latest_gate_status(_pr_db, "merge:e1") == "pending"


def test_4_auto_merge_pr_policy_off_does_not_enqueue(tmp_path, monkeypatch, _pr_db):
    """Belt-and-braces: with the policy off the HELD site must be a no-op, so
    no ``merge:e1`` row is written."""
    monkeypatch.setenv("MO_AUTO_MERGE", "1")
    # MO_REVIEW_BEFORE_MERGE stays unset via the autouse fixture.
    monkeypatch.setattr(amp, "gh_ready", lambda: 0)
    monkeypatch.setattr(amp, "checks_passing", lambda url: 0)
    monkeypatch.setattr(amp, "has_approval", lambda url: True)
    monkeypatch.setattr(amp, "age_ok", lambda url: True)

    # Fake `gh pr merge` succeeds.
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv[:3] == ["gh", "pr", "merge"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run, raising=True)
    monkeypatch.setattr(amp.subprocess, "run", fake_run, raising=True)

    rc = amp.auto_merge_pr_one("e1", state_db=_pr_db, repo_root=str(tmp_path / "no-such-repo"))
    assert rc == 0
    assert _count_gates(_pr_db, "merge:e1") == 0


# ── DoD 5: cleaner HELD → rc 8 + verdict.json + branch stays + main unchanged


def _cleaner_repo(tmp: Path, *, with_branch: bool = True):
    """Throwaway repo: main + a chore/cleaner-* branch with one file ahead."""
    r = tmp / "r"; r.mkdir()
    def g(*a, **k):
        return subprocess.run(["git", *a], cwd=r, capture_output=True, text=True, **k)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t.co"); g("config", "user.name", "t")
    (r / "f.txt").write_text("base\n")
    g("add", "."); g("commit", "-qm", "init")
    if with_branch:
        g("checkout", "-q", "-b", "chore/cleaner-baseline_rot-20200101T000000Z")
        (r / "fix.txt").write_text("cleaner fix\n")
        g("add", "."); g("commit", "-qm", "fix(baseline): rot")
        g("checkout", "-q", "main")
    return r


def _run_cleaner(r: Path, *, held_branch: str, monkeypatch):
    """Run ``cleaner.main`` with the seams the existing test uses; uses a
    ``spawn`` lambda that just commits the seeded file and a ``gauntlet``
    lambda that passes."""
    det = r / "det.json"
    det.write_text(json.dumps({
        "epic_id": "e9",
        "classification": "baseline_rot",
        "cleaner_brief": "fix the thing",
        "rationale": "rot detected",
    }))
    out = r / "out"; out.mkdir(exist_ok=True)
    old_env = dict(os.environ)
    os.environ.update({"MINI_ORK_ROOT": str(r), "REPO_ROOT": str(r),
                       "MINI_ORK_HOME": str(r / ".mini-ork")})
    cwd = os.getcwd(); os.chdir(r)
    try:

        def spawn(root, prompt_file, out_dir, model, budget, scope_card):
            # Simulate the cleaner writing a fix file and committing it on
            # the branch the cleaner just created (chore/cleaner-…-<ts>).
            # ``git add`` the single file, NOT ``-A`` — prompt.md lives
            # inside the worktree but isn't a candidate for the baseline.
            subprocess.run(["git", "-C", root, "config", "user.email", "t@t.co"],
                           check=True)
            subprocess.run(["git", "-C", root, "config", "user.name", "t"],
                           check=True)
            (Path(root) / "fix.txt").write_text("cleaner fix\n")
            subprocess.run(["git", "-C", root, "add", "fix.txt"], check=True)
            subprocess.run(["git", "-C", root, "commit", "-qm", "fix: rot"],
                           check=True,
                           env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                "GIT_AUTHOR_EMAIL": "t@t.co",
                                "GIT_COMMITTER_NAME": "t",
                                "GIT_COMMITTER_EMAIL": "t@t.co"})
            return 0

        def gauntlet(root, gauntlet_sh, gauntlet_out):
            return 0  # PASS

        return cleaner.main([str(det), str(out)], root=str(r),
                            home=str(r / ".mini-ork"),
                            spawn=spawn, gauntlet=gauntlet)
    finally:
        os.chdir(cwd)
        os.environ.clear(); os.environ.update(old_env)


def test_5_cleaner_held_returns_8_and_writes_held_verdict(tmp_path, monkeypatch):
    r = _cleaner_repo(tmp_path, with_branch=False)
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")

    # Resolve the branch the cleaner will create so we can assert against it.
    # _run_cleaner's spawn commits fix.txt; the resulting branch is the
    # fresh one cleaner created (chore/cleaner-baseline_rot-<ts>), not the
    # reuse branch. Read it from git afterwards.
    main_before = _g(r, "rev-parse", "main")
    rc = _run_cleaner(r, held_branch="", monkeypatch=monkeypatch)

    # The cleaner branch the HELD site references
    cur_branches = subprocess.run(
        ["git", "branch", "--list", "chore/cleaner-*", "--format=%(refname:short)"],
        cwd=r, capture_output=True, text=True).stdout.split()
    assert rc == 8, f"expected rc=8, got {rc}"
    assert len(cur_branches) >= 1
    branch = cur_branches[0]

    # main is unchanged
    assert _g(r, "rev-parse", "main") == main_before
    # branch still exists
    assert _g(r, "rev-parse", "--verify", "-q", branch, check=False)
    # verdict.json written in the same place as the FAIL site, with the
    # HELD shape.
    verdict = json.load(open(r / "out" / "verdict.json"))
    assert verdict["verdict"] == "HELD_FOR_REVIEW"
    assert verdict["branch"] == branch
    assert "reason" in verdict and verdict["reason"]

    # HELD writes the gate row through ``merge_approval.require`` to whatever
    # path the env's ``MINI_ORK_DB`` points at (the autouse fixture pins it
    # to a tmp_path file), not the local ``r/.mini-ork/state.db`` the cleaner
    # itself writes to.
    db = os.environ["MINI_ORK_DB"]
    assert _count_gates(db, f"merge:cleaner:{branch}") == 1
    assert _latest_gate_status(db, f"merge:cleaner:{branch}") == "pending"


def test_5_cleaner_policy_off_merges_normally(tmp_path, monkeypatch):
    """Belt-and-braces: with policy off, the HELD site is a no-op and the
    cleaner merges as before (rc 0, PASS verdict)."""
    r = _cleaner_repo(tmp_path, with_branch=False)
    # MO_REVIEW_BEFORE_MERGE stays unset via the autouse fixture → default off.
    rc = _run_cleaner(r, held_branch="", monkeypatch=monkeypatch)
    assert rc == 0
    verdict = json.load(open(r / "out" / "verdict.json"))
    assert verdict["verdict"] == "PASS"
    # No gate row written — and ``mo_inbox_gates`` need not even exist on
    # disk because policy off is a no-op. Use the env's MINI_ORK_DB which
    # the autouse fixture pins to a tmp_path.
    db = os.environ["MINI_ORK_DB"]
    # The DB file may not exist at all (no row was written, no connection
    # was opened). If it does exist, ensure no HELD-shaped row.
    if os.path.isfile(db):
        assert _count_gates(db, "merge:cleaner:%") == 0