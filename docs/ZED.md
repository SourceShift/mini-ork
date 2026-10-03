# mini-ork inside Zed

Zed runs agents through the Agent Client Protocol (ACP) and forwards MCP
servers to every agent in the editor. mini-ork ships both an ACP agent
(`mini-ork acp`) — a conversation with the mini-ork orchestrator — and an MCP
context server (`mini-ork mcp-context`);
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

### Talk to the orchestrator

Agent Panel → **New Thread** → **mini-ork**, then say what you want in plain
language — the way you would ask a colleague. The thread talks to the mini-ork
**orchestrator**, an agent that:

- reads the project (Read, Grep, Glob only — it never edits files itself);
- checks past runs and learnings before doing anything;
- picks a recipe, writes the kickoff, starts the run, waits for it, and tells
  you in plain words what changed, whether verification passed, and what it cost;
- can stop a run or certify a change when you ask.

Every change goes through a mini-ork run, so it is verified and rolled back on
failure exactly as from the CLI. The conversation continues across prompts.

### The pickers

The thread header has three pickers, like other Zed agents:

| Picker | Values | Default |
|---|---|---|
| **Mode** | *Orchestrate* (talk to the orchestrator) or *Direct run* (each prompt is a run kickoff) | `MO_ACP_DEFAULT_MODE`, else Orchestrate |
| **Model** | the lane the orchestrator runs on: Opus and Sonnet through your Claude subscription, plus every other `claude`-CLI lane in `providers.yaml` (GLM-5.3, MiniMax-M3, deepseek, …) | `MO_ORCHESTRATOR_LANE`, else the `orchestrator` role in `.mini-ork/config/agents.yaml`, else Opus |
| **Recipe** | every recipe (`code-fix`, `docs`, audits, research, …) — used by Direct run and `/run` | `MO_ACP_RECIPE`, else `code-fix` |

`/run <task>` skips the conversation for one prompt and starts the selected
recipe directly.

### Runs stream into the thread

A run the orchestrator starts (or you start with Direct run or `/run`) appears
in the thread as a tool call `run <id> (<recipe>)`. Under it, each workflow node
is its own tool call with the agent's live output — commands, file reads and
edits, its text — as it works. The run's call closes as completed when it
publishes and as failed otherwise, with a one-line result. The thread
shows one running cost: the orchestrator's turns plus every run it started,
including stage spend (judges, learning) — the same ledger the budget guard
reads.

Stopping a turn stops the conversation turn; runs it already started keep
going — ask the orchestrator to stop one. Stopping a Direct run stops the run.

### History

Thread History → **Import Threads** lists your orchestrator threads (titled by
their first prompt) and every run of the project, including runs started from
the CLI. Opening a thread replays it — your prompts, the orchestrator's
answers and tool calls, and each run with its nodes and agent output — and the
next prompt continues the same conversation. Opening a run replays it, and
follows it live if it is still going.

### Same data for every agent in Zed

`mini-ork mcp-context` gives every MCP-aware agent in the editor (Zed's own
agent, Claude, Codex, …) read-only tools: `list_runs`, `run_detail`,
`learnings`, `cost`, `lanes`. The orchestrator runs it with `--control`, which
adds `list_recipes`, `start_run`, `run_status`, `wait_for_run`, `stop_run` and
`certify`; the default server stays read-only.

---

## 3. Coming next

Tracked in [`docs/plans/2026-10-03-zed-integration.md`](plans/2026-10-03-zed-integration.md):

- **Z4 — diffs in Review Changes.** When an implementer node ends, every
  changed file shows up under Zed's **Review Changes** panel.
- **Z5 — slash commands.** `/runs`, `/status`, `/learnings`, `/cost`,
  `/lanes`, `/stop`, `/certify`, `/serve`, `/help`.
- **Z6 — live plan.** The run's DAG as a live checklist in the thread.

---

## 4. Troubleshooting

- **Run `mini-ork zed status` first.** It prints the settings path, whether
  both entries exist, whether the embedded command path exists and is
  executable, whether the `acp` extra is importable, and whether the
  current directory has a `.mini-ork/` (it must).
- **Open the ACP logs.** Command Palette → **dev: open acp logs**. Each run
  writes `.mini-ork/runs/<run id>/agent-<node>.live.jsonl` per workflow node;
  orchestrator threads are kept in `.mini-ork/acp-threads/`. This is where to
  look when a run stalls.
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
- **"Orchestrator turn failed (rc=…)".** The orchestrator runs the `claude`
  CLI on the chosen lane. For Opus/Sonnet, check that `claude` is logged in
  to your subscription (`claude` → `/login`); for other lanes, that their key
  is in your secrets file. Switch the Model picker to test another lane.
- **What Claude settings the orchestrator uses.** It is a `claude` process
  run with `--setting-sources project,local`: the project's `.claude/`
  settings and `CLAUDE.md` apply, your personal `~/.claude` ones (hooks,
  global instructions) do not. Set `MO_ORCHESTRATOR_SETTING_SOURCES` to
  change that (empty = everything, like plain `claude`).
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