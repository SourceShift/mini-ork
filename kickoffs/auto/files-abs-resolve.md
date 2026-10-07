# Changed files resolve to a real path, so "Open" works and the label shows the repo path

## Why

In the IDE's node Overview / Changes FILES list, rows of a real run showed bare names
(`board_cmd.py`, `node.py`, `node_artifacts.py`, …) and had no `abs`, so "Open" was missing.
Two causes:

1. **`mini_ork/cli/board_cmd.py:380` `_project_file(path, project)`** maps a changed-file path
   onto the project by trying tails of the path, but the loop is `for start in range(1,
   len(parts))`. It never tries the path itself (start 0). A relative path such as
   `mini_ork/ide_pages/node_artifacts.py` tries only `ide_pages/node_artifacts.py` and
   `node_artifacts.py`, finds neither, and returns `(p.name, None)`: a bare name and no path,
   even though `<project>/mini_ork/ide_pages/node_artifacts.py` exists. The `p.is_file()` shortcut
   just above only works when the process cwd happens to be the project, and then
   `relative_to(project)` fails for a relative `p`.
2. **`mini_ork/ide_pages/node_changes.py` `_files_and_note`** (via `_project_file_lazy`, :922)
   resolves only against the main project. A file the run created that main doesn't have yet
   (its worktree hasn't merged) gets no path, although it exists in the run's own target repo.
   `runs/<id>/run_profile.json` records it: `"target_repo": "/…/mini-ork-worktrees/<slug>"` and
   `"roots": {"target": …, "exec_cwd": …}`. The finding resolver at `node_changes.py:596-622`
   already tries `run.workspace.path`, but not `target_repo`.

## Files in scope (touch ONLY these)

- `mini_ork/cli/board_cmd.py`: ONLY `_project_file`
- `mini_ork/ide_pages/node_changes.py`: ONLY `_files_and_note`, `_project_file_lazy` and the
  finding-path resolver near :596 (share one helper)
- `tests/unit/test_files_abs_resolve.py` (new)

Do NOT modify any other file.

## Fix (exact)

1. **`_project_file`:** for a RELATIVE path, try `project / path` first (start 0), then the tails
   as today. For an absolute path, behaviour is unchanged. When nothing resolves, return the
   relative path as given for the display (not `p.name`). Only an absolute path that resolves
   nowhere keeps today's `p.name` display.
2. **`node_changes._run_target_dirs(run) -> list[Path]`** (new, ordered, existing dirs only):
   `run.workspace.path` (if any), `run_profile.json` `target_repo`, `roots.target`,
   `roots.exec_cwd`. Missing file or keys → skip. Cache per `run.run_dir`.
3. **`_project_file_lazy(path, project, run=None)`:** when the project lookup gives no absolute
   path, try each `_run_target_dirs(run)` dir: the path as given if relative, else its tails. On
   a hit: `abs` = that file, display = the path relative to that dir. `_files_and_note` passes
   `run`.
4. The finding resolver (:596) uses the same `_run_target_dirs` after the project lookup, so
   both lists agree.

## Tests (`tests/unit/test_files_abs_resolve.py`, temp dirs)

- `_project_file("pkg/mod.py", project)` with `project/pkg/mod.py` present →
  `("pkg/mod.py", "<project>/pkg/mod.py")`, with the process cwd somewhere else (monkeypatch
  chdir).
- An absolute worktree path whose tail exists in the project → unchanged behaviour.
- A relative path missing everywhere → display stays `"pkg/new.py"` (not `new.py`), abs None.
- `_files_and_note` for a run whose `run_profile.json` has `target_repo` = a temp dir containing
  the file (missing from the project) → abs = the target_repo file, display = the relative path.
- `run.workspace.path` wins over `target_repo` when both have the file.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_files_abs_resolve.py tests/unit/test_ide_pages_node_changes.py tests/unit/test_board_cmd.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/cli/board_cmd.py mini_ork/ide_pages/node_changes.py tests/unit/test_files_abs_resolve.py` → clean.
- Live proof (read-only): `build_node(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "node-artifacts-20261007165902", "publisher", view="overview")["files"]`.
  Every row has a full repo-relative `path` and a non-empty `abs`. Paste it.
- `git diff --stat` touches only the files in scope.
