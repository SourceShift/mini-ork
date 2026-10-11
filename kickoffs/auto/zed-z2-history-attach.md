# Zed Z1+Z2 — run history and attach-to-running-run over ACP

Plan: `docs/plans/2026-10-03-zed-integration.md`.

## Goal

`mini-ork acp` today supports `initialize`, `session/new`, `session/prompt`,
`session/cancel`. In Zed, a user cannot see past mini-ork runs or open a run that is
already going (for example one started from the CLI). Zed has both surfaces: Thread
History → Import Threads calls `session/list` and opening a thread calls `session/load`.
Implement them, and resolve the run home from the project Zed has open.

## Files in scope

- `mini_ork/acp/history.py` — NEW: read-model queries for listing and replaying runs.
- `mini_ork/acp/agent.py` — capabilities, home resolution, `list_sessions`,
  `load_session`, live follow of an attached run.
- `tests/unit/test_acp_history.py` — NEW.
- `tests/unit/test_acp_agent_py.py` — extend (existing tests must keep passing).

No other file changes.

## Mechanism (exact spec)

### Home resolution (Z1)

Add `MiniOrkAcpAgent._home_for(session_id: str | None) -> Path`:
1. if the session has a bound cwd and `<cwd>/.mini-ork` is a directory → that;
2. elif the constructor `home` → that;
3. elif `MINI_ORK_HOME` env → that;
4. else `<process cwd>/.mini-ork`.
Use it everywhere the agent resolves a home today (`_launch`, `_read_snapshot`, stop,
kill). `_resolve_home()` stays as the no-session fallback (steps 2–4).

### `mini_ork/acp/history.py`

```python
def read_snapshot(home: Path, run_id: str) -> dict
def list_runs(home: Path, *, limit: int = 50, offset: int = 0) -> tuple[list[dict], int | None]
def kickoff_text(home: Path, run_id: str, max_chars: int = 4000) -> str
```

- `read_snapshot`: MOVE the body of `MiniOrkAcpAgent._read_snapshot` here unchanged
  (task_run status, node lifecycle events, llm_calls via `RunDetailRepository`, deduped);
  the agent method becomes a one-line call. Same return shape
  `{"status", "events", "llm_calls"}`.
- `list_runs`: rows from `task_runs` ordered newest first (by `created_at`, falling back
  to rowid), `limit` + `offset` paging; each row →
  `{"run_id", "status", "recipe", "cost_usd", "created_at", "updated_at", "title"}`.
  `title` = first non-empty line of the kickoff file (`kickoff_path` column; if missing
  try `<home>/runs-inbox/<run_id>.md`), stripped of leading `#`, max 80 chars; fallback
  `f"{recipe or 'mini-ork'} run"`. Skip ids that fail `_is_safe_token`. Return
  `next_offset` or None when exhausted. Missing DB / table → `([], None)`.
- `kickoff_text`: the kickoff markdown (same lookup as the title), truncated to
  `max_chars`; "" when absent.
- Timestamps: `created_at`/`updated_at` may be ints (epoch seconds) or ISO strings;
  normalize to ISO-8601 UTC strings for `updated_at`.

### `mini_ork/acp/agent.py`

- `initialize` returns
  `agent_capabilities=AgentCapabilities(load_session=True, session_capabilities=SessionCapabilities(list=SessionListCapabilities()))`.
- `async def list_sessions(self, cwd=None, cursor=None, **kwargs) -> ListSessionsResponse`:
  home = `<cwd>/.mini-ork` when `cwd` is given and that dir exists, else `_resolve_home()`;
  if the home does not exist → empty list. `cursor` is the decimal offset string
  (invalid → 0). Each run → `SessionInfo(session_id=run_id, cwd=str(home.resolve().parent),
  title=title, updated_at=iso, field_meta={"status": ..., "recipe": ..., "cost_usd": ...})`.
  `next_cursor` = str(next_offset) or None.
- `async def load_session(self, cwd, session_id, mcp_servers=None, additional_directories=None, **kwargs) -> LoadSessionResponse | None`:
  - reject a non-safe `session_id` by raising `acp.RequestError.invalid_params(...)`
    (use whatever constructor the installed SDK exposes for invalid params);
  - bind `self._sessions[session_id] = cwd`; reset `self._emitted[session_id]`;
  - snapshot = `history.read_snapshot(self._home_for(session_id), session_id)`; when the
    run has no task_run row → raise invalid params ("no mini-ork run <id>");
  - emit `UserMessageChunk(session_update="user_message_chunk", content=TextContentBlock(type="text", text=kickoff))`
    when the kickoff text is non-empty, then `await self._project_snapshot(session_id, snapshot)`
    (existing projection: tool calls, usage, terminal message);
  - if the status is not terminal → start a follow task
    (`asyncio.create_task(self._follow(session_id))`, kept in `self._followers`) that
    loops like `_await_terminal` (poll, `_project_snapshot`, stop on terminal or when the
    session is cancelled) — no launch, no start-timeout refusal;
  - return `LoadSessionResponse()`.
- `prompt` on a session that was loaded (not created by `session/new`): do NOT launch.
  If its run is running, emit an agent message "Attached to running mini-ork run <id>;
  updates stream here. Start a new thread to launch another run." and return
  `end_turn` immediately; if finished, emit "This thread shows a finished run (<status>).
  Start a new thread to run again." and return `end_turn`. Track loaded sessions in a set.
- `cancel` on an attached running session stops the follower and soft-stops the run
  (existing stopper path); on a finished one it is a no-op.

## Tests

`tests/unit/test_acp_history.py` (temp home with a migrated DB — copy how existing tests
build one: `grep -rln "init_db\|migrate" tests/unit | head`):
- `list_runs` returns newest first, titles from the kickoff file (heading `#` stripped,
  80-char cap), fallback title, paging with `next_offset`, unsafe ids skipped, missing DB → empty.
- `kickoff_text` reads `kickoff_path`, falls back to `runs-inbox/<id>.md`, truncates.
- `read_snapshot` returns the same shape as before for a run with events and calls.

`tests/unit/test_acp_agent_py.py` (reuse its fake connection / injected seams):
- `initialize` advertises `load_session` and session list.
- `list_sessions(cwd=<project>)` resolves `<project>/.mini-ork` and maps rows to
  `SessionInfo` with `cwd == <project>`; cursor round-trips.
- `load_session` on a finished run emits the kickoff as a user message, tool-call
  updates and the terminal message, and starts no follower.
- `load_session` on a running run starts a follower that emits new transitions and the
  terminal message once the injected reader flips to `published`.
- `prompt` on a loaded session never calls the launcher.
- `_home_for` precedence: session cwd with `.mini-ork` > constructor home > env > process cwd.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_acp_history.py tests/unit/test_acp_agent_py.py tests/unit/test_web_control_launch.py` passes.
- `ruff check` clean on touched files.
- Diff touches only the files in scope.
