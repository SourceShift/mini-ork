# remote-nodes-03 — Path map and portable transport argv

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
decision **D4** (fixed in-sandbox roots, one translation seam). Depends on
epic 01 (`RunRoots`). Testable locally: it makes the *existing* `docker`
scope=agent path actually work, which it does not today.

## Goal

When an agent CLI is spawned into an isolated workspace, nothing host-specific
reaches it untranslated. cwd, argv, env values, prompt text and the MCP config
all refer to the D4 roots (`/workspace/target`, `/workspace/run`,
`/workspace/home`, `/workspace/mo-home`, `/opt/mini-ork`). Any absolute host
path that survives translation fails loud.

## Why (traced 2026-10-01)

- `_spawn_in_workspace` (`mini_ork/dispatch/core.py:243`) passes the host cwd
  straight through. `DockerWorkspace.spawn` uses it as `docker exec -w <host
  path>` (`runtime/backends/docker.py:203-205`), while the drive is mounted at
  `/workspace` (`runtime/agent_workspace.py:149`). Working directory and mount
  disagree.
- Host paths leak through four channels:
  - **argv:** claude `--mcp-config <run_dir>/.mcp-config.json`
    (`providers.py:1280`, `:1332`). Its contents list host MCP commands
    (`:1250-1255`).
  - **Python interpreter:** the transports run `sys.executable`, a host
    interpreter path. `openai-chat` and `uhp` also run a host script path
    (`providers.py:596`, `:628`). codex and opencode already use `-m`
    (`providers.py:634-653`).
  - **env:** `MO_TARGET_CWD` (`execute_handlers.py:751`),
    `MO_USAGE_FILE`/`MO_COST_FILE` (`providers.py:1531`), `MO_WORKFLOW_YAML`,
    `MINI_ORK_HOME`, `MINI_ORK_DB` (allowlisted at `_workspace_env.py:91-100`)
    and `PYTHONPATH` (`providers.py:726`, dropped by the allowlist).
    `MINI_ORK_RUN_DIR` is *not* forwarded, yet framework-edit prompts read
    `${MINI_ORK_RUN_DIR}/lens-*.md`.
  - **prompt text:** the scope guard run dir (`execute_handlers.py:550`), the
    `{out_file}` path (`:679`), `{impl_log}` (`:739`), `{review_file}`
    (`:847`) and input-manifest paths (`mini_ork/workflow/artifacts.py:217-221`).
- `core.dispatch` re-seeds env from a full `context_env_snapshot()`
  (`core.py:294`). That is the P5 violation cloud-swarm calls out.
- The only translator today is `_host_to_container` (`mini_ork/cli/spawn.py:311`),
  and only recursive spawn uses it.

## Requirements

1. New module `mini_ork/runtime/path_map.py`:
   - `PathMap.from_roots(roots: RunRoots, *, agent_home: str) -> PathMap`
     holds an ordered list of (host_prefix → sandbox_prefix) pairs. The
     longest prefix wins. Prefixes are realpath-normalized, so macOS
     `/private/var` vs `/var` and symlinked `/Volumes` paths map correctly.
   - Methods: `path(p)`, `argv(list)`, `env(dict)` (values only),
     `text(str)` (prompt rewrite of any occurrence of a mapped prefix), and
     `json_file(src, dst)` (rewrites string values, used for `.mcp-config.json`).
   - `assert_no_host_paths(argv, env, text, *, forbidden_prefixes)` raises
     `UnmappedHostPathError` naming the channel and the offending value.
     `forbidden_prefixes` defaults to `$HOME`, `/Users`, `/Volumes`,
     `/private`, `/home` and the four host roots.
   - Generalize `_host_to_container` to delegate to `PathMap` while keeping
     its signature and errors. Its existing tests must stay green.
2. Apply the map in `_spawn_in_workspace`, and only there. Add a `path_map`
   field to `DispatchRequest` that the executor fills with the run's map when
   the workspace is isolated. Translate cwd, argv, env and stdin (prompt) via
   the map, then run `assert_no_host_paths`. Under `remote` the check raises.
   Under `docker`/`local` it logs a WARN per leaked value, so existing docker
   users are not broken.
3. Portable transport argv: when the workspace is isolated, rewrite the
   interpreter to `python3`. `openai-chat`/`uhp` script paths become
   `-m mini_ork.dispatch.openai_chat_transport` / `-m mini_ork.dispatch.uhp`
   (both modules have `__main__` guards). Set `PYTHONPATH=/opt/mini-ork` in the
   sandbox env. The host path stays byte-identical.
4. MCP config: write a translated copy of `.mcp-config.json` into the run dir
   as `.mcp-config.sandbox.json`, and pass that path through the map. Drop any
   MCP server whose command does not exist under `/opt/mini-ork` or on the
   image's `PATH` per a static allowlist (`MO_SANDBOX_MCP_ALLOW`), and log a
   WARN. Add a stub entry so `--strict-mcp-config` still holds, matching the
   existing no-op-stub pattern (`providers.py:1239-1240`).
5. Env contract inside the sandbox:
   - Forward `MINI_ORK_RUN_DIR` (mapped).
   - Set `MINI_ORK_HOME=/workspace/mo-home`.
   - **Unset** `MINI_ORK_DB`.
   - Set `MO_REMOTE_NODE=1` and `MINI_ORK_ALLOW_CHILD_SPAWN=0`.

   Extend `_RUN_CONTRACT_KEYS` in `_workspace_env.py` as an exact-name set, not
   a prefix (the S1 lesson). Agent-side helpers that would open state.db must
   check `MO_REMOTE_NODE` and exit with a clear message. Add the check to the
   steering MCP server entry (`mini_ork/steering/mcp_server.py`).

## Files in scope (touch ONLY these)

- `mini_ork/runtime/path_map.py` (new)
- `mini_ork/dispatch/core.py` (`_spawn_in_workspace`, `DispatchRequest.path_map`)
- `mini_ork/dispatch/providers.py` (transport argv + MCP sandbox copy, isolated branch only)
- `mini_ork/runtime/backends/_workspace_env.py` (run-contract keys)
- `mini_ork/cli/spawn.py` (`_host_to_container` delegates)
- `tests/unit/test_path_map.py` (new)

## Out of scope

- Moving files to the remote side (epics 07 and 08). This epic only makes
  references correct.
- The remote backend itself (epic 06).

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_path_map.py tests/unit/test_spawn_workspace_isolation_py.py tests/unit/test_workspace_env.py tests/unit/test_docker_spawn.py && make lint
```

## Acceptance

- A captured `docker` spawn of an implementer node contains no string starting
  with any forbidden prefix in argv, env values or stdin. The test uses a fake
  backend that records the request.
- Under backend `remote` (stubbed via a registered fake named `remote`), a
  deliberately unmapped prompt path raises `UnmappedHostPathError` naming the
  channel.
- The `openai-chat` argv under isolation is
  `python3 -m mini_ork.dispatch.openai_chat_transport …`. On host it is
  unchanged (byte parity test).
- `MINI_ORK_DB` is absent, and `MO_REMOTE_NODE=1` is present, in the isolated
  env.
- The existing recursive-spawn isolation tests stay green.
