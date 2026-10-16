"""Unit tests for the base worktree's dependency overlay.

The delta gate replays the suite on a ``git worktree add --detach HEAD`` base.
That worktree carries the source but none of the gitignored dependency
directories, so a JS (or venv'd Python) repo's runner dies before executing a
single test. jest reports the missing module as a *configuration* error with
rc=1 — not one of ``_UNRUNNABLE_RC``'s shell codes {126,127,77} — so
``_opaque_delta`` read the base log as "its output carries no test-run marker"
and parked the run UNVERIFIED with a reason naming the wrong cause. Measured on
the researcher home: 4 code-fix runs in 48h (~$30), every one with a *green*
candidate suite (653/653 passing).

These tests pin the overlay that makes the base runnable: the candidate's own
dependency dirs are symlinked — never copied, so the disk cost stays zero —
onto the base before either the replay or the baseline runs.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"


@pytest.fixture(scope="module")
def verifier():
    """Load the verifier as a module.

    It is a script, not a package member, so it is loaded from its path; its
    top-level work is guarded by ``if __name__ == "__main__"``.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("mo_codefix_verifier_under_test", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(tmp_path: Path, *dirs: str) -> Path:
    """A stand-in candidate tree holding the named dependency directories."""
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for rel in dirs:
        target = candidate / rel
        target.mkdir(parents=True)
        (target / "marker.txt").write_text(rel)
    return candidate


def test_links_the_candidate_dependency_dirs(verifier, tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, "node_modules")
    base = tmp_path / "base"
    base.mkdir()

    assert verifier._link_candidate_dependencies(str(base), str(candidate)) == ["node_modules"]

    link = base / "node_modules"
    assert os.path.islink(link)
    assert os.readlink(link) == str(candidate / "node_modules")
    # The point of the link: the base can now resolve a module it never had.
    assert (link / "marker.txt").read_text() == "node_modules"


def test_link_is_idempotent(verifier, tmp_path: Path) -> None:
    """A second call must not relink — both call sites share one worktree."""
    candidate = _candidate(tmp_path, "node_modules")
    base = tmp_path / "base"
    base.mkdir()

    assert verifier._link_candidate_dependencies(str(base), str(candidate)) == ["node_modules"]
    assert verifier._link_candidate_dependencies(str(base), str(candidate)) == []


def test_missing_dependency_dirs_are_not_linked(verifier, tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)  # no dependency dir at all
    base = tmp_path / "base"
    base.mkdir()

    assert verifier._link_candidate_dependencies(str(base), str(candidate)) == []
    assert os.listdir(base) == []


def test_a_real_directory_in_the_base_is_never_clobbered(verifier, tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, "node_modules")
    base = tmp_path / "base"
    (base / "node_modules").mkdir(parents=True)
    (base / "node_modules" / "root.txt").write_text("base")

    assert verifier._link_candidate_dependencies(str(base), str(candidate)) == []
    assert not os.path.islink(base / "node_modules")
    assert (base / "node_modules" / "root.txt").read_text() == "base"


def test_nested_dependency_dir_parent_is_created(verifier, tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, "vendor/bundle")
    base = tmp_path / "base"
    base.mkdir()

    assert verifier._link_candidate_dependencies(str(base), str(candidate)) == ["vendor/bundle"]
    assert os.path.islink(base / "vendor" / "bundle")


def _git(cwd: Path, *args: str) -> None:
    subprocess.check_call(
        ["git", *args], cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_repo(parent: Path) -> Path:
    repo = parent / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_attached_base_can_run_a_command_that_needs_the_dependency_dir(
    verifier, tmp_path: Path,
) -> None:
    """The end the overlay exists for: a base that could not run, now can.

    The stand-in command fails exactly like jest does without its deps — its
    output carries no test-run marker — and succeeds once ``node_modules`` is
    reachable from the base worktree.
    """
    repo = _make_repo(tmp_path)
    (repo / "node_modules").mkdir()
    command = "bash -c 'test -d node_modules && echo \"2 passed\"'"

    def run_in(cwd: Path) -> str:
        return subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, check=False,
        ).stdout

    assert run_in(repo) == "2 passed\n", "the candidate must be able to run the command"

    base_wt, _ = verifier._attach_overlaid_worktree(str(repo), "")
    assert base_wt is not None
    try:
        assert run_in(Path(base_wt)) == "2 passed\n", (
            "the base worktree must be able to run the same command, or the delta "
            "gate abstains on every deps-needing repo"
        )
    finally:
        verifier._detach_git_worktree(base_wt)

    assert not os.path.exists(base_wt), "the worktree and its link must be cleaned up"
