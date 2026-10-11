# Zed Z9c-2 — orchestrator threads persist, show in Thread History, and reopen

Plan: `docs/plans/2026-10-03-zed-integration.md`. Depends on Z9c-1 (merged eca51e38, fce1b887).

## Goal

A Zed thread is now a conversation with the mini-ork orchestrator (session ids
`orch-<epoch>-<hex>`), but it lives only in the agent process's memory: restart
Zed and it is gone; Thread History lists only runs. Persist each thread in the
project's `.mini-ork`, list threads in `session/list`, and make `session/load`
replay a thread — the user's prompts, the orchestrator's messages and tool calls,
and every run it started (with that run's history and live output) — and resume
the same claude conversation on the next prompt.

## Files in scope

- `mini_ork/acp/threads.py` — NEW: the thread store.
- `mini_ork/acp/agent.py` — record, list, load.
- `tests/unit/test_acp_threads.py` — NEW.
- `tests/unit/test_acp_agent_py.py` — extend.

No other file changes (in particular not `mini_ork/acp/history.py`).

## Mechanism (exact spec)

### `threads.py`

Store: `<home>/acp-threads/<thread_id>.jsonl`, one JSON object per line, append-only.

```python
class ThreadStore:
    def __init__(self, home: Path) -> None
    def append(self, thread_id: str, record: dict) -> None   # adds "t": time.time()
    def read(self, thread_id: str) -> list[dict]             # [] when missing; skips bad lines
    def exists(self, thread_id: str) -> bool
    def list_threads(self, limit: int = 100) -> list[dict]   # newest first
```

- `thread_id` must match `^orch-[A-Za-z0-9-]+$`, else `ValueError` (no path tricks).
- `append` creates the directory, opens in append mode, writes one line, flushes.
  Never raises on I/O errors (log to stderr and continue — persistence must not
  break a live thread).
- Record types (`"type"` key):
  - `meta` — `{"thread_id", "cwd"}`, written once by `new_session`;
  - `config` — `{"mode", "model", "recipe"}`, full current values, on new_session and every change;
  - `user` — `{"text"}`, each prompt;
  - `update` — `{"update": <ACP update as JSON>}` (`model_dump(mode="json", by_alias=True, exclude_none=True)`);
  - `claude_session` — `{"id"}` when the resume id changes;
  - `costs` — `{"costs": {key: usd}}`, the thread's cost map whenever its usage is sent.
- `list_threads`: one row per `*.jsonl` file: `{"thread_id", "cwd", "title",
  "updated_at"}` — `title` = first `user` record's text, first line, ≤ 80 chars
  (fallback "mini-ork thread"); `updated_at` = file mtime as ISO-8601 UTC with `Z`;
  sorted by mtime desc; at most `limit`.

### `agent.py`

Recording (thread sessions only — ids in `_thread_sessions`):
- `new_session` (thread branch) → `meta` + `config`.
- `set_config_option` on a thread → `config`.
- `_prompt_thread` → `user` record before routing; on the FIRST prompt of a
  thread also emit `SessionInfoUpdate(session_update="session_info_update",
  title=<same title rule>)`.
- `_emit`: when the ORIGINAL `session_id` is a thread session (not a routed run),
  record the update — except `UsageUpdate` (record a `costs` record with
  `self._thread_costs[thread]` instead) and `SessionInfoUpdate`. Updates of
  routed runs (child run internals) are NOT recorded: they are re-projected from
  the run's own data on load. The run marker (`<run_id>:parent` start and close)
  is emitted directly to the thread, so it IS recorded.
- `_prompt_thread_orchestrate` → `claude_session` when the stored id changes.

Listing — `list_sessions(cwd, cursor)`:
- First page (no cursor): `ThreadStore(home).list_threads()` rows as
  `SessionInfo(session_id=thread_id, cwd=<recorded cwd or project root>, title,
  updated_at, field_meta={"kind": "thread"})`, then the run rows exactly as today;
  the whole page sorted by `updated_at` desc (missing last). Later pages: runs only,
  as today (the cursor stays the run offset).
- Same home rules as today (a cwd without `.mini-ork` → empty list).

Loading — `load_session(cwd, session_id)` when `session_id` starts with `orch-`:
- Unknown thread (no file) → invalid params error.
- Restore: `_sessions[id] = cwd`, add to `_thread_sessions`, `_thread_config[id]`
  from the last `config` record (+ `claude_session_id` from the last
  `claude_session` record), `_thread_costs[id]` from the last `costs` record.
  Mark the thread as having had its first prompt (no new title update).
- Replay in file order: `user` → `UserMessageChunk`; `update` → rebuild with
  `acp.schema.SessionNotification.model_validate({"sessionId": id, "update": rec["update"]})`
  and emit (skip a record that fails validation). Do NOT re-record replayed updates.
- When a replayed update is a `ToolCallStart` whose id ends with `:parent`, the run
  id is the prefix: register the route (`_routes[run_id] = (id, f"{run_id}:")`,
  `_sessions.setdefault(run_id, cwd)`), then project that run's history right
  there — `snapshot = (reader)(run_id)`, `_project_snapshot(run_id, snapshot,
  live=False)` and `_replay_live_for(run_id, sorted(started node ids))` (the
  routed `_emit` prefixes and re-addresses them). If the snapshot is not terminal,
  after the replay start `_follow_child(id, run_id)` as a follower (no new marker).
- Finally emit one thread `UsageUpdate` (`_thread_usage_update`) and return
  `LoadSessionResponse(config_options=<current options>, modes=<modes>)`.
- A later prompt resumes the claude conversation (`claude_session_id`) — existing
  `_prompt_thread_orchestrate` behaviour.

Run sessions (`run-…` ids) load exactly as today.

## Tests

`tests/unit/test_acp_threads.py`: append/read round trip with `t`; bad lines
skipped; unsafe ids rejected; `list_threads` title rule, newest first, limit,
ISO `updated_at`; append to an unwritable home does not raise.

`tests/unit/test_acp_agent_py.py` (fake orchestrator turn / reader / launcher as
the existing thread tests do; the autouse `_never_launch_a_real_run` guard stays):
- a thread with one orchestrate turn (message + tool call + a `start_run` child
  run) writes meta, config, user, update, claude_session, costs records; child
  run internals are not recorded; the first prompt emits a `SessionInfoUpdate`.
- `list_sessions` first page includes the thread (kind thread, title) and runs,
  sorted by updated_at; second page runs only.
- a NEW agent instance `load_session`s the thread: replays the user prompt, the
  message, the tool call, the run marker, then the child run's lifecycle under
  `<run_id>:` ids; returns config options with the stored model; the next prompt
  passes the stored claude session id as `resume`.
- loading a thread whose child run is still running starts a follower.
- unknown `orch-` id → error; run sessions load as before.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_threads.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_orchestration.py tests/unit/test_acp_history.py tests/unit/test_acp_live.py` passes (no pytest-asyncio in CI: tests drive coroutines with `asyncio.run`).
- `ruff check` clean; diff touches only the files in scope.
