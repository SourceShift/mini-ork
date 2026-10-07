"""Changed-file paths resolve to a real path (kickoff ``files-abs-resolve``).

Two defects are covered here:

* ``board_cmd._project_file`` mapped a RELATIVE changed-file path onto the
  project by trying its tails only (``range(1, len(parts))``), so
  ``mini_ork/ide_pages/x.py`` — whose head is the project itself — never
  resolved and came back as a bare name with ``abs=None``.
* ``node_changes._project_file_lazy`` resolved against the main project only,
  so a file the run created that main does not have yet (its worktree has not
  merged) got no path, although ``runs/<id>/run_profile.json`` records the
  run's ``target_repo``.

All fixtures are temp dirs; the process cwd is deliberately moved somewhere
else for the relative-path cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.cli.board_cmd import _project_file
from mini_ork.ide_pages.node_changes import (
    _TARGET_DIRS_CACHE,
    _files_and_note,
    _project_file_lazy,
    _resolve_finding_path,
    _run_target_dirs,
)
from mini_ork.ide_pages.run import Run
from mini_ork.workspaces import Workspace


@pytest.fixture(autouse=True)
def _clear_target_dirs_cache():
    """``_run_target_dirs`` memoises by run dir — keep tests independent."""
    _TARGET_DIRS_CACHE.clear()
    yield
    _TARGET_DIRS_CACHE.clear()


def _make_run(tmp_path: Path, *, run_profile: dict | None, workspace: Workspace | None):
    """A minimal real ``Run``: home/run_dir enough for path resolution."""
    project = tmp_path / "proj"
    home = project / ".mini-ork"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    if run_profile is not None:
        (run_dir / "run_profile.json").write_text(json.dumps(run_profile))
    return Run(
        id="run-1",
        home=home,
        run_dir=run_dir,
        row={},
        card={},
        nodes=[],
        cols=[],
        calls=[],
        recipe_dir=None,
        workspace=workspace,
    )


# ── _project_file: relative paths resolve against the project root ──────────


def test_relative_path_resolves_against_project_from_another_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative path is tried as ``project / path`` first (start 0)."""
    project = tmp_path / "proj"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "mod.py").write_text("x = 1\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    display, absolute = _project_file("pkg/mod.py", project)
    assert display == "pkg/mod.py"
    assert absolute == str(project / "pkg" / "mod.py")


def test_absolute_path_tail_maps_onto_project(tmp_path: Path) -> None:
    """An absolute worktree path whose tail exists in the project is unchanged."""
    project = tmp_path / "proj"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "mod.py").write_text("x = 1\n")
    worktree = tmp_path / "wt"
    (worktree / "pkg").mkdir(parents=True)
    (worktree / "pkg" / "mod.py").write_text("x = 2\n")

    display, absolute = _project_file(str(worktree / "pkg" / "mod.py"), project)
    assert display == "pkg/mod.py"
    assert absolute == str(project / "pkg" / "mod.py")


def test_relative_path_missing_everywhere_keeps_its_display(tmp_path: Path) -> None:
    """A miss returns the relative path as given — never just its basename."""
    project = tmp_path / "proj"
    (project / "pkg").mkdir(parents=True)

    display, absolute = _project_file("pkg/new.py", project)
    assert display == "pkg/new.py"
    assert absolute is None


# ── _run_target_dirs: workspace, then run_profile target_repo / roots ───────


def test_run_target_dirs_reads_profile_and_prefers_workspace(tmp_path: Path) -> None:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    run = _make_run(
        tmp_path,
        run_profile={"target_repo": str(target), "roots": {"target": str(target)}},
        workspace=Workspace(
            run_id="run-1", path=ws_dir, branch="wt/x", base_branch="main",
            base_sha="deadbeef", project=tmp_path,
        ),
    )
    dirs = _run_target_dirs(run)
    # ws first, dedupe of the twice-recorded target, missing keys skipped.
    assert dirs == [ws_dir, target]


def test_run_target_dirs_skips_missing_file(tmp_path: Path) -> None:
    run = _make_run(tmp_path, run_profile=None, workspace=None)
    assert _run_target_dirs(run) == []


# ── _files_and_note / _project_file_lazy: run-target fallback ───────────────


def test_files_and_note_resolves_file_from_target_repo(tmp_path: Path) -> None:
    """A file main lacks but the run's ``target_repo`` has → abs is that file."""
    target = tmp_path / "target"
    (target / "pkg").mkdir(parents=True)
    (target / "pkg" / "new_mod.py").write_text("x = 1\n")
    run = _make_run(tmp_path, run_profile={"target_repo": str(target)}, workspace=None)

    files, note = _files_and_note(
        run, True, [{"path": "pkg/new_mod.py", "added": 1, "removed": 0}], "patch"
    )
    assert note == ""
    assert len(files) == 1
    assert files[0]["path"] == "pkg/new_mod.py"
    assert files[0]["abs"] == str(target / "pkg" / "new_mod.py")


def test_workspace_path_wins_over_target_repo(tmp_path: Path) -> None:
    """When both dirs hold the file, the workspace record wins (dirs ordered)."""
    ws_dir = tmp_path / "ws"
    (ws_dir / "pkg").mkdir(parents=True)
    (ws_dir / "pkg" / "mod.py").write_text("from ws\n")
    target = tmp_path / "target"
    (target / "pkg").mkdir(parents=True)
    (target / "pkg" / "mod.py").write_text("from target\n")
    run = _make_run(
        tmp_path,
        run_profile={"target_repo": str(target)},
        workspace=Workspace(
            run_id="run-1", path=ws_dir, branch="wt/x", base_branch="main",
            base_sha="deadbeef", project=tmp_path,
        ),
    )

    display, absolute = _project_file_lazy("pkg/mod.py", run.home.absolute().parent, run)
    assert display == "pkg/mod.py"
    assert absolute == str(ws_dir / "pkg" / "mod.py")


def test_finding_resolver_agrees_with_file_list(tmp_path: Path) -> None:
    """The finding resolver uses the same ``_run_target_dirs`` lookup."""
    target = tmp_path / "target"
    (target / "pkg").mkdir(parents=True)
    (target / "pkg" / "mod.py").write_text("x = 1\n")
    run = _make_run(tmp_path, run_profile={"target_repo": str(target)}, workspace=None)

    assert _resolve_finding_path("pkg/mod.py", run) == str(target / "pkg" / "mod.py")


# ── ordering: run-target dirs win over a project basename tail match ────────


def test_new_file_not_hijacked_by_project_root_basename(tmp_path: Path) -> None:
    """A run-created ``docs/new/README.md`` must NOT collapse onto the
    project's root ``README.md`` (same basename).

    The project tail match would resolve the basename at the project root;
    the run's own ``target_repo`` copy is the authority. The files list and
    the finding resolver must agree on that same absolute file.
    """
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    (project / "README.md").write_text("# project\n")  # basename collision
    target = tmp_path / "target"
    (target / "docs" / "new").mkdir(parents=True)
    (target / "docs" / "new" / "README.md").write_text("# new\n")
    run = _make_run(tmp_path, run_profile={"target_repo": str(target)}, workspace=None)

    files, note = _files_and_note(
        run, True, [{"path": "docs/new/README.md", "added": 1, "removed": 0}], "patch"
    )
    assert note == ""
    assert len(files) == 1
    # Full relative path, not the bare basename the project tail match gave.
    assert files[0]["path"] == "docs/new/README.md"
    assert files[0]["abs"] == str(target / "docs" / "new" / "README.md")
    # The finding resolver points at the SAME file.
    assert _resolve_finding_path("docs/new/README.md", run) == str(
        target / "docs" / "new" / "README.md"
    )


def test_project_exact_hit_wins_over_target_repo(tmp_path: Path) -> None:
    """A relative path that exists at the SAME place in the project is an
    exact hit and wins — the tail match is not consulted."""
    project = tmp_path / "proj"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "mod.py").write_text("from project\n")
    target = tmp_path / "target"
    (target / "pkg").mkdir(parents=True)
    (target / "pkg" / "mod.py").write_text("from target\n")
    run = _make_run(tmp_path, run_profile={"target_repo": str(target)}, workspace=None)

    display, absolute = _project_file_lazy("pkg/mod.py", run.home.absolute().parent, run)
    assert display == "pkg/mod.py"
    assert absolute == str(project / "pkg" / "mod.py")
