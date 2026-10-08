"""The code-fix typecheck verifier must check the run's OWN change surface.

Regression (issue #4, observed live): ``MINI_ORK_TYPECHECK_CMD`` was armed with
the project's repo-wide ``type-check:full``, so a red main rolled back every
lane for diagnostics the child never caused. The same class of error sits in
the touched-file computation: ``git diff --name-only origin/main`` misses
untracked new files and, once origin/main moves, drags in OTHER sessions' files.

Two contracts are pinned here:
  * ``touched_files`` = working tree ∪ untracked ∪ ``base...HEAD``, with the
    base being the run's own start point — never a branch ref that other
    sessions move.
  * a tool this module detected itself (tsc / mypy) is narrowed to those files;
    an operator-supplied command is run verbatim, and an unscopable tool runs
    unscoped rather than being silently skipped.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

VERIFIER = (
    Path(__file__).resolve().parents[2]
    / "recipes"
    / "code-fix"
    / "verifiers"
    / "typecheck.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("codefix_typecheck", VERIFIER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    """Load the verifier with its import-time log dir pointed at tmp."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / ".mini-ork"))
    monkeypatch.delenv("MINI_ORK_TYPECHECK_CMD", raising=False)
    monkeypatch.delenv("MINI_ORK_TYPECHECK_FULL", raising=False)
    monkeypatch.delenv("MO_GOAL_SCOPED_BASE", raising=False)
    return _load()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    )
    return proc.stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "base.ts").write_text("export const a = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")


def _repo_with_lane_and_moved_origin(repo: Path) -> tuple[str, str]:
    """A lane branch with one commit and one untracked file, on top of a base,
    while a second session advances ``origin/main`` past that base."""
    _init_repo(repo)
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-q", "-b", "lane")
    (repo / "lane.ts").write_text("export const b = 2\n", encoding="utf-8")
    _git(repo, "add", "lane.ts")
    _git(repo, "commit", "-qm", "lane work")
    lane_sha = _git(repo, "rev-parse", "HEAD").strip()

    # The child also leaves a new, never-committed file behind.
    (repo / "new-untracked.ts").write_text("export const c = 3\n", encoding="utf-8")

    # A DIFFERENT session pushes to origin/main: a commit the lane never saw.
    _git(repo, "checkout", "-q", "-b", "other", base_sha)
    (repo / "other-session.ts").write_text("export const z = 9\n", encoding="utf-8")
    _git(repo, "add", "other-session.ts")  # never the lane's untracked file
    _git(repo, "commit", "-qm", "other session work")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "lane")

    return base_sha, lane_sha


def _write_pre_impl_ref(tmp_path: Path, monkeypatch, sha: str) -> None:
    home = tmp_path / ".mini-ork"
    run_id = "codefix-1789911252-19277"
    (home / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    (home / "runs" / run_id / "pre-implementer-ref").write_text(sha + "\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)


# ── touched_files ───────────────────────────────────────────────────────────


def test_touched_covers_untracked_and_ignores_a_moved_origin_main(mod, tmp_path):
    """The peer's acceptance case: a moving origin/main plus an untracked file."""
    repo = tmp_path / "repo"
    _repo_with_lane_and_moved_origin(repo)

    touched = mod.touched_files(str(repo))
    assert "lane.ts" in touched
    assert "new-untracked.ts" in touched
    # The other session's commit is on origin/main, NOT this lane's divergence.
    assert "other-session.ts" not in touched


def test_pre_implementer_ref_is_the_base_not_origin_main(mod, tmp_path, monkeypatch):
    """The run's own start point wins: work committed BEFORE it is not "touched"."""
    repo = tmp_path / "repo"
    _base_sha, lane_sha = _repo_with_lane_and_moved_origin(repo)
    _write_pre_impl_ref(tmp_path, monkeypatch, lane_sha)

    assert mod._scoped_base(str(repo)) == lane_sha
    touched = mod.touched_files(str(repo))
    # lane.ts is already IN the base, so it is not part of this run's change.
    assert "lane.ts" not in touched
    assert "new-untracked.ts" in touched


def test_garbage_pre_implementer_ref_falls_back_to_merge_base(mod, tmp_path, monkeypatch):
    """A malformed ref must never be handed to git as a revision argument."""
    repo = tmp_path / "repo"
    _base_sha, _lane_sha = _repo_with_lane_and_moved_origin(repo)
    _write_pre_impl_ref(tmp_path, monkeypatch, "HEAD; rm -rf /\n")

    base = mod._scoped_base(str(repo))
    assert base and base != "HEAD; rm -rf /"
    assert "other-session.ts" not in mod.touched_files(str(repo))


def test_run_artifacts_never_enter_the_scope(mod, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".mini-ork" / "runs" / "r1").mkdir(parents=True)
    (repo / ".mini-ork" / "runs" / "r1" / "plan.json").write_text("{}", encoding="utf-8")
    assert mod.touched_files(str(repo)) == []


# ── _scoped_command ─────────────────────────────────────────────────────────


def test_tsc_scopes_through_an_overlay_that_extends_the_project_config(mod, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}', encoding="utf-8")

    cmd, note, overlay = mod._scoped_command(
        str(repo), mod._Detected("tsc --noEmit", "tsc", "tsc"), ["a.ts", "notes.md"],
    )
    assert overlay is not None and os.path.isfile(overlay)
    assert "-p " in cmd
    cfg = json.loads(Path(overlay).read_text(encoding="utf-8"))
    assert cfg["extends"] == "./tsconfig.json"
    assert cfg["files"] == ["a.ts"]  # .md is not a TS input
    assert "1 touched file(s)" in note
    os.unlink(overlay)


def test_tsc_without_a_project_config_runs_unscoped_not_skipped(mod, tmp_path):
    """No tsconfig to extend -> fall back to the whole-project run, never a skip."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    cmd, note, overlay = mod._scoped_command(
        str(repo), mod._Detected("tsc --noEmit", "tsc", "tsc"), ["a.ts"],
    )
    assert cmd is None and overlay is None
    assert note == ""  # empty note == "unscopable", distinct from "nothing to check"


def test_scope_with_no_matching_extension_is_a_real_nothing_to_check(mod, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    cmd, note, overlay = mod._scoped_command(
        str(repo), mod._Detected("mypy .", "mypy", "mypy"), ["README.md"],
    )
    assert cmd is None and overlay is None
    assert "no mypy files among 1 touched" in note


def test_mypy_scopes_by_appending_the_touched_files(mod, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    cmd, _note, overlay = mod._scoped_command(
        str(repo), mod._Detected("mypy .", "mypy", "mypy"), ["pkg/a.py", "pkg/b.pyi"],
    )
    assert overlay is None
    assert cmd == "mypy pkg/a.py pkg/b.pyi"


# ── main() end to end ───────────────────────────────────────────────────────


def _fake_tool(bindir: Path, name: str) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / name
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)


def test_main_narrows_a_detected_tool_to_touched_files(mod, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "new-untracked.ts").write_text("export const c = 3\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    _fake_tool(bindir, "tsc")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mod, "detect_typecheck", lambda: mod._Detected("tsc --noEmit", "tsc", "tsc"))

    assert mod.main() == 0
    err = capsys.readouterr().err
    assert "scoped to 1 touched file(s)" in err


def test_main_full_flag_opts_out_of_scoping(mod, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "new-untracked.ts").write_text("export const c = 3\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    _fake_tool(bindir, "tsc")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MINI_ORK_TYPECHECK_FULL", "1")
    monkeypatch.setattr(mod, "detect_typecheck", lambda: mod._Detected("tsc --noEmit", "tsc", "tsc"))

    assert mod.main() == 0
    err = capsys.readouterr().err
    assert "running: tsc --noEmit" in err
    assert "scoped to" not in err


def test_main_runs_an_operator_command_verbatim_and_exports_the_touch_set(
    mod, tmp_path, monkeypatch, capsys,
):
    """An explicit command owns its scope; it can read $MINI_ORK_TOUCHED_FILES."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "new-untracked.ts").write_text("export const c = 3\n", encoding="utf-8")
    dump = tmp_path / "env.txt"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MINI_ORK_TYPECHECK_CMD", f"sh -c 'env > {dump}'")

    assert mod.main() == 0
    err = capsys.readouterr().err
    assert "scoped" not in err
    assert "new-untracked.ts" in dump.read_text(encoding="utf-8")
