# Zed Z4 — what the run changed, as diffs in Zed's Review Changes

Plan: `docs/plans/2026-10-03-zed-integration.md`. Depends on Z9c-2 merged (serial: every
Zed slice edits `mini_ork/acp/agent.py`).

## Goal

ACP tool calls can carry `diff` content (`FileEditToolCallContent(type="diff", path,
old_text, new_text)`); Zed shows those in the thread and in Review Changes. A mini-ork
thread shows no file changes today. The run already records what it needs — a real
`docs` run dir holds:

- `implementer-summary.json`: `{"status": "implemented", "worktree_path": "<abs>",
  "files_changed": ["<abs path>", ...], "implementation_log": ...}`
  (`status` can also be `no_changes`);
- `pre-implementer-ref`: the baseline commit sha (one line);
- `pre-impl-fixture/files/<rel path>`: a copy of each in-scope file as it was before
  the implementer ran (also for files git does not track).

There is no post-run snapshot, and files keep changing after a run (a later run, the
user), so a diff computed when someone opens an old run could show the wrong change.
Compute the diff when the implementer finishes, cache it in the run dir, and replay the
cache when the run is opened later.

## Files in scope

- `mini_ork/acp/diffs.py` — NEW.
- `mini_ork/acp/agent.py` — emit diffs when an implementer finishes, and on load.
- `tests/unit/test_acp_diffs.py` — NEW.
- `tests/unit/test_acp_agent_py.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `diffs.py`

```python
def run_diffs(run_dir: Path, *, max_files: int = 20, max_bytes: int = 200_000) -> list[dict]
def cached_or_computed(run_dir: Path) -> tuple[list[dict], bool]   # (diffs, from_cache)
CACHE_NAME = "acp-diffs.json"
# each diff: {"path": <abs path>, "old_text": str | None, "new_text": str}
```

- `run_diffs`: read `implementer-summary.json`; missing / unreadable / `status ==
  "no_changes"` / no `files_changed` → []. Worktree = `worktree_path`; baseline =
  `pre-implementer-ref` contents (stripped; may be missing).
- For each changed file (first `max_files`, in order): `rel` = path relative to the
  worktree (skip paths outside it). `old_text` = `git -C <wt> show <baseline>:<rel>`
  stdout (timeout 10s); when that fails or there is no baseline, the file
  `<run_dir>/pre-impl-fixture/files/<rel>` if it exists; else `None` (a new file).
  `new_text` = current file contents, `""` when the file no longer exists (a deletion).
  A file whose old or new content exceeds `max_bytes` or is not valid UTF-8 →
  `{"path", "old_text": None, "new_text": "(binary or large file changed — not shown)"}`.
  Never raises: any git/IO error skips that file.
- `run_diffs` writes its non-empty result to `<run_dir>/acp-diffs.json` (best effort,
  never raises).
- `cached_or_computed`: the cache when it exists and parses (`from_cache=True`), else
  `run_diffs(run_dir)` (`from_cache=False`).

### `agent.py`

- Run dir for a run id: `self._home_for(run_id) / "runs" / run_id`.
- Live: when the projection emits a lifecycle `node_end` for a node whose type is
  `implementer` (payload `node_type`, or the node id `implementer`) and the run dir
  exists: `diffs.run_diffs(run_dir)` → one `ToolCallProgress(tool_call_id=<that node
  id>, content=[FileEditToolCallContent(type="diff", path, old_text, new_text) ...])`,
  at most once per (run, node) (track in a set). Emit it through `_emit(run_id, ...)`
  like every other run update, so a run followed inside a thread gets the
  `"<run_id>:"` prefix and lands in the thread automatically.
- `session/load` of a run, and the per-run re-projection inside a thread load (Z9c-2):
  after the run's lifecycle replay, `diffs.cached_or_computed(run_dir)`; when the run's
  status is `rolled_back`, emit one agent message "Run <id>: its changes were rolled
  back; nothing to review." instead of diffs; when the diffs were computed now (no
  cache), prefix one agent message "Run <id>: showing the files as they are now — they
  may have changed since the run." Attach diffs to the run's implementer node id (from
  the lifecycle events), falling back to `implementer`.
- No diffs → no update.

## Tests

`tests/unit/test_acp_diffs.py` (temp git repo + temp run dir): modified file → old/new
texts; new file → `old_text is None`; untracked file with a fixture copy → old_text from
the fixture; deleted file → `new_text == ""`; file outside the worktree skipped; large
and binary files → placeholder; `max_files` cap; `no_changes` / missing summary → [];
git failure never raises; cache written and preferred by `cached_or_computed`.

`tests/unit/test_acp_agent_py.py` (no pytest-asyncio — drive with `asyncio.run`): an
implementer `node_end` emits exactly one diff update with the expected paths and a
second poll does not re-emit; a run followed in a thread gets it under
`<run_id>:<node>` on the thread session; load of a published run replays the cache
(no "may have changed" note); without a cache the note precedes the diffs; load of a
rolled-back run emits the rolled-back message and no diffs.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_diffs.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_threads.py tests/unit/test_acp_history.py tests/unit/test_acp_live.py` passes.
- `ruff check` clean; diff touches only the files in scope.
