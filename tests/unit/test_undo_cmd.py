"""Hermetic tests for ``mini-ork undo``.

The topology is the one the worktree script's own suite uses: a bare
``origin.git`` plus a clone that stands in for the primary checkout, with the
worktree script copied in and every path under pytest's ``tmp_path``. The green
gate is stubbed with ``--test-cmd true`` except in the one test that asserts a
failing gate lands nothing.

The module is exercised both through its functions (``resolve_ref`` /
``plan_undo``) and through ``main()`` end to end, so the tests cover the refusals
and the single write path rather than only the happy case.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_ork.cli.undo as undo
from mini_ork.rsi import worktree as wt_mod

REPO = Path(__file__).resolve().parents[2]
WORKTREE_PY = REPO / "scripts" / "mini_ork_worktree.py"

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Undo Tests",
    "GIT_AUTHOR_EMAIL": "undo@example.invalid",
    "GIT_COMMITTER_NAME": "Undo Tests",
    "GIT_COMMITTER_EMAIL": "undo@example.invalid",
}

SEED_MOD = "print('v1')\n"
SEED_TEST = "def test_a():\n    assert True\n"


# ── helpers ────────────────────────────────────────────────────────────────


def strip_git_config_env(monkeypatch) -> None:
    """Drop a lane-level ``GIT_CONFIG_*`` push guard from the child git env."""
    for key in list(os.environ):
        if (key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_")
                or key.startswith("GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(key, raising=False)


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=120)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git -C {cwd} {' '.join(args)} failed rc={proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    return proc


def sha_of(cwd, ref="HEAD") -> str:
    return git(cwd, "rev-parse", ref).stdout.strip()


def commit_file(repo: Path, rel: str, content: str, message: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", "--", rel)
    git(repo, "commit", "-m", message)
    return sha_of(repo)


def commit_files(repo: Path, changes: dict, message: str) -> str:
    for rel, content in changes.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(repo, "add", "--", rel)
    git(repo, "commit", "-m", message)
    return sha_of(repo)


def push_main(repo: Path) -> None:
    git(repo, "push", "origin", "main")


def insert_rsi_cycle(db: Path, cycle_id: str, merged_sha: str) -> None:
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "INSERT INTO rsi_cycles (cycle_id, started_at, mode, status, merged_sha) "
            "VALUES (?, 0, 'auto', 'merged', ?)", (cycle_id, merged_sha))
        con.commit()
    finally:
        con.close()


def audit_rows(db: Path) -> list:
    con = sqlite3.connect(str(db))
    try:
        return con.execute(
            "SELECT event_type, actor, target, payload FROM audit_log "
            "ORDER BY audit_id").fetchall()
    finally:
        con.close()


def extract_json(text: str):
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        return value
    raise AssertionError(f"no JSON object in output: {text!r}")


def call_main(argv) -> int:
    try:
        return undo.main(list(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def _migrated_db(tmp_path_factory):
    from mini_ork.stores.migrate import init_db

    path = tmp_path_factory.mktemp("state") / "state.db"
    rc, _out, err = init_db(db=str(path), root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}: {err}"
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    strip_git_config_env(monkeypatch)
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)],
                   capture_output=True, check=True)
    git(clone, "symbolic-ref", "HEAD", "refs/heads/main")
    git(clone, "config", "user.name", "Undo Tests")
    git(clone, "config", "user.email", "undo@example.invalid")
    git(clone, "config", "commit.gpgsign", "false")
    git(clone, "config", "push.gpgsign", "false")
    git(clone, "config", "core.hooksPath", str(hooks))

    commit_file(clone, "mod.py", SEED_MOD, "seed: mod.py")
    commit_file(clone, "tests/unit/test_a.py", SEED_TEST, "seed: test_a")
    push_main(clone)
    return SimpleNamespace(origin=origin, clone=clone, hooks=hooks)


@pytest.fixture
def state_db(tmp_path, _migrated_db):
    target = tmp_path / "state.db"
    src = sqlite3.connect(str(_migrated_db))
    dst = sqlite3.connect(str(target))
    try:
        with dst:
            src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.commit()
    finally:
        dst.close()
        src.close()
    return target


@pytest.fixture
def undo_env(repo, state_db, tmp_path, monkeypatch):
    scripts = repo.clone / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WORKTREE_PY, scripts / "mini_ork_worktree.py")

    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setenv("MINI_ORK_ROOT", str(repo.clone))
    monkeypatch.setenv("MINI_ORK_DB", str(state_db))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_WORKTREES_DIR", str(worktrees))
    monkeypatch.setenv("MINI_ORK_OWNERSHIP_FILE", str(tmp_path / "ownership.tsv"))
    monkeypatch.setenv("MINI_ORK_WORKTREE_LOCK", str(tmp_path / "worktree.lockdir"))
    monkeypatch.setenv("MO_CONCORD", "0")
    monkeypatch.setenv("MO_MERGE_DIFFERENTIAL", "0")
    for leaked in ("MINI_ORK_ENGINE_ROOT", "MINI_ORK_TARGET_REPO",
                   "MINI_ORK_PROJECT_HOME", "MO_MERGE_ALLOW_OSS",
                   "MO_MERGE_ALLOW_UNCLAIMED"):
        monkeypatch.delenv(leaked, raising=False)
    return SimpleNamespace(db=state_db, worktrees=worktrees, home=home,
                           clone=repo.clone)


# ── resolve_ref ────────────────────────────────────────────────────────────


def test_a_full_sha_resolves_to_itself(repo):
    sha = sha_of(repo.clone)
    assert undo.resolve_ref(sha, root=repo.clone, db=None) == sha


def test_a_short_sha_resolves_to_the_full_sha(repo):
    sha = sha_of(repo.clone)
    resolved = undo.resolve_ref(sha[:8], root=repo.clone, db=None)
    assert resolved == sha and len(resolved) == 40


def test_a_run_id_resolves_to_the_commit_it_published(repo, undo_env):
    sha = commit_file(repo.clone, "mod.py", "print('v2')\n",
                      "mini-ork(code-fix): implementer [run run-x1]")
    push_main(repo.clone)
    assert undo.resolve_ref("run-x1", root=repo.clone, db=undo_env.db) == sha


def test_two_commits_for_one_run_id_are_refused_naming_both(repo, undo_env):
    first = commit_file(repo.clone, "mod.py", "print('v2')\n",
                        "mini-ork(code-fix): implementer [run run-x2]")
    second = commit_file(repo.clone, "mod.py", "print('v3')\n",
                         "mini-ork(code-fix): publisher [run run-x2]")
    push_main(repo.clone)

    with pytest.raises(undo.UndoError) as excinfo:
        undo.resolve_ref("run-x2", root=repo.clone, db=undo_env.db)

    message = str(excinfo.value)
    assert first[:12] in message and second[:12] in message
    assert "several commits" in message and "undo one sha at a time" in message


def test_an_rsi_cycle_id_resolves_to_its_merged_sha(repo, undo_env):
    sha = commit_file(repo.clone, "mod.py", "print('v2')\n", "rsi merge")
    push_main(repo.clone)
    insert_rsi_cycle(undo_env.db, "cyc-1", sha)
    assert undo.resolve_ref("cyc-1", root=repo.clone, db=undo_env.db) == sha


def test_an_unknown_ref_is_refused(repo, undo_env):
    with pytest.raises(undo.UndoError) as excinfo:
        undo.resolve_ref("no-such-ref", root=repo.clone, db=undo_env.db)
    assert "no commit found for no-such-ref" in str(excinfo.value)


# ── plan_undo ──────────────────────────────────────────────────────────────


def _merge_commit(helpers_repo) -> str:
    repo = helpers_repo
    git(repo, "checkout", "-b", "feature")
    commit_file(repo, "mod.py", "print('v2')\n", "feature change")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "-m", "merge feature", "feature")
    push_main(repo)
    return sha_of(repo)


def test_a_commit_not_on_origin_main_is_refused(repo, undo_env):
    git(repo.clone, "checkout", "-b", "side")
    sha = commit_file(repo.clone, "mod.py", "print('v2')\n", "side change")
    git(repo.clone, "checkout", "main")

    with pytest.raises(undo.UndoError) as excinfo:
        undo.plan_undo(sha, root=repo.clone, db=undo_env.db, test_cmd="true")
    assert "is not on origin/main" in str(excinfo.value)


def test_a_merge_commit_without_a_mainline_is_refused(repo, undo_env):
    sha = _merge_commit(repo.clone)
    with pytest.raises(undo.UndoError) as excinfo:
        undo.plan_undo(sha, root=repo.clone, db=undo_env.db)
    message = str(excinfo.value)
    assert "--mainline" in message and "merge commit" in message


def test_a_merge_commit_with_a_mainline_produces_a_plan(repo, undo_env):
    sha = _merge_commit(repo.clone)
    plan = undo.plan_undo(sha, root=repo.clone, db=undo_env.db,
                          mainline=1, test_cmd="true")
    assert plan.sha == sha and plan.mainline == 1 and list(plan.files)


def test_a_mainline_on_a_single_parent_commit_is_refused(repo, undo_env):
    sha = commit_file(repo.clone, "mod.py", "print('v2')\n", "plain change")
    push_main(repo.clone)
    with pytest.raises(undo.UndoError) as excinfo:
        undo.plan_undo(sha, root=repo.clone, db=undo_env.db, mainline=1,
                       test_cmd="true")
    assert "--mainline only applies to merge commits" in str(excinfo.value)


def test_an_out_of_range_mainline_is_refused(repo, undo_env):
    sha = _merge_commit(repo.clone)
    with pytest.raises(undo.UndoError):
        undo.plan_undo(sha, root=repo.clone, db=undo_env.db, mainline=3,
                       test_cmd="true")


def test_an_already_reverted_commit_is_refused(repo, undo_env):
    bad = commit_file(repo.clone, "mod.py", "print('v2')\n", "bad change")
    push_main(repo.clone)
    git(repo.clone, "revert", "--no-edit", bad)
    push_main(repo.clone)

    with pytest.raises(undo.UndoError) as excinfo:
        undo.plan_undo(bad, root=repo.clone, db=undo_env.db, test_cmd="true")
    message = str(excinfo.value)
    assert "already reverted" in message and bad[:12] in message


def test_the_plan_names_the_commit_files_and_slug(repo, undo_env):
    sha = commit_files(repo.clone, {"mod.py": "print('v2')\n",
                                    "tests/unit/test_a.py": "def test_a():\n    assert 2 == 2\n"},
                       "change mod and test")
    push_main(repo.clone)

    plan = undo.plan_undo(sha, root=repo.clone, db=undo_env.db)
    assert plan.ref == sha and plan.sha == sha
    assert plan.subject == "change mod and test"
    assert plan.slug == f"revert-{sha[:12]}"
    assert plan.mainline is None
    assert plan.files == ("mod.py", "tests/unit/test_a.py")


def test_the_default_gate_names_an_existing_test_file(repo, undo_env):
    sha = commit_files(repo.clone, {"mod.py": "print('v2')\n",
                                    "tests/unit/test_a.py": "def test_a():\n    assert 2 == 2\n"},
                       "change mod and test")
    push_main(repo.clone)

    plan = undo.plan_undo(sha, root=repo.clone, db=undo_env.db)
    assert plan.test_cmd.startswith("python3.11 -m pytest -q -p no:asyncio ")
    assert "tests/unit/test_a.py" in plan.test_cmd


def test_a_newly_added_test_file_does_not_satisfy_the_gate(repo, undo_env):
    sha = commit_files(repo.clone, {"mod.py": "print('v2')\n",
                                    "tests/unit/test_new.py": "def test_new():\n    assert True\n"},
                       "add a brand new test")
    push_main(repo.clone)

    with pytest.raises(undo.UndoError) as excinfo:
        undo.plan_undo(sha, root=repo.clone, db=undo_env.db)
    message = str(excinfo.value)
    assert "no existing test file is touched" in message
    assert "--test-cmd" in message


def test_a_caller_supplied_test_cmd_is_used_verbatim(repo, undo_env):
    sha = commit_file(repo.clone, "mod.py", "print('v2')\n", "plain change")
    push_main(repo.clone)
    plan = undo.plan_undo(sha, root=repo.clone, db=undo_env.db, test_cmd="true")
    assert plan.test_cmd == "true"


# ── main() ─────────────────────────────────────────────────────────────────


def test_help_exits_zero(repo, undo_env):
    assert call_main(["--help"]) == 0


def test_a_usage_error_exits_two(repo, undo_env):
    assert call_main(["--no-such-option"]) == 2


def test_an_undo_error_exits_one_on_stderr(repo, undo_env, capsys):
    rc = call_main(["no-such-ref"])
    captured = capsys.readouterr()
    assert rc == 1 and "no commit found for no-such-ref" in captured.err


def test_dry_run_json_prints_the_plan_and_changes_nothing(repo, undo_env, capsys):
    sha = commit_files(repo.clone, {"mod.py": "print('v2')\n",
                                    "tests/unit/test_a.py": "def test_a():\n    assert 2 == 2\n"},
                       "bad change")
    push_main(repo.clone)
    before = sha_of(repo.clone, "origin/main")

    rc = call_main([sha, "--dry-run", "--json"])
    captured = capsys.readouterr()

    assert rc == 0
    plan = extract_json(captured.out)
    assert plan["sha"] == sha and plan["subject"] == "bad change"
    assert plan["slug"] == f"revert-{sha[:12]}"
    assert list(undo_env.worktrees.iterdir()) == []
    assert audit_rows(undo_env.db) == []
    assert sha_of(repo.clone, "origin/main") == before


def test_a_gated_revert_lands_a_new_commit_and_is_audited(repo, undo_env, capsys):
    sha = commit_file(repo.clone, "mod.py", "print('v2')\n", "bad change")
    push_main(repo.clone)

    rc = call_main([sha, "--test-cmd", "true"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err + captured.out

    git(repo.clone, "fetch", "origin", "main")
    message = git(repo.clone, "log", "-1", "--format=%B", "origin/main").stdout
    assert message.lstrip().startswith('Revert "')
    assert f"This reverts commit {sha}" in message
    assert (repo.clone / "mod.py").read_text(encoding="utf-8") == SEED_MOD
    assert git(repo.clone, "merge-base", "--is-ancestor", sha,
               "origin/main").returncode == 0

    rows = audit_rows(undo_env.db)
    assert len(rows) == 1
    event_type, actor, target, payload = rows[0]
    assert event_type == "undo" and target == f"commit:{sha}"
    assert actor.startswith("human:")
    data = json.loads(payload)
    assert data["sha"] == sha and data["slug"] == f"revert-{sha[:12]}"
    assert data["status"] in ("merged", "diverged")

    revert_on_main = sha_of(repo.clone, "origin/main")
    assert revert_on_main.startswith(data["revert_sha"])
    assert f"undo {sha[:12]}" in captured.out
    assert revert_on_main[:12] in captured.out


def test_a_failing_gate_leaves_origin_main_unchanged(repo, undo_env, capsys):
    sha = commit_file(repo.clone, "mod.py", "print('v2')\n", "bad change")
    push_main(repo.clone)
    before = sha_of(repo.clone, "origin/main")

    rc = call_main([sha, "--test-cmd", "false"])
    captured = capsys.readouterr()
    assert rc == 1, captured.err + captured.out
    assert sha_of(repo.clone, "origin/main") == before
    assert (repo.clone / "mod.py").read_text(encoding="utf-8") == "print('v2')\n"

    rows = audit_rows(undo_env.db)
    assert len(rows) == 1
    assert json.loads(rows[0][3])["status"] != "merged"


# ── revert(mainline=...) argv ──────────────────────────────────────────────


class _FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, cwd=None, env=None, timeout_s=None):
        argv = list(argv)
        self.calls.append(argv)
        if "--show-toplevel" in argv:
            return 0, argv[argv.index("-C") + 1] + "\n", ""
        if "rev-parse" in argv and "HEAD" in argv:
            return 0, "e" * 40 + "\n", ""
        if "remote" in argv and "get-url" in argv:
            return 0, "https://example.invalid/repo.git\n", ""
        return 0, "", ""

    def revert_calls(self):
        return [call for call in self.calls
                if call and call[0] == "git" and "revert" in call]


def test_revert_passes_mainline_as_dash_m(tmp_path):
    primary = tmp_path / "primary"
    scripts = primary / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "mini_ork_worktree.py").write_text("# stand-in\n", encoding="utf-8")
    cfg = wt_mod.WorktreeConfig(
        slug="abc123456789",
        files=("mod.py",),
        worktrees_dir=tmp_path / "worktrees",
        primary=primary,
        test_cmd="true",
    )
    runner = _FakeRunner()

    wt_mod.revert(cfg, "f" * 40, mainline=2, runner=runner)

    calls = runner.revert_calls()
    assert calls
    assert calls[0][-5:] == ["revert", "--no-edit", "-m", "2", "f" * 40]
