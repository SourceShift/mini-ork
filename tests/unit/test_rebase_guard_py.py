"""Standalone unit tests for ``mini_ork.vcs.rebase_guard``.

Replaces the bash-parity gate (previously ran ``lib/rebase-guard.sh`` in a
bash subprocess and diffed outcomes) as part of the bash→Python migration:
the Python port is now the sole implementation, so its coverage no longer
shells out to the bash *script* — it exercises the port directly and pins
the decision contract (rebase outcome + rebase-decision.json shape +
stale-base marker text) that ``lib/rebase-guard.sh`` used to define.

Fixture setup and the port's own git operations still shell out to real
``git`` (the port IS a git-ops routine) — only the bash comparison oracle
is gone from the loop.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from mini_ork.vcs import rebase_guard as rg

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


def _g(cwd: Path | str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **_ENV})
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def _scenario(root: Path, kind: str) -> tuple[Path, Path, str]:
    """Build a repo + worktree branch ``feat`` off main, per ``kind``:

    fresh          - worktree freshly branched, main untouched (no rebase needed)
    dirty          - worktree has an uncommitted tracked change; main also advanced
    no_main        - repo's default branch is renamed off "main" (rev-parse main fails)
    no_overlap     - branch and main touch disjoint files -> auto-resolves cleanly
    overlap_clean  - both touch shared.txt on different lines -> rebases cleanly
    conflict       - both touch shared.txt on the same line -> rebase conflicts
    mixed_overlap  - each side also touches a private file (overlap plus disjoint)

    Returns (repo, worktree, run_dir).
    """
    repo = root / "repo"
    repo.mkdir(parents=True)
    _g(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    (repo / "shared.txt").write_text("l1\nl2\nl3\nl4\nl5\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "init")

    if kind == "no_main":
        _g(repo, "branch", "-m", "main", "trunk")
        wt = root / "wt"
        _g(repo, "worktree", "add", "-q", "-b", "feat", str(wt), "trunk")
        return repo, wt, str(root / "run")

    wt = root / "wt"
    _g(repo, "worktree", "add", "-q", "-b", "feat", str(wt), "main")

    if kind == "fresh":
        return repo, wt, str(root / "run")

    if kind == "dirty":
        (wt / "shared.txt").write_text("dirty edit\nl2\nl3\nl4\nl5\n")  # uncommitted tracked
        (repo / "main_only.txt").write_text("x\n")  # main still advances
        _g(repo, "add", "-A")
        _g(repo, "commit", "-qm", "m")
        return repo, wt, str(root / "run")

    if kind == "conflict":
        (wt / "shared.txt").write_text("BRANCH\nl2\nl3\nl4\nl5\n")        # top line
    elif kind == "overlap_clean":
        (wt / "shared.txt").write_text("l1\nl2\nl3\nl4\nBRANCH5\n")       # bottom line
    elif kind == "mixed_overlap":
        (wt / "branch_only.txt").write_text("branch only\n")
        (wt / "shared.txt").write_text("l1\nl2\nl3\nl4\nBRANCH5\n")       # bottom line
    else:  # no_overlap
        (wt / "branch.txt").write_text("branch work\n")
    _g(wt, "add", "-A")
    _g(wt, "commit", "-qm", "branch work")

    if kind == "conflict":
        (repo / "shared.txt").write_text("MAIN\nl2\nl3\nl4\nl5\n")        # same top line -> conflict
    elif kind == "overlap_clean":
        (repo / "shared.txt").write_text("MAIN1\nl2\nl3\nl4\nl5\n")       # top line -> merges w/ branch's bottom
    elif kind == "mixed_overlap":
        (repo / "main_only.txt").write_text("main only\n")
        (repo / "shared.txt").write_text("MAIN1\nl2\nl3\nl4\nl5\n")       # top line -> merges w/ branch's bottom
    elif kind == "no_overlap":
        (repo / "main_only.txt").write_text("main work\n")               # disjoint file
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "main advance")
    return repo, wt, str(root / "run")


def _decision(run_dir: str) -> dict | None:
    p = Path(run_dir) / "rebase-decision.json"
    return json.loads(p.read_text()) if p.exists() else None


class TestEarlyReturns:
    """Paths that must never reach the rebase/decision-write logic."""

    def test_fresh_branch_no_rebase(self, tmp_path: Path) -> None:
        repo, wt, run_dir = _scenario(tmp_path, "fresh")
        rc = rg.rebase_branch_onto_main("e", str(wt), repo_root=str(repo), run_dir=run_dir)
        assert rc == 0
        assert _decision(run_dir) is None

    def test_no_main_branch_returns_zero(self, tmp_path: Path) -> None:
        # `git rev-parse main` fails -> main_sha empty -> early return, no decision.
        repo, wt, run_dir = _scenario(tmp_path, "no_main")
        rc = rg.rebase_branch_onto_main("e", str(wt), repo_root=str(repo), run_dir=run_dir)
        assert rc == 0
        assert _decision(run_dir) is None

    def test_dirty_worktree_skips(self, tmp_path: Path) -> None:
        repo, wt, run_dir = _scenario(tmp_path, "dirty")
        rc = rg.rebase_branch_onto_main("e", str(wt), repo_root=str(repo), run_dir=run_dir)
        assert rc == 2
        assert _decision(run_dir) is None  # returns before any decision write

    def test_skip_autorebase_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo, wt, run_dir = _scenario(tmp_path, "no_overlap")
        monkeypatch.setenv("MO_SKIP_AUTOREBASE", "1")
        rc = rg.rebase_branch_onto_main("e", str(wt), repo_root=str(repo), run_dir=run_dir)
        assert rc == 0
        assert _decision(run_dir) is None  # short-circuits before touching git at all


class TestRebaseDecisionShape:
    @pytest.mark.parametrize("kind,exp_decision,exp_rc,exp_overlap", [
        ("no_overlap", "no_overlap_auto", 0, []),
        ("overlap_clean", "overlap_attempted", 0, ["shared.txt"]),
        ("conflict", "conflict_aborted", 1, ["shared.txt"]),
        ("mixed_overlap", "overlap_attempted", 0, ["shared.txt"]),
    ])
    def test_decision_json_matches_outcome(
        self, tmp_path: Path, kind: str, exp_decision: str, exp_rc: int, exp_overlap: list[str]
    ) -> None:
        repo, wt, run_dir = _scenario(tmp_path, kind)
        rc = rg.rebase_branch_onto_main("epic1", str(wt), "dispatch", repo_root=str(repo), run_dir=run_dir)
        assert rc == exp_rc
        d = _decision(run_dir)
        assert d is not None
        assert d["decision"] == exp_decision
        assert d["overlap_files"] == exp_overlap
        assert d["branch_files"] == sorted(set(d["branch_files"]))
        assert d["main_files"] == sorted(set(d["main_files"]))

    def test_no_overlap_full_shape(self, tmp_path: Path) -> None:
        repo, wt, run_dir = _scenario(tmp_path, "no_overlap")
        rg.rebase_branch_onto_main("epic1", str(wt), repo_root=str(repo), run_dir=run_dir)
        assert _decision(run_dir) == {
            "branch_files": ["branch.txt"],
            "main_files": ["main_only.txt"],
            "overlap_files": [],
            "decision": "no_overlap_auto",
        }

    def test_mixed_overlap_full_shape(self, tmp_path: Path) -> None:
        repo, wt, run_dir = _scenario(tmp_path, "mixed_overlap")
        rg.rebase_branch_onto_main("epic1", str(wt), repo_root=str(repo), run_dir=run_dir)
        assert _decision(run_dir) == {
            "branch_files": ["branch_only.txt", "shared.txt"],
            "main_files": ["main_only.txt", "shared.txt"],
            "overlap_files": ["shared.txt"],
            "decision": "overlap_attempted",
        }


class TestRebaseOutcomeOnDisk:
    """Verify the port actually drove git to the right resulting state."""

    def test_no_overlap_auto_resolves(self, tmp_path: Path) -> None:
        repo, wt, run_dir = _scenario(tmp_path, "no_overlap")
        rc = rg.rebase_branch_onto_main("epic1", str(wt), repo_root=str(repo), run_dir=run_dir)
        assert rc == 0
        # rebase actually happened: worktree now carries both branch and main work
        assert (wt / "branch.txt").exists()
        assert (wt / "main_only.txt").exists()
        assert _g(wt, "symbolic-ref", "--short", "HEAD") == "feat"

    def test_overlap_clean_merges_both_edits(self, tmp_path: Path) -> None:
        repo, wt, run_dir = _scenario(tmp_path, "overlap_clean")
        rc = rg.rebase_branch_onto_main("epic1", str(wt), repo_root=str(repo), run_dir=run_dir)
        assert rc == 0
        lines = (wt / "shared.txt").read_text().splitlines()
        assert lines[0] == "MAIN1"    # main's edit
        assert lines[-1] == "BRANCH5"  # branch's edit, preserved post-rebase

    def test_mixed_overlap_all_files_present(self, tmp_path: Path) -> None:
        repo, wt, run_dir = _scenario(tmp_path, "mixed_overlap")
        rc = rg.rebase_branch_onto_main("epic1", str(wt), repo_root=str(repo), run_dir=run_dir)
        assert rc == 0
        assert (wt / "branch_only.txt").exists()
        assert (wt / "main_only.txt").exists()
        lines = (wt / "shared.txt").read_text().splitlines()
        assert lines[0] == "MAIN1"
        assert lines[-1] == "BRANCH5"

    def test_conflict_aborts_and_restores_branch(self, tmp_path: Path) -> None:
        repo, wt, run_dir = _scenario(tmp_path, "conflict")
        original_head = _g(wt, "rev-parse", "HEAD")
        original_content = (wt / "shared.txt").read_text()
        rc = rg.rebase_branch_onto_main("epic1", str(wt), repo_root=str(repo), run_dir=run_dir)
        assert rc == 1
        # rebase was aborted: back on the original branch tip, nothing left mid-rebase
        assert _g(wt, "symbolic-ref", "--short", "HEAD") == "feat"
        assert _g(wt, "rev-parse", "HEAD") == original_head
        assert (wt / "shared.txt").read_text() == original_content


class TestNameList:
    def test_sorts_and_dedupes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rg, "_git", lambda cwd, *a, **k: ("b.txt\na.txt\na.txt", 0))
        assert rg._name_list("wt", "main...HEAD") == ["a.txt", "b.txt"]

    def test_nonzero_rc_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rg, "_git", lambda cwd, *a, **k: ("a.txt", 1))
        assert rg._name_list("wt", "x") == []

    def test_empty_output_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rg, "_git", lambda cwd, *a, **k: ("", 0))
        assert rg._name_list("wt", "x") == []


class TestWriteRebaseDecision:
    def test_writes_expected_json_and_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "rebase-decision.json"
        rg._write_rebase_decision(str(path), ["a.txt"], ["b.txt"], [], "no_overlap_auto")
        assert json.loads(path.read_text()) == {
            "branch_files": ["a.txt"],
            "main_files": ["b.txt"],
            "overlap_files": [],
            "decision": "no_overlap_auto",
        }


class TestIdentityEnv:
    def test_uses_ambient_identity_without_querying_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "amb@e")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "amb@e")

        def _boom(*a: object, **k: object) -> object:
            raise AssertionError("should not query git config when ambient identity is present")

        monkeypatch.setattr(rg.subprocess, "run", _boom)
        env = rg._identity_env("wt")
        assert env["GIT_AUTHOR_EMAIL"] == "amb@e"
        assert env["GIT_COMMITTER_EMAIL"] == "amb@e"

    def test_falls_back_when_no_ambient_and_no_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
        monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        monkeypatch.setattr(rg.subprocess, "run", lambda *a, **k: fake)
        env = rg._identity_env("wt")
        assert env["GIT_AUTHOR_NAME"] == "mini-ork"
        assert env["GIT_AUTHOR_EMAIL"] == "mini-ork@localhost"
        assert env["GIT_COMMITTER_NAME"] == "mini-ork"
        assert env["GIT_COMMITTER_EMAIL"] == "mini-ork@localhost"

    def test_no_fallback_when_git_config_has_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
        monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="configured@example.com\n", stderr="")
        monkeypatch.setattr(rg.subprocess, "run", lambda *a, **k: fake)
        env = rg._identity_env("wt")
        assert "GIT_AUTHOR_EMAIL" not in env
        assert "GIT_COMMITTER_EMAIL" not in env


class TestWriteStaleBaseMarker:
    def test_content_matches_expected_note(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        path = rg.write_stale_base_marker("epicX", "2", run_dir=str(run_dir))
        assert path == str(run_dir / "iter-2" / "stale-base.note")
        text = Path(path).read_text()
        assert text == rg._STALE_BASE_NOTE
        assert "scope violations" in text

    def test_creates_nested_iter_dir(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        rg.write_stale_base_marker("e", 5, run_dir=str(run_dir))
        assert (run_dir / "iter-5").is_dir()
