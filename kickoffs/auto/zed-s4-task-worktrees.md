# Zed S4 — one isolated workspace per task: a git worktree + branch for each run

Depends on S3b-2 merged (serial with the Zed slices that edit `mini_ork/acp/agent.py`).

## Goal

Parallel tasks must not trample each other, and every task's change must be reviewable and
mergeable or discardable on its own. Give each run started from a Zed thread its own git
worktree on its own branch: the run edits and commits there (the publisher commits on
reviewer approval, as today), the user's checkout is untouched, and a later slice (S5) adds
Merge / Discard. In-place runs stay available.

## Files in scope

- `mini_ork/workspaces.py` — NEW: create / inspect / merge / discard task worktrees.
- `mini_ork/mcp_context/server.py` — `start_run` gains `workspace`; new read tool `workspaces`.
- `mini_ork/acp/agent.py` — Workspace config option; direct mode / `/run` use it; the MCP
  server for the orchestrator gets the thread's mode.
- `mini_ork/acp_orchestrator/harness.py` — pass `MO_WORKSPACE_MODE` into the MCP server env.
- `tests/unit/test_workspaces.py` — NEW.
- `tests/unit/test_mcp_context_server.py`, `tests/unit/test_acp_agent_py.py`,
  `tests/unit/test_acp_orchestrator.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `workspaces.py`

```python
@dataclass(frozen=True)
class Workspace:
    run_id: str; path: Path; branch: str; base_branch: str; base_sha: str; project: Path

def create(project: Path, home: Path, run_id: str) -> Workspace
def load(home: Path, run_id: str) -> Workspace | None
def list_open(home: Path) -> list[Workspace]
def status(ws: Workspace) -> dict      # {"exists", "commits_ahead", "uncommitted", "added", "removed", "files"}
def merge(ws: Workspace, message: str) -> dict
def discard(ws: Workspace) -> dict
```

- `create`: `project` must be a git repo (`git -C project rev-parse --show-toplevel`; error
  otherwise). Path `<home>/worktrees/<run_id>`, branch `mini-ork/<run_id>`, base = the
  project's current branch name (`git symbolic-ref --short HEAD`; detached → the sha) and
  `HEAD` sha. `git -C project worktree add -b <branch> <path> <base_sha>`. If
  `<home>/worktree-setup.sh` exists, run it with `cwd=<path>` (timeout 600 s; its failure
  is reported in the result but does not delete the worktree) — the place for copying `.env`,
  installing deps, etc. Record `<home>/worktrees/<run_id>.json` (all Workspace fields as
  strings). Errors → `RuntimeError` with git's stderr.
- `load` / `list_open`: from the records whose worktree directory still exists.
- `status`: commits on the branch not on `base_sha` (`git rev-list --count base..branch`),
  uncommitted changes in the worktree (`git status --porcelain`), and the total diff vs
  `base_sha` including uncommitted changes (`git diff --numstat <base_sha>` in the worktree):
  added/removed totals and per-file rows. A missing worktree → `{"exists": false}`.
- `merge`: in the worktree, if uncommitted changes exist: `git add -A` then commit with
  `message` (author from the repo config; if none, `-c user.name=mini-ork -c
  user.email=mini-ork@localhost`). Then in the project: refuse with `{"ok": false,
  "error": ...}` when the project's current branch is not `base_branch`, or when the
  project has uncommitted changes to any file the branch changes (`git diff --name-only
  base_sha branch` ∩ `git status --porcelain` paths). Otherwise `git merge --ff-only
  <branch>`; if not fast-forwardable, `git merge --no-ff -m "<message>" <branch>`; a
  conflict → `git merge --abort` and `{"ok": false, "error": "conflicts in: …"}`. On
  success remove the worktree (`git worktree remove --force`), delete the branch
  (`git branch -D`), delete the record; return `{"ok": true, "merged": <sha>, "mode":
  "fast-forward"|"merge"}`. Nothing to merge (no commits, no changes) → discard and
  `{"ok": true, "merged": null}`.
- `discard`: remove the worktree (force), delete the branch, delete the record;
  `{"ok": true}` (also when parts are already gone).
- All git calls via one module-level `_git(args, cwd, timeout=120)` seam returning
  `(rc, stdout, stderr)`.

### Launching into a workspace

- MCP `start_run` gains `workspace` (`"worktree"` | `"in-place"`, default: env
  `MO_WORKSPACE_MODE`, else `"worktree"`). For `worktree`: mint the run id first with
  `mini_ork.acp.agent.mint_run_id()` (or an equivalent local helper with the same format),
  `create(project, home, run_id)`, then `control.launch_run(home, recipe, kickoff,
  run_id=run_id, extra_env={"MO_TARGET_CWD": str(ws.path)})` — `launch_run` already takes
  `run_id` and `extra_env`. Not a git repo → in-place, with `"note"` in the result. Result
  gains `workspace`, `worktree`, `branch`.
- New read-only MCP tool `workspaces` → `list_open(home)` each with `status`.
- `agent.py`: a fourth config option `workspace` (no category; values `worktree` /
  `in-place`, names "New worktree per task" / "In place (this checkout)"; default
  `MO_WORKSPACE_MODE` or `worktree`), stored per thread like the others, persisted with the
  thread config. Direct mode and `/run` create the worktree (when `worktree` and the project
  is a git repo) before calling the launcher, passing `MO_TARGET_CWD` through the launcher's
  existing env mechanism (read `_launch` / `launch_run`; add a minimal pass-through if
  needed). The run marker title gains ` · worktree mini-ork/<run_id>` when isolated.
- The orchestrator's MCP server gets the thread's mode: `harness.run_turn(...,
  extra_mcp_env={"MO_WORKSPACE_MODE": mode})` → added to the MCP server's `env` in the
  temp MCP config.

## Tests

`tests/unit/test_workspaces.py` (real temp git repos — `git init`, a commit; set
`user.name`/`user.email` in the temp repo): create (path, branch, record, setup script run
and failure reported), not-a-repo error; status with a commit and with uncommitted edits;
merge fast-forward, merge with a non-ff base (commit on base after create → `--no-ff`),
conflict → aborted and reported, refused when the project has a dirty overlapping file,
refused when the project is on another branch, nothing to merge → discarded; discard; list
after merge/discard is empty.

`tests/unit/test_mcp_context_server.py`: `start_run` with `workspace=worktree` creates the
worktree and passes `MO_TARGET_CWD` (launch_run monkeypatched); in-place passes the project
root; a non-git project falls back with a note; `workspaces` lists it.

`tests/unit/test_acp_agent_py.py`: the workspace option exists with its default; direct
mode with `worktree` creates one (workspaces.create monkeypatched) and passes its path to
the launcher; `in-place` does not.

`tests/unit/test_acp_orchestrator.py`: `run_turn` writes `MO_WORKSPACE_MODE` into the MCP
server env.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_workspaces.py tests/unit/test_mcp_context_server.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_orchestrator.py tests/unit/test_acp_commands.py` passes.
- `ruff check` clean; diff touches only the files in scope.
