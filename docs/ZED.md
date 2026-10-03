# mini-ork inside Zed

Zed runs agents through the Agent Client Protocol (ACP) and forwards MCP
servers to every agent in the editor. mini-ork ships both an ACP agent
(`mini-ork acp`) and a read-only MCP context server (`mini-ork mcp-context`);
this guide is the one-command wiring for the two, plus what you can do with
it today and where the web UI (`mini-ork serve`) still earns its keep.

---

## 1. Setup

```bash
# In the project you want to drive from Zed:
mini-ork init                # if you have not already — creates .mini-ork/

# One command, reversible, makes a timestamped backup before writing:
mini-ork zed setup
```

What it does:

- Resolves the absolute path of your `mini-ork` launcher (so the macOS GUI
  app, which does not inherit shell `PATH`, can still find it).
- Writes two entries to `~/.config/zed/settings.json`:
  - `agent_servers.mini-ork` for ACP (the **Agent Panel**)
  - `context_servers.mini-ork` for MCP (so the built-in Zed agent, Claude,
    Codex etc. all see runs, learnings, cost and lanes)
- Creates `settings.json.bak-<timestamp>` before mutating anything.

If your project lives at a non-default `MINI_ORK_HOME`, pass `--home <dir>`
so the entries embed it in both `env` maps.

Restart nothing. Zed reloads `settings.json` automatically. Open the project
in Zed, hit **Agent Panel** → **New Thread** → **mini-ork** and you are
in.

```bash
# See what is wired up:
mini-ork zed status

# Take it back out:
mini-ork zed uninstall
```

Both `setup` and `uninstall` accept `--dry-run` to print the resulting JSON
without writing it. If your existing `settings.json` is unparseable (Zed
allows `//` comments and trailing commas), setup refuses to overwrite and
prints the two blocks to merge by hand — return code 2, file unchanged.

---

## 2. What you get today

Once Zed is wired:

- **Start a run from a prompt.** Agent Panel → New Thread → mini-ork → type
  the kickoff in plain language. Each workflow node becomes a live tool
  call inside the thread, with cost and duration visible in real time.
- **Run history, including CLI-started runs.** Thread History → **Import
  Threads** lists every run of the project (whether you started it from
  inside Zed or via `mini-ork run` on the CLI). Opening one replays it.
- **Attach to a still-running run.** Opening a run that is currently in
  flight attaches and follows it: the thread shows live node tool calls
  as they happen.
- **Same data, every agent in Zed.** `mini-ork mcp-context` exposes runs,
  run detail, learnings, cost and lane map as MCP tools. The built-in
  Zed agent, Claude, Codex and any other MCP-aware agent in the editor
  see the same surface.

---

## 3. Coming next

Tracked in [`docs/plans/2026-10-03-zed-integration.md`](plans/2026-10-03-zed-integration.md):

- **Z3 — live agent output.** Per-node text, thinking and shell commands
  stream into the thread as the agent works.
- **Z4 — diffs in Review Changes.** When an implementer node ends, every
  changed file shows up under Zed's **Review Changes** panel.
- **Z5 — slash commands.** `/runs`, `/status`, `/learnings`, `/cost`,
  `/lanes`, `/stop`, `/resume`, `/recover`, `/certify`, `/serve`.
- **Z6 — recipe picker + live plan.** Pick the recipe (code-fix, docs,
  audit, …) from the thread; the run's DAG becomes a live checklist.

---

## 4. Troubleshooting

- **Run `mini-ork zed status` first.** It prints the settings path, whether
  both entries exist, whether the embedded command path exists and is
  executable, whether the `acp` extra is importable, and whether the
  current directory has a `.mini-ork/` (it must).
- **Open the ACP logs.** Command Palette → **dev: open acp logs**. The
  agent writes `agent-<node>.live.jsonl` per workflow node; this is where
  to look when a run stalls.
- **The project must contain `.mini-ork/`.** `mini-ork zed setup` writes
  settings, but each run needs its own home — run `mini-ork init` in the
  project root if you have not.
- **Worktree must be trusted.** Zed's workspace trust is per-folder. If
  the Agent Panel refuses to start a thread, check that the folder has
  been trusted.
- **`acp` extra missing.** `mini-ork zed setup` warns on stderr when the
  `acp` extra is not importable. Install with:
  ```bash
  pip install 'mini-ork[acp]'
  ```
- **macOS GUI cannot find `mini-ork`.** The launcher path is always
  absolute in the settings file; if you moved the binary, run
  `mini-ork zed setup` again to rewire.

---

## 5. What stays in the web UI

`mini-ork serve` (the ORK·COMMAND web UI) is still where the DAG graph and
the run-trajectory dashboards live — ACP cannot draw custom panels in
Zed. From a run in Zed, jump to the web UI with `mini-ork serve` and open
`http://127.0.0.1:7090`; from there, the run forensics page links back
into the same run id you have open in the Agent Panel.

See [`docs/UI.md`](UI.md) for the full reading guide.