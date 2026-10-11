# Zed Z7 — `mini-ork mcp-context`: runs, learnings, cost and lanes as MCP tools

Plan: `docs/plans/2026-10-03-zed-integration.md`.

## Goal

Zed forwards configured MCP servers to every agent in the editor (Zed's own agent, Claude,
Codex …). Give those agents read access to what mini-ork knows about the project: its run
history, a run's detail, the learned failure modes and patterns, spend, and the lane map.
A stdio MCP server, read-only, no new dependency.

## Files in scope

- `mini_ork/mcp_context/__init__.py` — NEW (empty package marker).
- `mini_ork/mcp_context/server.py` — NEW: tool implementations + stdio JSON-RPC loop.
- `mini_ork/cli/mcp_context_cmd.py` — NEW: `main(rest, root) -> int`, same shape as
  `mini_ork/cli/acp_cmd.py`.
- `mini_ork/cli/main.py` — register `"mcp-context": "mini_ork.cli.mcp_context_cmd"` in
  `_NATIVE_MODULE_SUBS` and add one help line next to the other subcommands.
- `tests/unit/test_native_dispatch_py.py` — add `"mcp-context"` to the exact-set guard.
- `tests/unit/test_mcp_context_server.py` — NEW.

No other file changes.

## Mechanism (exact spec)

### Transport (`server.py`)

Line-delimited JSON-RPC 2.0 on stdio, like `bin/mini-ork-mcp-steering` (read it for the
exact framing). stdout carries only protocol messages; logs go to stderr. Handle:
- `initialize` → `{"protocolVersion": <client's value or "2025-06-18">, "capabilities": {"tools": {}}, "serverInfo": {"name": "mini-ork-context", "version": <mini-ork version>}}`;
- `notifications/initialized` → no response;
- `tools/list` → the tool definitions below (name, description, JSON-schema `inputSchema`);
- `tools/call` → `{"content": [{"type": "text", "text": <json.dumps(result, indent=1, default=str)>}], "isError": false}`;
  unknown tool or a raised error → `isError: true` with the message;
- unknown methods with an id → JSON-RPC error `-32601`; malformed JSON → skip the line.

### Home

`MINI_ORK_HOME` env, else `<cwd>/.mini-ork`. Missing home or DB → every tool returns
`{"error": "no mini-ork home at <path>"}` (not an exception).
Open the DB with the web layer's `mini_ork.web.deps.db_for(home)` (a `StateDB`) so the
tools can reuse the read functions below. Read-only: no tool writes anything.

### Tools (all arguments optional unless noted)

| tool | arguments | returns (reuse) |
|---|---|---|
| `list_runs` | `limit` (default 20, max 200), `status` | newest-first rows: id, status, recipe, cost_usd, created_at, title (first non-empty kickoff line, `#` stripped, ≤80 chars). If `mini_ork/acp/history.py` exists with `list_runs`, call it; otherwise query `task_runs` directly. |
| `run_detail` | `run_id` (required) | task_run row (status, recipe, cost_usd, created_at/updated_at), node lifecycle events (`RunDetailRepository.fetch_node_lifecycle_events`), first 2000 chars of the kickoff, `verdict.json` from `<home>/runs/<id>/` if present. |
| `learnings` | `task_class`, `query` (substring, case-insensitive), `limit` (default 20) | `{"failure_modes": RunDetailRepository.fetch_failure_mode_gradients(task_class) (when task_class given), "patterns": mini_ork.web.routes.learning.emergent_patterns(db, limit), "memory": mini_ork.web.routes.learning.memory(db, limit)}`, each list filtered by `query` over its JSON text. |
| `cost` | `days` (default 7) | `mini_ork.web.routes.trajectory.cost_by_day(db)` filtered to the last `days` days, plus a total. |
| `lanes` | — | `mini_ork.web.recipes.load_lanes(home)` (the function `/lanes` uses). |

Call the route functions as plain functions (they take a `StateDB`); if one raises
because a table is missing, return an empty value for that key.

### CLI

`mini-ork mcp-context` runs the server until stdin closes; `--help` prints usage to
stderr and returns 0; any other argument → usage + return 2.

## Tests (`tests/unit/test_mcp_context_server.py`)

Temp home with a migrated DB (copy how existing tests build one) and a couple of
`task_runs` rows + a kickoff file. Drive the server through an in-memory loop (call the
dispatch function with request dicts) AND once end to end as a subprocess
(`[sys.executable, bin/mini-ork, "mcp-context"]` with `MINI_ORK_HOME` set): `initialize`,
`tools/list` names, `tools/call list_runs` returns the rows newest first with titles,
`run_detail` for a missing id returns an error object, `learnings` with a query filters,
unknown tool → `isError: true`, unknown method → `-32601`, malformed line skipped,
missing home → error objects. Nothing is ever printed to stdout except protocol lines.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_mcp_context_server.py tests/unit/test_native_dispatch_py.py` passes.
- `ruff check` clean on touched files.
- Diff touches only the files in scope.
