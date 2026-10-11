# Zed Z6 — the run's DAG as a live plan in the thread

Plan: `docs/plans/2026-10-03-zed-integration.md`. Depends on Z5 merged (serial: every Zed
slice edits `mini_ork/acp/agent.py`). The recipe picker this slice once included shipped
in Z9c-1 as the Recipe config option — only the live plan is left.

## Goal

ACP `plan` updates render in Zed as a checklist above the thread. Show the run's
workflow nodes there, ticking off as they finish, so the user sees where a run is.

## Files in scope

- `mini_ork/acp/plan.py` — NEW.
- `mini_ork/acp/agent.py` — emit plan updates.
- `tests/unit/test_acp_plan.py` — NEW.
- `tests/unit/test_acp_agent_py.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `plan.py`

```python
def plan_entries(run_dir: Path, events: list[dict]) -> list[dict]   # [{"content", "status", "priority"}]
```

- Nodes come from `<run_dir>/plan.json` → `decomposition`, a list of
  `{"id", "description", "node_type", "depends_on": [ids]}` (real shape, from a `docs`
  run). Missing/unparseable file or no `decomposition` list → `[]`.
- Lifecycle `events` are the snapshot's events (`event_type` `node_start` / `node_end`,
  `payload_json` a dict or JSON string with `node_id`).
- Status per node: `node_end` seen → `completed`; `node_start` without end →
  `in_progress`; no events, but some node that lists it in `depends_on` has started →
  `completed` (it was skipped — e.g. the planner under a static plan emits no events);
  else `pending`.
- `content` = `"<id> (<node_type>)"`; `priority` = `"high"` for node types `implementer`
  and `reviewer`, else `"medium"`. Order = decomposition order.

### `agent.py`

- In `_project_snapshot(session_id, snapshot, live=...)` — every projection pass: live
  polls, follows, and load replays — build `plan_entries(<home>/runs/<run_id>, events)`
  for the projected run and emit
  `AgentPlanUpdate(session_update="plan", entries=[PlanEntry(content=…, status=…, priority=…)])`
  through `_emit` only when the entries differ from the last ones sent to that ACP
  session (track per destination session: the run session, or the thread for a routed
  run). Empty entries → nothing.
- Threads: ACP has one plan per session. A routed run's plan is emitted only when that
  run is the thread's latest run (`self._thread_runs[thread][-1]`, from Z5); an older run
  still being followed does not overwrite it. When a new run starts in the thread, its
  plan replaces the previous one on its first projection.
- Load: run sessions and thread loads emit the plan of the (latest) run as part of the
  replay — no special casing beyond the rules above.

## Tests

`tests/unit/test_acp_plan.py`: entries from a `decomposition` (the five-node docs shape:
planner → doc_editor → grep_assert, link_verifier → publisher); statuses from events
(dict and JSON-string payloads); the skipped planner reads `completed` once doc_editor
started and `pending` before; priorities; missing / bad plan.json → [].

`tests/unit/test_acp_agent_py.py` (no pytest-asyncio — drive with `asyncio.run`; keep
the autouse `_never_launch_a_real_run` guard): a run session emits a plan update on each
status change and none on an unchanged poll; loading a finished run emits its final plan;
in a thread, a routed run's plan is addressed to the thread; with two runs in a thread
only the latest one's plan is sent.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_plan.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_commands.py tests/unit/test_acp_threads.py tests/unit/test_acp_diffs.py tests/unit/test_acp_history.py tests/unit/test_acp_live.py` passes.
- `ruff check` clean; diff touches only the files in scope.
