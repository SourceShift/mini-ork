# Zed W1 — tasks live in Zed's own worktrees, so Zed's panels show them

## Goal

Zed already has the task UI we want: the Threads Sidebar (one row per thread, with a status
indicator), a worktree picker in the title bar (each thread can run in its own git worktree),
and the Git panel / Review Changes for the worktree a thread is in. mini-ork bypasses all of
it: runs edit a hidden nested worktree (`.mini-ork/worktrees/<run>`) that Zed never shows, and
a mini-ork thread opened inside a Zed worktree cannot even find the project's `.mini-ork`
(it is gitignored, so the worktree has none).

After this slice, one task = one Zed thread in one git worktree:
- A thread started in a Zed linked worktree runs its tasks **in that worktree** — the Git
  panel on the right shows the files and +/− live, and Merge / Discard still work.
- A thread in the main checkout with "New worktree per task" creates the task's worktree
  **where Zed keeps its own** (`git.worktree_directory`, default `../worktrees`, Zed appends
  the project name when that is outside the project), named after the task, so it appears
  in Zed's worktree picker.
- From inside any linked worktree, mini-ork finds the main checkout's `.mini-ork`.

## Files in scope

- `mini_ork/workspaces.py` — worktree location, linked-worktree helpers, adopt.
- `mini_ork/acp/agent.py` — home resolution; direct mode / `/run` / race / recipe test run use
  adopt or the new location; Workspace option labels.
- `mini_ork/mcp_context/server.py` — `start_run` uses the same logic; home from the main checkout.
- `mini_ork/automations.py` — `fire` names its worktree `<automation id>-<run suffix>`.
- `tests/unit/test_workspaces.py`, `tests/unit/test_acp_agent_py.py`,
  `tests/unit/test_mcp_context_server.py`, `tests/unit/test_automations.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `workspaces.py`

```python
def main_checkout(path: Path) -> Path | None      # the repo's main worktree root
def is_linked_worktree(path: Path) -> bool         # git-dir != git-common-dir
def worktree_dir(project: Path) -> Path            # where new task worktrees go
def task_name(title: str, run_id: str) -> str      # "<slug of title, ≤40>-<last 6 of run_id>"
def create(project, home, run_id, *, name: str | None = None) -> Workspace
def adopt(cwd: Path, home: Path, run_id: str) -> Workspace
```

- `main_checkout`: `git rev-parse --path-format=absolute --git-common-dir` → its parent when
  it ends in `.git`; None outside a repo.
- `is_linked_worktree`: `--git-dir` and `--git-common-dir` (absolute) differ.
- `worktree_dir(project)`: `MO_WORKTREE_DIR` if set (absolute, or relative to the main
  checkout); else Zed's `git.worktree_directory` from `<main>/.zed/settings.json`, then the
  user settings (`ZED_SETTINGS`, else `~/.config/zed/settings.json`; parse leniently with
  `mini_ork.cli.zed_cmd._load_settings(path, lenient=True)`), default `"../worktrees"`;
  resolve relative to the main checkout; when the result is outside the main checkout,
  append the main checkout's directory name (Zed's rule). `create` uses
  `worktree_dir(project) / (name or run_id)` instead of `<home>/worktrees/<run_id>`; the
  record stays at `<home>/worktrees/<run_id>.json` (existing records with old paths keep
  working — they store their path).
- `adopt(cwd, home, run_id)` — for a cwd that is a linked worktree: branch = the current
  branch; detached HEAD → `git switch -c mini-ork/<run_id>`; `base_branch` = the main
  checkout's current branch; `base_sha` = HEAD now; `project` = main checkout; record
  `adopted: true` and `clean_at_start` (= `git status --porcelain` empty). Not a linked
  worktree → `RuntimeError`.
- `load` / `list_open` read `adopted`/`clean_at_start` (default False) — add both fields to
  `Workspace` with defaults so existing callers are unaffected.
- `merge` on an adopted workspace: same merge into `base_branch` in the main checkout, but on
  success keep the worktree and the branch (Zed owns the worktree; the branch is checked out
  there) and delete only the record.
- `discard` on an adopted workspace: when `clean_at_start` → `git reset --hard <base_sha>` and
  `git clean -fd` in the worktree, delete the record, `{"ok": true}`; otherwise refuse
  `{"ok": false, "error": "this worktree had changes before the run — discard them in Zed's
  Git panel"}`. Never remove an adopted worktree or delete its branch.

### Home resolution (agent + MCP)

`_home_for` / `_home_for_for_cwd` (agent) and the MCP server's home: when `<cwd>/.mini-ork`
is missing and cwd is a linked worktree, use `<main_checkout>/.mini-ork` when it exists,
before the existing fallbacks.

### Where a run works (agent: direct mode, `/run`, recipe test run, race; MCP `start_run`)

- Workspace mode `worktree` and the thread cwd **is a linked worktree** → `adopt` (race:
  each contestant still gets a NEW worktree via `create` — racing needs one per model).
  Marker title suffix `· this worktree <branch>`.
- `worktree` in the main checkout → `create(project, home, run_id, name=task_name(<task
  title>, run_id))` where the task title is the kickoff's first non-empty line (`#`
  stripped). Marker suffix `· worktree <branch>` as today.
- `in-place` → unchanged.
- Workspace option labels: in a linked worktree the `worktree` value is named `This worktree
  (<directory name>)`; elsewhere unchanged.
- A second run in the same adopted worktree while one is still open → adopt again works on
  the same branch (base_sha = HEAD then); the S5 review flow per run is unchanged.

### Automations

`fire` passes `name=f"{automation_id}-{run_id[-6:]}"` to `create`.

## Tests

`tests/unit/test_workspaces.py` (real temp git repos): `main_checkout` and
`is_linked_worktree` for main/linked/non-repo; `worktree_dir` default (outside → project name
appended), `MO_WORKTREE_DIR`, a project `.zed/settings.json` with comments; `create` puts
the worktree there with the given name; `task_name`; `adopt` on a detached linked worktree
creates `mini-ork/<run>`, on a named branch keeps it; status after an edit; merge of an
adopted workspace lands on the base branch and keeps the worktree + branch; discard resets a
clean-at-start worktree and refuses a dirty-at-start one; adopt outside a linked worktree
raises.

`tests/unit/test_acp_agent_py.py`: a thread whose cwd is a linked worktree of a temp repo
(home only in the main checkout) resolves the main `.mini-ork`; direct mode adopts (launcher
sees `MO_TARGET_CWD` = the linked worktree, no new worktree created); the Workspace option
label; in the main checkout a new worktree lands under the worktree dir with the task name.

`tests/unit/test_mcp_context_server.py`: `start_run` from a linked worktree adopts; from the
main checkout creates under the worktree dir.

`tests/unit/test_automations.py`: `fire` names the worktree `<id>-<suffix>`.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_agent_py.py tests/unit/test_automations.py tests/unit/test_mcp_context_server.py tests/unit/test_workspaces.py tests/unit/test_acp_task_state.py tests/unit/test_acp_race.py` passes.
- `ruff check` clean; diff touches only the files in scope.
