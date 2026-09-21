# Zed Slice 0c — land `mini-ork acp` on the current base

## Goal

`mini-ork acp` is a stdio ACP agent that binds one ACP session to one mini-ork run
and projects that run's live node lifecycle onto the ACP wire, using the **existing**
launch seam and the **existing** read model. This tree has **no** `mini_ork/acp/`
at all — port the reference back in and fix its two defects.

The event feed this projects **now exists**. A prior cycle restored it: `dispatch_node`
writes `node_start`/`node_end` rows to `run_events` (merged as `8e6b6403`, the base of
this branch). Do not touch that plumbing — just consume it.

## Reference implementation (read this; it is ~90% correct)

Read, do not re-invent:

```bash
git show acp-slice0b-checkpoint:mini_ork/acp/agent.py            # 328 lines
git show acp-slice0b-checkpoint:mini_ork/cli/acp_cmd.py          # 44 lines
git show acp-slice0b-checkpoint:tests/unit/test_acp_agent_py.py  # 205 lines
```

The tag `acp-slice0b-checkpoint` is pinned in this repo. Keep what is sound
(`mint_run_id`, `on_connect` capture, the stdout discipline, the `_launch` seam,
the CLI shape, `_build_usage_update`, `cancel`) and **fix the two defects below**.
Do not copy it blindly — it has never been on main.

## Seams (verified against this tree — read the source, do not trust this prose)

- `mini_ork/web/control.py` — `launch_run(home, recipe, kickoff_markdown, run_id=None, extra_env=None) -> dict`.
  Detached; returns `{ok, run_id, ...}`. Run id is client-mintable and validated by
  `control._is_safe_token`. **There is no second spawn path and you must not add one.**
  Also `stop_run(home, db, task_run_id)` and `kill_run(...)`.
- `mini_ork/web/deps.py` — `db_for(home) -> StateDB`, the cached handle factory the web
  layer itself uses. Build your db handle this way.
- Read model `mini_ork/web/routes/run_detail.py`: `get_task_run(task_run_id, db)`,
  `get_events(task_run_id, db, limit=500)`, `get_llm_calls(task_run_id, db)`.
  `TERMINAL_STATUSES` is a module constant (`{"published","rolled_back","failed"}`).
  Call these with explicit arguments; the `Depends` defaults are FastAPI wiring.
  **Do not write new SQL.**

### The node event contract (this is what you project)

`run_events` rows, written by `mini_ork/observability/node_events.py`:

| event_type | `payload_json` keys |
|---|---|
| `node_start` | `node_id`, `node_type`, and `model_lane` when the lane was known |
| `node_end` | `node_id`, `node_type`, `duration_ms` (always), plus `verdict`, `artifact_path`, `finish_reason` when truthy |

`get_events(...)` returns them oldest-first with an `event_type` and `payload_json`
(string — `json.loads` it). A `node_start`/`node_end` pair for the same `node_id` is
one node's life.

### ACP SDK wiring (`agent-client-protocol` 0.12.1, module name `acp`)

- **Getting the connection:** `AgentSideConnection.__init__` ends with
  `if on_connect := getattr(agent, "on_connect", None): on_connect(self)`
  (`acp/agent/connection.py:101-102`). Define `def on_connect(self, conn)` and store it.
- `AgentSideConnection.session_update(session_id, update)` — the four updates you need:
  `ToolCallStart`, `ToolCallProgress`, `AgentMessageChunk`, `UsageUpdate`.
- `UsageUpdate(used, size, cost=None)` and `Cost(amount: float, currency: str)` (ISO 4217).
  Cost rides on `UsageUpdate` — the `Usage` model has no cost field.
- `StopReason` is a `Literal`, not an enum:
  `["end_turn","max_tokens","max_turn_requests","refusal","cancelled"]`. Do not wrap it.
- `acp.run_agent(agent, ...)` drives the loop.

## Defects to fix (the reason this is not just a copy)

### D1 — emit each node transition exactly once

The reference re-emits the **whole backlog on every poll**. Measured on a real run:
**6 `ToolCallStart` for 2 nodes over 3 polls**. Track what has already been sent
per session (node id + whether start/end went out) and emit only what is new.
Ingest events in timestamp order.

### D2 — honour the client's `_meta` overrides

`new_session` must honour a client-minted `_meta.run_id` when it passes
`control._is_safe_token`, else mint one in `launch_run`'s shape. It must also accept
`_meta.recipe` (default `"code-fix"`). The reference ignored both. The session id
**is** the run id.

## Mechanisms (exact spec)

1. **Session identity and cwd.** `new_session` mints/accepts the run id, stores
   `{session_id: cwd}` (and the recipe), and **launches nothing**. `prompt` resolves the
   session's `cwd` and passes it to the launch so the run edits the project the client
   opened — `launch_run` takes `extra_env`; the target directory belongs there (mirror how
   the web layer sets the target cwd rather than inventing a key).

2. **Projection.** While the run is not terminal, poll the read model at a module-level
   `_POLL_INTERVAL_S` (default 2.0) and emit via `self._conn.session_update(...)`:

   | Read model | ACP update |
   |---|---|
   | a `node_start` event | `ToolCallStart` |
   | the matching `node_end` event | `ToolCallProgress` carrying the node's verdict |
   | agent text on the run | `AgentMessageChunk` |
   | cumulative `SUM(llm_calls.cost_usd)` | `UsageUpdate(used=…, size=…, cost=Cost(amount=…, currency="USD"))` |

   `get_task_run(...)["status"]` decides terminality against `TERMINAL_STATUSES`.
   Emit each transition **once** (D1).

3. **Stop reasons.** Terminal run → `"end_turn"`. Client cancelled → `"cancelled"`.
   Unsafe session id → `"refusal"`. **A successful launch is not `"cancelled"`.**

4. **`cancel`.** `async def cancel(self, session_id, **kwargs) -> None`: set the
   per-session cancelled flag the projection loop checks, then call
   `control.stop_run(home, db, session_id)`. Escalate to `control.kill_run` only if the
   run is still non-terminal after a module-level `_CANCEL_ESCALATE_S`. `cancel` must be
   able to run *while* `prompt` awaits — verify that against the SDK source and say in
   your summary how you verified it.

5. **CLI.** `mini_ork/cli/acp_cmd.py` exposes `main(rest, root) -> int` and a `__main__`
   block (the registry runs it as `python -m mini_ork.cli.acp_cmd`). **stdout is the ACP
   wire** — never `print()` to it, diagnostics to stderr only, and importing the module
   must print nothing.

## Tests — `tests/unit/test_acp_agent_py.py` (new to this tree)

Hermetic: stub the launch seam and the read model; no lane, no network, no real run.
The editable install resolves `mini_ork` to the **main** checkout, so insert the repo
root on `sys.path` before importing:

```python
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
```

**These seven assertions are the contract** — implement all seven, do not reword,
weaken, or drop one to match what you built:

1. `new_session` returns an id passing `control._is_safe_token`; a `_meta.run_id`
   override **wins**; the response's `field_meta` records the bound run id.
2. `new_session` launches nothing (assert the launch stub was never called).
3. `prompt` calls `launch_run` with `run_id == session_id`, and the flattened kickoff
   contains the prompt's text.
4. A run with two `node_start` and one `node_end` produces **two** `ToolCallStart` and
   **one** `ToolCallProgress`.
5. **D1 regression:** projecting the *same* snapshot twice emits no additional
   `ToolCallStart`/`ToolCallProgress` for the nodes already sent.
6. The `UsageUpdate` cost equals the stubbed `SUM(llm_calls.cost_usd)` exactly and
   `currency == "USD"`.
7. A terminal run yields `stop_reason == "end_turn"`; `cancel` calls `stop_run` with the
   session id.

## Files in scope

- `mini_ork/acp/__init__.py`, `mini_ork/acp/agent.py` (new)
- `mini_ork/cli/acp_cmd.py` (new)
- `mini_ork/cli/main.py` — add `"acp": "mini_ork.cli.acp_cmd"` to `_NATIVE_MODULE_SUBS`
- `pyproject.toml` — add a bounded `acp` extra: `agent-client-protocol>=0.12,<0.13`
- `tests/unit/test_acp_agent_py.py` (new)
- `tests/unit/test_native_dispatch_py.py` — add `"acp"` to the `expected` set in
  `test_all_former_exec_subs_registered_natively` (that exact-set assertion is a real
  contract; leaving it red is a failure)

Do not touch `mini_ork/web/**` except to **read** it, `mini_ork/dispatch/**`, any recipe,
any probe, or `mini_ork/observability/node_events.py`. Do not add a second launch path,
a session→run mapping *table*, or new SQL against `task_runs` / `llm_calls`.

## Verification commands

Run from the worktree root with `python3.11` explicitly — the ambient `python3` is 3.9
and dies at collection.

```bash
python3.11 -m pytest tests/unit/test_acp_agent_py.py -q
python3.11 -m pytest tests/unit/test_native_dispatch_py.py -q
python3.11 -c "from mini_ork.cli.main import SUBCOMMAND_REGISTRY as R; assert 'acp' in R, sorted(R); print('acp registered')"
python3.11 -m py_compile mini_ork/acp/agent.py mini_ork/cli/acp_cmd.py mini_ork/cli/main.py
python3.11 -c "import mini_ork.cli.acp_cmd" | wc -c    # expect 0
```

## Self-application measurement

Zero model spend. Drive the real stdio transport with `python3.11 /tmp/mo-acp-smoke.py`
(it forces `PYTHONPATH` to the worktree, defeating the editable-install trap). Adjust its
`W` constant to this worktree before running. `initialize` + `session/new` must launch
nothing and the session id must pass `_is_safe_token`.

Then prove the projection against a **real run already in the live DB** — no new spend:
`UsageUpdate.cost.amount == SELECT SUM(cost_usd) FROM llm_calls WHERE run_id=<sid>`, and
the node events for that run project to ToolCall updates. If you cannot run this, say so
explicitly and report the hermetic counts — **do not** claim a measurement you did not take.

Evidence artifact `${MINI_ORK_RUN_DIR}/acp-slice0c.json`:

```json
{"initialize_ok": true, "session_id": "<id>", "session_id_is_run_id": true,
 "launches_on_new_session": 0, "meta_run_id_override_honoured": true,
 "updates_emitted": <int>, "no_backlog_reemission": true,
 "cost_matches_llm_calls": true, "cancel_calls_stop_run": true,
 "native_dispatch_test_green": true}
```

## Done When

- `tests/unit/test_acp_agent_py.py` and `tests/unit/test_native_dispatch_py.py` are green.
- `python3.11 -c "import mini_ork.cli.acp_cmd" | wc -c` prints `0`.
- `agent.py` defines `on_connect`, emits all four update kinds, binds `session→cwd`,
  defines `cancel`, returns `"end_turn"` on terminal, and tracks sent transitions so no
  transition is emitted twice — verifiable by reading it, not by reading a summary.
- `pyproject.toml` carries a bounded `acp` extra.
- `${MINI_ORK_RUN_DIR}/acp-slice0c.json` exists with the fields above.

## If you cannot finish

Say so — with the failing command and its output — rather than narrowing the tests or
reporting a partial build as done. A earlier run on this surface claimed "six files, all
verified" while the suite was red, the projection was absent, and `new_session` discarded
`cwd`. A precise partial result beats a green claim that does not survive `git diff`.
