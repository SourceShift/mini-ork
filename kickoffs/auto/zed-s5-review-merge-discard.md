# Zed S5 — review, merge, discard: a finished task waits for the user's decision

Depends on S4 merged (`mini_ork/workspaces.py`; runs from Zed threads get their own worktree
+ branch). Serial with the other Zed slices.

## Goal

Orchestration without a review surface only moves the bottleneck. When a task's run ends in
its own worktree with changes, the task should *need the user*: the thread list shows
`✋ <task> — ready to review +12 −3`, the run's diff is already in the thread (Z4), and the
user decides — **Merge into <branch>** / **Discard** / **Keep for later** — with buttons
when the run ends inside a turn, or with `/merge` / `/discard` any time. `/workspaces`
lists every open task workspace.

## Files in scope

- `mini_ork/acp/task_state.py` — the "ready to review" state.
- `mini_ork/acp/agent.py` — the decision buttons; the one-time "ready to review" message.
- `mini_ork/acp/commands.py` — `/merge`, `/discard`, `/workspaces`; help text.
- `mini_ork/acp_orchestrator/prompt.md` — one paragraph on review.
- `tests/unit/test_acp_task_state.py`, `tests/unit/test_acp_agent_py.py`,
  `tests/unit/test_acp_commands.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `task_state.py`

Before the `published` → done rule: if a workspace record exists for the run
(`mini_ork.workspaces.load(home, run_id)` — derive `home` from `run_dir.parent.parent`) and
`workspaces.status(ws)` reports `exists` and (`commits_ahead` > 0 or `uncommitted`) and the
run status is terminal (`published`, `failed`, `rolled_back`):
- status `published` → `needs_you`, detail `"Ready to review: +<a> −<r> on <branch> — merge
  or discard it."`, `added`/`removed` from the workspace status;
- `failed` / `rolled_back` → stays `failed`, detail gains `" Its worktree is kept: /discard
  <run> removes it."`.
`run_mark` stays cheap (no git): a terminal run whose `<home>/worktrees/<run_id>.json`
record exists and is published → `✋`.
`title_with_state` for this state: `"✋ <base> — ready to review +a −r"`.

### Decision buttons (agent)

- When a run followed in a thread (child or direct) reaches a terminal status inside an
  active prompt turn — direct mode / `/run` / the recipe test run, or an orchestrator turn
  that was still running when the run ended — and its task state is the "ready to review"
  one: after the run's own updates, `request_permission` on the run's marker tool call
  (`<run_id>:parent`) titled `Review run <run_id> (+a −r)` with options `merge` ("Merge into
  <base_branch>", allow_once), `discard` ("Discard changes", reject_always), `keep` ("Keep for
  later", reject_once).
  - `merge` → `workspaces.merge(ws, message=<thread or run title>)`; ok → message
    `"Merged into <base_branch> (<fast-forward|merge commit> <short sha>)."`; refused/conflict
    → message with the error and "The worktree is kept — fix it, then /merge <run>.".
  - `discard` → `workspaces.discard(ws)`; message `"Discarded <run> — its worktree and branch
    are gone."`.
  - `keep` / dismissed → message `"Kept on <branch> — /merge <run> or /discard <run> when
    you're ready."`.
  - After any decision, re-emit the thread title from the new task state (merged/discarded →
    the plain published state `✓`).
- A child run that ends after the orchestrator's turn has ended (no active turn): no
  buttons — emit once per run the message `"Run <run_id> is ready to review (+a −r on
  <branch>): /merge <run_id> or /discard <run_id>."` (the title already shows ✋).
- Orchestrator turns: when a turn ends and runs it followed during the turn ended "ready to
  review", offer the buttons for each (newest first) before ending the turn.

### Commands

- `/workspaces` → table `| | run | branch | base | change | commits | age |` over
  `workspaces.list_open(home)` (mark from the task state); empty → "No open task workspaces."
- `/merge [run]` → resolve the run as other run commands do; no workspace → "Run <id> has no
  open workspace."; else `workspaces.merge(...)` with the same messages as the button.
- `/discard [run]` → same resolution; `workspaces.discard(...)`.
- Announce `merge`, `discard`, `workspaces`.

### Orchestrator prompt

Add: "Runs started from a thread work in their own worktree. When one finishes with changes
the user sees Merge / Discard buttons (or uses /merge, /discard). Never merge or discard on
the user's behalf; tell them what changed and that the decision is theirs."

## Tests

`tests/unit/test_acp_task_state.py`: ready-to-review from a real temp git repo + workspace
(commit in the worktree) for a published run; a failed run with a worktree keeps `failed`
with the kept note; no workspace → unchanged behaviour; `run_mark` ✋ from the record;
title format.

`tests/unit/test_acp_agent_py.py` (no pytest-asyncio — `asyncio.run`; keep the autouse
guard; real temp git repo; launcher seam that "edits" the worktree; a `_PermConn`-style fake
connection answering request_permission): direct-mode run ends ready to review → buttons on
`<run_id>:parent` with the three options; `merge` merges into the project (file content on
the base branch) and the title becomes ✓; `discard` removes worktree + branch; `keep` keeps
it; a merge refused because the project has a dirty overlapping file reports it and keeps
the worktree; a child run ending after the turn gets the one-time message and no buttons.

`tests/unit/test_acp_commands.py`: `/workspaces` (empty and one row), `/merge` and
`/discard` with an explicit run id and with the thread's latest run, no-workspace message.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_task_state.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_commands.py tests/unit/test_workspaces.py` passes.
- `ruff check` clean; diff touches only the files in scope.
