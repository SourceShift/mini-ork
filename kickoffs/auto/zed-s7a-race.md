# Zed S7a — /race: the same task on 2–3 models, each in its own worktree; keep the best verified change

Depends on S4 (task worktrees), S5 (merge/discard) and S6b-2 merged. Serial with the other
slices that edit `mini_ork/acp/agent.py`.

## Goal

For a task that matters, one model's attempt is a sample, not an answer. `/race <task>`
runs the thread's selected recipe on two or three models at once, each in its own git
worktree, streams all of them into the thread, and when they finish shows one table —
which ones verified, how much each changed, what each cost — and lets the user keep one
change with a button. The rest are discarded. Every attempt goes through the recipe's own
checks, so "best" means "verified", not "looks good".

## Files in scope

- `mini_ork/acp/race.py` — NEW: argument parsing, lane choice, run-config seeding, the table.
- `mini_ork/acp/agent.py` — the `/race` turn: launch, follow, decide.
- `mini_ork/acp/commands.py` — announce `race`; help text.
- `tests/unit/test_acp_race.py` — NEW; `tests/unit/test_acp_agent_py.py`,
  `tests/unit/test_acp_commands.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `mini_ork/acp/race.py`

```python
DEFAULT_LANES = ("sonnet", "glm", "minimax")
def known_lanes(home: Path) -> list[str]
def parse_race_arg(arg: str, home: Path) -> tuple[list[str], str] | str
def model_roles(home: Path, recipe: str) -> list[str]
def seed_run_config(home: Path, run_id: str, recipe: str, lane: str) -> Path
def render_race_table(rows: list[dict]) -> str
```

- `known_lanes`: the lane names of the effective provider registry for this home (read
  how `/lanes` and `mini_ork.dispatch.providers` list them; do not hard-code).
- `parse_race_arg`: when the first token is a comma-separated list whose items are all known
  lanes → those lanes (2–4, deduplicated, order kept) and the rest is the task; otherwise
  the lanes are `MO_RACE_LANES` (comma list) or `DEFAULT_LANES`, filtered to known lanes,
  first 3. Errors (returned as the message string): a listed lane that is unknown → `"Unknown
  lane <x>. Known: <list>."`; fewer than 2 lanes → `"Racing needs at least two lanes — name
  them: /race sonnet,glm <task>."`; empty task → `"What should they do? /race <task>"`.
- `model_roles`: the `model_lane` of every `implementer` node in the recipe's workflow
  (resolve the recipe directory home-first, as runs do — `mini_ork.planning.recipe_plan.
  recipe_dir`), plus `"implementer"` and `"worker"`, deduplicated.
- `seed_run_config`: write `<home>/runs/<run_id>/config/agents.yaml` = the home's EFFECTIVE
  agents policy (`mini_ork.dispatch.agents_config.effective_path()` with `MINI_ORK_HOME`
  = home) with `lanes[role] = lane` for every role in `model_roles`. A run never overwrites
  an existing snapshot (`config_resolve.snapshot_run_config` is idempotent), so this pins
  the run's implementer lane for its whole life. **Find the function that turns an
  implementer node's `model_lane` into the lane it dispatches on for a run, and prove the
  pin through it in a test** (with `MINI_ORK_RUN_DIR` = the seeded run dir and the home's
  own agents.yaml mapping the role to a different lane). If the run snapshot is not
  consulted for that resolution, say so in the summary and pin through the mechanism that
  IS consulted — do not ship a pin that only looks right in the YAML.
- `render_race_table(rows)` — each row `{lane, run_id, status, added, removed, cost_usd,
  seconds}` → `| | model | result | change | cost | time |`: mark ✓ published / ✗ failed or
  rolled back / ● still running; result `verified` / `failed` / `rolled back`; change
  `+a −r` (`—` when none); cost `$0.42`; time `3m 12s`.

### The `/race` turn (agent)

- `/race …` is routed like `/run` (read `_strip_slash_run` and mirror it): only in thread
  sessions (a run session → `"Races start in a mini-ork thread."`). Parse with
  `parse_race_arg`; an error string → that message, end turn. The recipe is the thread's
  Recipe picker. The project must be a git repo (`workspaces.create` raising →
  `"Racing needs a git project — each model works in its own worktree."`).
- First message: `"Racing <n> models on <recipe>: <lanes>. Each works in its own worktree;
  you keep one change. It costs about <n>× one run."`.
- For each lane: `mint_run_id()`, `workspaces.create(project, home, rid)`,
  `seed_run_config(home, rid, recipe, lane)`, register the run exactly as direct mode does
  (`_sessions[rid] = worktree`, `_recipes`, `_routes`, `_thread_runs`), and launch through
  the launcher seam with extra env `MO_ROUTING_POLICY=workflow_default` (learning-governed
  routing may remap unpinned nodes; a race must not) — add a minimal per-run env
  pass-through to `_launch` (`self._run_env.get(run_id)` merged into `extra_env`). Marker
  title: `race <i>/<n> · <lane> — run <rid> (<recipe>) · worktree <branch>`. A lane whose
  launch fails → its marker `failed` with the error, its worktree discarded; the race
  continues when at least one launched.
- Follow every launched run concurrently (`asyncio.gather` over `_await_terminal`; the live
  output streams under each marker as for any run). Close each marker `completed` /
  `failed`; do NOT offer the per-run S5 review buttons for race runs. Cancelling the turn
  stops every race run that is still going (the same stop seam direct mode uses).
- When all have ended: a message with `render_race_table` (cost = the run's spend as
  `/status` computes it; time = first to last event; change = `workspaces.status`).
- Decision: the candidates are the runs whose task state is ready to review (published
  with changes). None → `"No model produced a verified change. Their worktrees are kept for
  a look: /workspaces lists them, /discard <run> removes one."`. Otherwise
  `request_permission` on tool call `race:<first run id>` (emit its `ToolCallStart` titled
  `Pick the change to keep`, kind `edit`) with one option per candidate, `keep:<rid>`
  named `Keep <lane> (+a −r, $cost)` (allow_once), then `later` ("Decide later",
  reject_once) and `discard` ("Discard all", reject_always).
  - `keep:<rid>` → `workspaces.merge(ws, message=<thread title, else the task's first line>
    + f" (mini-ork race, {lane})")`; ok → discard every other race run's workspace →
    `"Merged <lane>'s change into <base branch> (<fast-forward | merge commit> <short
    sha>). Discarded the other <k>."`; a refused merge → its error + `"Nothing was
    discarded."`.
  - `later` / dismissed → `"All <n> kept: /merge <run> keeps one, /discard <run> drops
    one."`.
  - `discard` → discard all → `"Discarded all <n>."`.

### Commands

Announce `race` ("Race the selected recipe on 2–3 models, each in its own worktree; keep
the best verified change.", hint `"[lane,lane] task — default sonnet,glm,minimax"`), and
list it in `/help`.

## Tests

`tests/unit/test_acp_race.py`: parse (explicit lanes, defaults, `MO_RACE_LANES`, unknown
lane, one lane, empty task, a task whose first word merely contains a comma); `model_roles`
from a temp recipe with two implementer nodes; `seed_run_config` + the dispatch resolver
returns the race lane for that run while the home maps the role elsewhere; table rows and
formats.

`tests/unit/test_acp_agent_py.py` (no pytest-asyncio — `asyncio.run`; keep the autouse
guard; a real temp git project; a fake launcher that edits each worktree; a scripted
snapshot reader driving each run to its terminal status; a `_PermConn`-style fake
connection): three lanes → three markers with the race titles and the
`MO_ROUTING_POLICY` env passed; two published + one failed → the table and two `keep`
options plus later/discard; `keep` merges that change into the project and the other
worktrees and branches are gone; `later` keeps all; `discard` removes all; no candidate →
the kept-for-a-look message and no buttons; one lane failing to launch → the race goes on
with two; a non-git project → the message, nothing launched; no per-run review buttons;
a run session → the thread-only message.

`tests/unit/test_acp_commands.py`: `race` is announced and in `/help`.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_race.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_commands.py tests/unit/test_workspaces.py` passes.
- `ruff check` clean; diff touches only the files in scope.
