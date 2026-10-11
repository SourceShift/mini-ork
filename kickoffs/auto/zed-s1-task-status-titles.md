# Zed S1 — every task shows working / needs you / done / failed, live, in Zed's thread list

Depends on Z9c-2, Z4 and S0 merged (serial: every Zed slice edits `mini_ork/acp/agent.py`).

## Goal

A fleet is only manageable if status comes to you. In Zed the thread list is the sidebar:
each mini-ork thread is a task, and its title should say where the task is —
`● Fix login loop` while working, `✋ Fix login loop` when it needs the user,
`✓ Fix login loop +12 −3` when done (with the diff size), `✗ Fix login loop` when it failed —
updated as the run moves (ACP `SessionInfoUpdate(title=...)`, which Zed shows in its thread
list). Runs in Thread History carry the same mark.

## Files in scope

- `mini_ork/acp/task_state.py` — NEW: one state model.
- `mini_ork/acp/agent.py` — live titles, needs-you message, marks in history.
- `mini_ork/acp/threads.py` — a `title` record; `list_threads` shows the latest title.
- `tests/unit/test_acp_task_state.py` — NEW.
- `tests/unit/test_acp_agent_py.py`, `tests/unit/test_acp_threads.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `task_state.py`

```python
@dataclass(frozen=True)
class TaskState:
    state: str        # "working" | "needs_you" | "done" | "failed"
    detail: str       # one plain line for the user
    step: str         # the node running now, else the last node seen, else ""
    added: int        # lines added by the run's changes
    removed: int      # lines removed

MARKS = {"working": "●", "needs_you": "✋", "done": "✓", "failed": "✗"}

def task_state(run_dir: Path, snapshot: dict) -> TaskState
def run_mark(status: str | None, run_dir: Path | None) -> str     # cheap: status + sentinel only
def title_with_state(base: str, ts: TaskState | None) -> str
```

Rules for `task_state` (first match wins), using the snapshot from
`mini_ork.acp.history.read_snapshot` (`status`, `events` with `event_type`/`payload_json`):
1. `<run_dir>/.cost-pause` exists → `needs_you`, detail "Paused: the daily budget was
   reached. /resume <run> continues it, /stop <run> ends it."
2. an `execute_blocked` event whose payload has `human_questions` → `needs_you`, detail
   "Waiting for your answer: <first question>".
3. status `published` → `done`, detail "Published".
4. status `failed` or `rolled_back` → `failed`, detail "Failed at <last node whose node_end
   finish_reason is not 'done'> (<finish_reason>)" when found, else "Failed".
5. otherwise (`None`, `classified`, `planned`, `executing`, `reviewing`, …) → `working`,
   detail "Working: <step>" or "Starting".
`step` = the last `node_start` without a matching `node_end`, else the last node seen.
`added/removed`: from `mini_ork.acp.diffs.cached_or_computed(run_dir)` (never writes a
cache), counting `+`/`-` lines of `difflib.unified_diff(old.splitlines(), new.splitlines())`
per file (old `None` = all lines added); 0/0 on any error. Only computed for `done`.

`run_mark` (for listing many runs — no event or diff reads): `.cost-pause` → ✋; status
`published` → ✓; `failed`/`rolled_back` → ✗; else ●.

`title_with_state(base, ts)`: `base` alone when `ts` is None; else `"<mark> <base>"`, plus
`" +<added> −<removed>"` when the state is done and either is non-zero.

### `agent.py`

- Keep each thread's base title (the first-prompt title it already computes;
  `self._thread_titles[thread]`, restored on load from the store).
- In every projection pass of a run followed in a thread (routed run — the place that
  already emits its lifecycle updates), when that run is the thread's latest run
  (`self._thread_runs[thread][-1]`), compute `task_state(run_dir, snapshot)` and, when the
  resulting title differs from the last one sent for that thread, emit
  `SessionInfoUpdate(session_update="session_info_update", title=...)` to the thread and
  record a `{"type": "title", "title": ...}` record. When the state turns `needs_you`, also
  emit one agent message with the detail (once per run per state).
- Run sessions (a run opened from history or started in a run session) get the same title
  update on their own session.
- `list_sessions`: run rows' titles get `run_mark(...)` prepended (the run dir is
  `<home>/runs/<run_id>`); thread rows use the latest recorded title (from the store).

### `threads.py`

- Accept a `title` record type. `list_threads` uses the last `title` record's text when
  present, else the first-prompt title as today (keep the single early-exit pass for the
  meta/first prompt, and read the last `title` record without loading the whole file into
  memory — e.g. scan lines once keeping the last title seen).

## Tests

`tests/unit/test_acp_task_state.py`: each rule (cost pause, blocked with question, published,
failed with the failing node, working with the current step, no snapshot); added/removed
from a cached diff (new file, modified file); `run_mark` per status; `title_with_state`
formats.

`tests/unit/test_acp_agent_py.py` (no pytest-asyncio — `asyncio.run`; keep the autouse
guard): a thread whose child run goes executing → published emits titles `● base` then
`✓ base +a −b`, once each, and records them; a second, newer run in the same thread drives
the title and the older one does not; a cost-paused run emits `✋` and the explanation once;
`list_sessions` marks run rows and shows a thread's latest title.

`tests/unit/test_acp_threads.py`: latest `title` record wins in `list_threads`; first-prompt
title when none.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_task_state.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_threads.py tests/unit/test_acp_diffs.py tests/unit/test_acp_commands.py` passes.
- `ruff check` clean; diff touches only the files in scope.
