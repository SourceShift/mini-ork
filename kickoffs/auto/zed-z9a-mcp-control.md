# Zed Z9a — control tools on `mini-ork mcp-context --control`

Plan: `docs/plans/2026-10-03-zed-integration.md` (orchestrator section). Depends on Z7.

## Goal

The Zed text bar will talk to an orchestrator agent that drives mini-ork the way a human
operator does: write a kickoff, launch a run, wait for it, read the outcome, stop it,
certify a change. The orchestrator gets those abilities as MCP tools. Add them to the
existing MCP server behind an explicit flag, so the default server stays read-only.

## Files in scope

- `mini_ork/mcp_context/server.py` — new tools, gated.
- `mini_ork/cli/mcp_context_cmd.py` — accept `--control`.
- `tests/unit/test_mcp_context_server.py` — extend.

No other file changes.

## Mechanism (exact spec)

`mini-ork mcp-context --control` (or env `MO_MCP_CONTROL=1`) enables the tools below in
`tools/list` and `tools/call`; without it they are absent and calling one is an unknown
tool. The project root is `home.parent` (home = `MINI_ORK_HOME` or `<cwd>/.mini-ork`).

| tool | arguments | behaviour |
|---|---|---|
| `list_recipes` | — | every `<engine root>/recipes/<name>/` with `workflow.yaml` + `task_class.yaml`: `{"id", "description"}` (first line of task_class `description`, ≤120 chars). Engine root = `mini_ork.web.control._mini_ork_root()` (what `launch_run` uses). |
| `start_run` | `recipe` (required), `kickoff_markdown` (required) | `mini_ork.web.control.launch_run(home, recipe, kickoff_markdown, extra_env={"MO_TARGET_CWD": str(project root)})`; return its dict (run_id, pid, kickoff_path, log_path) or `{"error": ...}` when `ok` is False. |
| `run_status` | `run_id` (required) | `{"status", "recipe", "cost_usd", "nodes": [{"node_id", "state"}]}` — node state from lifecycle events (`node_end` → done, `node_start` only → running); unknown run → error object. Reuse `mini_ork.acp.history.read_snapshot` if present, else `RunDetailRepository`. |
| `wait_for_run` | `run_id` (required), `timeout_s` (default 600, clamped to 30..1800) | poll `run_status` every `MO_MCP_POLL_S` (default 5) until the status is terminal (`published`, `rolled_back`, `failed`) or the timeout passes; return the last status plus `{"terminal": bool, "waited_s": n}` and, when terminal, the run's `verdict.json` (if any) and the last 30 lines of `<home>/runs-inbox/<run_id>.launch.log`. |
| `stop_run` | `run_id` (required), `hard` (bool, default false) | `control.stop_run(home, db, run_id)` or `control.kill_run(...)`; return the result dict. |
| `certify` | `issue` (required), `base` (default `HEAD~1`), `head` (default `HEAD`) | run `[sys.executable, <engine root>/bin/mini-ork, "certify", "--repo", <project root>, "--base", base, "--head", head, "--issue", issue, "--json"]`, timeout `MO_MCP_CERTIFY_TIMEOUT_S` (default 900); return the parsed certificate JSON plus `exit_code`; on non-JSON output return stdout/stderr tails. |

All tools keep the existing error contract (error object, never a crash). `wait_for_run`
must not block the server for other requests longer than its own call (single-threaded
stdio is fine — the client issues one call at a time).

## Tests (extend `tests/unit/test_mcp_context_server.py`)

- without `--control`: `tools/list` has only the read-only five; calling `start_run` →
  `isError: true`.
- with control: `list_recipes` on a temp engine root (monkeypatch the root resolver);
  `start_run` with `launch_run` monkeypatched records recipe/kickoff/`MO_TARGET_CWD`;
  `ok: False` → error object; `run_status` maps events to node states; `wait_for_run`
  returns terminal immediately for a published run and times out (tiny poll + timeout)
  for a running one; `stop_run` hard/soft call the right control function; `certify` with
  the subprocess seam monkeypatched parses JSON and handles non-JSON output.
- end to end: `mini-ork mcp-context --control` subprocess lists the control tools.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_mcp_context_server.py tests/unit/test_native_dispatch_py.py` passes.
- `ruff check` clean; diff touches only the files in scope.
