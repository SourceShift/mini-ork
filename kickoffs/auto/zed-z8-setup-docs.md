# Zed Z8 — `mini-ork zed setup|status|uninstall` and docs/ZED.md

Plan: `docs/plans/2026-10-03-zed-integration.md`.

## Goal

Today a user has to hand-edit `~/.config/zed/settings.json` to get mini-ork into Zed.
Make it one command, reversible, and documented. Two entries are involved:

```json
"agent_servers":   { "mini-ork": { "type": "custom", "command": "<mini-ork>", "args": ["acp"], "env": {} } },
"context_servers": { "mini-ork": { "command": "<mini-ork>", "args": ["mcp-context"], "env": {} } }
```

## Files in scope

- `mini_ork/cli/zed_cmd.py` — NEW: `main(rest, root) -> int`.
- `mini_ork/cli/main.py` — register `"zed": "mini_ork.cli.zed_cmd"` in `_NATIVE_MODULE_SUBS`
  and one help line.
- `tests/unit/test_native_dispatch_py.py` — add `"zed"` to the exact-set guard.
- `tests/unit/test_zed_cmd.py` — NEW.
- `docs/ZED.md` — NEW user guide.

No other file changes.

## Mechanism (exact spec)

Settings path: `$ZED_SETTINGS` if set, else `~/.config/zed/settings.json` (macOS/Linux).
Command path written into settings: the absolute path of the `mini-ork` launcher —
`shutil.which("mini-ork")`, else `~/.local/bin/mini-ork` if it exists, else
`<root>/bin/mini-ork`. macOS GUI apps do not see the shell PATH, so never write a bare
`mini-ork`.

- `mini-ork zed setup [--home <dir>] [--dry-run]`
  - read the file; missing → start from `{}`; parse with `json.loads`. If it does not
    parse (Zed allows comments / trailing commas) → do NOT write: print the two JSON
    blocks to merge by hand and return 2.
  - set `agent_servers["mini-ork"]` and `context_servers["mini-ork"]` exactly as above,
    preserving every other key; `--home <dir>` adds `"MINI_ORK_HOME": <abs dir>` to both
    `env` maps (otherwise `env` is `{}` — the ACP agent takes the home from the project
    Zed has open).
  - before writing, copy the existing file to `settings.json.bak-<YYYYmmdd-HHMMSS>`;
    write with `indent=2`; print what changed.
  - `--dry-run` prints the resulting JSON and writes nothing.
  - warn (stderr, not fatal) when the `acp` extra is not importable
    (`importlib.util.find_spec("acp") is None`): "run: pip install 'mini-ork[acp]'".
- `mini-ork zed status` — print: settings path, whether each entry exists and its command,
  whether that command path exists and is executable, whether `acp` is importable, and
  the project home for the current directory (`./.mini-ork` exists?). Return 0.
- `mini-ork zed uninstall [--dry-run]` — remove both `mini-ork` entries (leave everything
  else, drop now-empty `agent_servers` / `context_servers` maps), with the same backup
  and parse-failure rules. Return 0 when nothing to remove.
- `--help` → usage on stderr, 0; unknown subcommand → usage, 2.

## docs/ZED.md (user guide, plain language)

1. Setup: `mini-ork zed setup`, restart nothing (Zed reloads settings), open a project
   that has `.mini-ork/` (`mini-ork init`), Agent Panel → new thread → **mini-ork**.
2. What you get today: start a run from a prompt; each workflow node as a live tool call
   with cost; Thread History → Import Threads lists every run of the project (including
   CLI-started ones) and opening one replays it; opening a run that is still going
   attaches and follows it; `mini-ork mcp-context` gives every agent in Zed (Zed agent,
   Claude, Codex) tools for runs, run detail, learnings, cost and lanes.
3. Coming next (link the plan): live agent output, diffs in Review Changes, slash
   commands, recipe picker, live plan.
4. Troubleshooting: `mini-ork zed status`; Command Palette → `dev: open acp logs`; the
   project must contain `.mini-ork/`; worktree must be trusted in Zed.
5. What stays in the web UI (`mini-ork serve`): the DAG graph and dashboards.

## Tests (`tests/unit/test_zed_cmd.py`)

Use `ZED_SETTINGS` pointing at a temp file. Cases: setup on a missing file creates both
entries with an absolute command path; setup preserves unrelated keys and an existing
unrelated agent; a backup file is created; `--home` adds `MINI_ORK_HOME` to both envs;
`--dry-run` writes nothing; a file with `//` comments is left byte-identical and the
command returns 2 printing both blocks; status reports configured / not configured;
uninstall removes only the mini-ork entries and drops empty maps; unknown subcommand → 2.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_zed_cmd.py tests/unit/test_native_dispatch_py.py` passes.
- `ruff check` clean on touched files; diff touches only the files in scope.
