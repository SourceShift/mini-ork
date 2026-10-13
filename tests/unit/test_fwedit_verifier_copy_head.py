"""The framework-edit test verifier's throwaway copy has a HEAD.

The copy is ``git archive`` + ``git init``; with no commit, every test that
reads the engine sha or bundles the engine (``git rev-parse HEAD``,
``HEAD^{tree}``) failed in the copy alone — ten false failures on the
cloud-providers B run (run-1791906478-48004), all green in the real worktree.
The base tree is now committed before the diff is applied, so HEAD is the base
and the diff is a working-tree change, exactly like a real checkout.

The verifier runs all its checks at import, so it is loaded with
``subprocess.run`` stubbed (the pattern ``test_verifier_test_env.py`` uses) and
the copy helpers are then driven against a real repository with real git."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
FW_VERIFIER_DIR = REPO_ROOT / "recipes" / "framework-edit" / "verifiers"


def _load_verifier(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "verifier-test-work" / "repo").mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
    monkeypatch.syspath_prepend(str(FW_VERIFIER_DIR))
    spec = importlib.util.spec_from_file_location("_fwedit_verifier_copy_head",
                                                  FW_VERIFIER_DIR / "test.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    monkeypatch.undo()          # real subprocess.run from here on
    return mod


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def test_copy_has_a_base_commit_and_the_diff_stays_a_worktree_change(monkeypatch, tmp_path):
    mod = _load_verifier(monkeypatch, tmp_path)
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "app.py").write_text("print('v1')\n")
    monkeypatch.setattr(mod, "WORKTREE", str(copy))

    assert mod._assert_copy_is_own_git_root()
    head = _git(copy, "rev-parse", "HEAD")
    assert len(head) == 40
    assert _git(copy, "rev-parse", "HEAD^{tree}")           # what the engine bundle reads
    assert _git(copy, "status", "--short") == ""             # base fully committed

    # the candidate diff lands as a working-tree change on top of the base
    (copy / "app.py").write_text("print('v2')\n")
    assert _git(copy, "status", "--short") == "M app.py"

    # idempotent: a second pass (the apply step re-asserts) adds no commit
    assert mod._assert_copy_is_own_git_root()
    assert _git(copy, "rev-parse", "HEAD") == head
    assert _git(copy, "status", "--short") == "M app.py"


def test_base_commit_does_not_need_a_user_git_identity(monkeypatch, tmp_path):
    mod = _load_verifier(monkeypatch, tmp_path)
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "x.txt").write_text("x\n")
    monkeypatch.setattr(mod, "WORKTREE", str(copy))
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))                    # no ~/.gitconfig identity
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "nope"))
    assert mod._assert_copy_is_own_git_root()
    assert _git(copy, "log", "-1", "--format=%an") == "mini-ork-verifier"
