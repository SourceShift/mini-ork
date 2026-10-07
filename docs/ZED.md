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

On the first thread, if something is missing — no `.mini-ork` in the project,
no `claude` CLI or login for the orchestrator, no key for a worker lane — Zed
offers **Set up mini-ork**, which opens `mini-ork acp --setup` in a terminal: it
checks each of those, says what is wrong, and offers to fix it (`mini-ork init`,
`claude auth login`, `mini-ork providers configure <lane>`). You can run it
yourself any time.

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

The thread header has four pickers, like other Zed agents:

| Picker | Values | Default |
|---|---|---|
| **Mode** | *Orchestrate* (talk to the orchestrator) or *Direct run* (each prompt is a run kickoff) | `MO_ACP_DEFAULT_MODE`, else Orchestrate |
| **Model** | the lane the orchestrator runs on: Opus and Sonnet through your Claude subscription, plus every other `claude`-CLI lane in `providers.yaml` (GLM-5.3, MiniMax-M3, deepseek, …) | `MO_ORCHESTRATOR_LANE`, else the `orchestrator` role in `.mini-ork/config/agents.yaml`, else Opus |
| **Recipe** | every recipe (`code-fix`, `docs`, audits, research, …) — used by Direct run and `/run` | `MO_ACP_RECIPE`, else `code-fix` |
| **Workspace** | *New worktree per task* or *In place (this checkout)* | `MO_WORKSPACE_MODE`, else a new worktree |

`/run <task>` skips the conversation for one prompt and starts the selected
recipe directly.

### The thread list shows each task's state

Every thread and run title starts with its state, updated live:

| Mark | Meaning |
|---|---|
| ● | working — the title also names the current step |
| ✋ | needs you — a cost pause, or a finished change waiting for your review |
| ✓ | done — with the size of the change, e.g. `✓ Fix login redirect +12 −3` |
| ✗ | failed |

So the thread list in the Agent Panel works as a task board: start several
threads, and the ✋ ones are the ones waiting for you.

### The task board: Zed's own panels show every task

mini-ork hands each finished change to Zed the way Zed's own agent does —
as **agent edits** — so it shows up in Zed's panels, not just in the chat:

- the **Threads Sidebar** row of the thread: its status while the run works
  and the change's +/− when it is done;
- the changed-files bar above the message box and **Review Changes**, where
  you **Keep** or **Reject** each change;
- the **Git panel**, as uncommitted changes in your project;
- Zed's notification when the thread finishes.

How it works: each run works in its own git worktree (your files are not
touched while it runs). When it finishes verified, mini-ork writes each
changed file into your project through Zed and removes the worktree. If you
edited one of those files yourself while the run worked, or the run deleted
or produced a binary file, nothing is written and the run's worktree is kept
— the buttons under the run (**Merge into `<branch>`**, **Discard changes**,
**Keep for later**) take over, and `/workspaces`, `/merge [run]`,
`/discard [run]` work any time.

`mini-ork zed setup --layout` docks threads and the agent on the left and the
Git and Project panels on the right (Zed: **Panel Layout > Agentic**). For
parallel tasks, start each thread in its own worktree from the worktree
picker in the title bar. *In place* (Workspace picker) skips the worktree
and edits your checkout directly. If `.mini-ork/worktree-setup.sh` exists it
runs in each new worktree first (copy `.env`, install dependencies).

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

### What the run changed, its plan, and slash commands

- **Diffs.** When a run's implementer finishes, every changed file appears as a
  diff in the thread and in Zed's **Review Changes**. Opening an old run shows
  the diff recorded when it ran, not today's files.
- **Live plan.** The run's workflow shows as a checklist above the thread —
  each node pending, in progress, completed. In a thread with several runs it
  follows the latest one.
- **Slash commands.** Type `/` for the list; `/help` prints it. Commands
  answer in the thread and never start a run (except `/run` and
  `/automation run`); those that act on a run use the thread's latest run
  unless you name one.

### Check the kickoff first: `/kickoff`

`/kickoff <task>` (or "write me a kickoff for …, I want to check it
first") has the orchestrator read the recipe and the code and draft the
kickoff a run will receive — what to do, the real files in scope (new
ones marked `(new)`), and the commands that prove success. The thread shows
it as a new file plus mini-ork's own checks: paths that do not exist, no
success section, sections the recipe's examples have and yours lacks. If
another recipe fits better, the orchestrator says so. You decide with
**Start run**, **Save only** (to `.mini-ork/kickoffs/`), **Change
something** or **Discard**.

### Race models: `/race`

`/race <task>` runs the thread's recipe on several models at once —
`sonnet`, `glm` and `minimax` unless you name them (`/race sonnet,glm
<task>`; `MO_RACE_LANES` sets your default). Each works in its own
worktree and goes through the recipe's checks, so "best" means verified.
When all finish, one table shows each model's result, change, cost and
time, and you pick: **Keep <model> (+a −r, $cost)** merges that change and
discards the others, **Decide later** keeps them all (`/merge`,
`/discard`), **Discard all** removes them. It costs about one run per
model. Stopping the turn stops every model.

### Runs at a glance

- **`/runs`** — every run of the project: state, recipe, current step,
  age, cost and the size of its change. Filter by typing what you want:
  `/runs needs-you`, `/runs failed recipe:code-fix 50`. The header counts
  each state.
- **`/status [run]`** — one run's card: state, steps, cost by stage, the
  files it changed, the verifier's verdict and what it learned.
- `/cost`, `/learnings`, `/lanes`, and `/stop`, `/kill`, `/resume`,
  `/recover`, `/certify <bug report>` for a run.

### Recipes

- **`/recipes [project|engine] [text]`** — every recipe with its source,
  steps, grade, number of runs, success rate and average cost. Project
  recipes (`.mini-ork/recipes/`) override engine recipes of the same name.
- **`/recipe <id>`** — the recipe card: what it does, its steps and flow,
  what it must produce, its grade and track record, with links to its files.
- **Create one by asking**: "I want a recipe that audits our SQL
  migrations", or `/recipe new <what it should do>`. The orchestrator asks a
  few questions — what a run receives, the steps, the command that proves
  success, which models — then shows the draft: every file as a diff, plus
  its grade. You decide with **Create recipe**, **Change something** or
  **Discard draft**. A created recipe lands in `.mini-ork/recipes/<id>/`,
  appears in the Recipe picker, and **Test it now** runs it once on its
  example kickoff.
- **`/recipe edit <id>`** — changes a recipe the same way. An engine recipe
  is first copied into the project (**Copy into this project**); the copy
  overrides the engine's.

### Automations

Recipes that run on a schedule — a nightly dependency check, a weekday
changelog entry, a weekly dead-code sweep — whether or not Zed is open.

- **Create one by asking**: "run changelog-entry every weekday at 9", or
  `/automation new <what and when>`. The orchestrator picks the recipe,
  says the schedule back in words, writes what each run should do, and
  shows a proposal: the recipe, when, the next three times, the kickoff.
  **Create automation** saves it; if the scheduler is off you are asked to
  turn it on.
- **The scheduler** is one small background job per project that checks
  every minute — a LaunchAgent on macOS, a crontab line on Linux. It is
  installed only when you say so (**Turn on the scheduler**, or
  `/automation scheduler on`) and removed with `/automation scheduler off`.
  A firing missed while the machine sleeps is skipped, not caught up.
- **`/automations`** — every automation: when, next run, last run and its
  state, and whether the scheduler is on. **`/automation <id>`** — its card
  with recent runs. `/automation run <id>` fires it now; `pause`, `resume`,
  `delete` do what they say.
- Each firing is an ordinary run in its own worktree, so it shows up in the
  thread list and waits for your review like any other task.
- The same from the terminal: `mini-ork automations list|add|remove|pause|resume|run|tick`
  and `mini-ork automations scheduler status|install|uninstall`.

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
`learnings`, `cost`, `lanes`, `describe_recipe` and `list_automations`. The
orchestrator runs it with `--control`, which adds `list_recipes`,
`start_run`, `workspaces`, `run_status`, `wait_for_run`, `stop_run`,
`certify`, and the authoring tools `recipe_guide`, `draft_recipe`,
`get_recipe_spec` and `propose_automation`. Drafts and proposals never take
effect on their own: you create them with the buttons in the thread. The
default server stays read-only.

---

## 3. Coming next

- **One-click install from Zed's agent list (ACP Registry).** Needs mini-ork on
  PyPI first; the registry installs Python agents with `uvx`.

---

## 4. Troubleshooting

- **Run `mini-ork acp --setup` in the project first.** It checks the
  project, the orchestrator's Claude login and the worker lanes' keys.
- **Run `mini-ork zed status`.** It prints the settings path, whether
  both entries exist, whether the embedded command path exists and is
  executable, whether the `acp` extra is importable, and whether the
  current directory has a `.mini-ork/` (it must).
- **Open the ACP logs.** Command Palette → **dev: open acp logs**. Each run
  writes `.mini-ork/runs/<run id>/agent-<node>.live.jsonl` per workflow node;
  orchestrator threads are kept in `.mini-ork/acp-threads/`. This is where to
  look when a run stalls.
- **The Claude CLI's startup lines are not the agent's output.** On a gateway
  lane (`deepseek`, `glm`, `kimi`, `minimax`) the harness prints an
  auth-precedence notice and a model-registry warning to stderr before it does
  any work. They are advisory and harmless, and mini-ork withholds them from
  the live view (`_HARNESS_STARTUP_NOISE` in `mini_ork/dispatch/core.py`) so a
  node still starting up does not show warnings where its output belongs. A
  node whose panel is *empty* is idle, not erroring; the failure text for a
  lane that did fail still leads with the API error from its stdout envelope.
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
- **An automation does not fire.** `/automation scheduler` says whether
  the scheduler is on and when it last ticked; its output goes to
  `.mini-ork/automations-tick.log`, and each firing is a line in
  `.mini-ork/automations.log`. The scheduled job keeps the `PATH` of the
  shell that installed it (launchd and cron start with a bare one); after
  installing a new CLI, turn the scheduler off and on again.
- **"claude CLI not found" when Zed is opened from the Dock.** Apps
  started from the Dock get a bare `PATH`. `mini-ork zed setup` writes the
  `PATH` of the shell you run it from into both Zed entries; run it again
  (and restart Zed) after installing a new CLI.
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