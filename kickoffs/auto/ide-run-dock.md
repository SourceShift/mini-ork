# Run page v2: the right-hand Run panel's data — Changes, Checks, Agents, Cost, Learned (Orca right sidebar)

## Why

The plan is `/Users/admin/.claude/plans/twinkling-churning-feather.md` (phase P2).

Orca's right sidebar puts **Source Control** (changed files with +/−, merge) and **Checks**
(triage line, pass/fail counts, expandable check rows) next to the session. The mini-ork IDE
will get a right dock panel that follows the open run. The panel loads:

```
mini-ork board page run --arg run=<id> --arg view=dock --tab <changes|checks|agents|cost|learned>
```

and draws it compact and single-column. This run builds that data in Python.

**Already on main (reuse, do not re-derive):**

- `spec.py` v2 helpers: `files`/`file_entry`, `checks`/`check_row`, `findings`/`finding`,
  `agents`/`agent_row`, `triage`, `meta_item`, plus `kv`, `bars`, `lst`.
- `outcome.resolve(run)`: text, tone, icon, counts, actions.
- `run_story.py`: per-node headline, files, checks and findings logic.
  - Factor shared helpers OUT of `run_story.py` only if needed. Prefer calling its existing
    private helpers.
- `node_changes`: `_load_diffs`, `_files_and_note`, `_commits`, `_review_items`, `_verifier_items`.
- `node._learning_view`, `run._attribute_calls`, `run._wall`, `run._providers`.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/run_dock.py` (new)
- `mini_ork/ide_pages/run.py`: ONLY `build`, with a branch at its top for
  `args.get("view") == "dock"` when `ide_level() >= 2`
- `tests/unit/test_ide_pages_run_dock.py` (new)

## Changes (exact)

### `run.build`, dock branch

Only when `ide_level() >= 2` and `args["view"] == "dock"`:

- Return `S.page("run", <run title>, sub=<state word · $cost · elapsed>, chips_=[],
  actions=[], tabs=DOCK_TABS, tab=t, args={"run", "view": "dock"}, sections=…)` plus the same
  top-level `graph`.
- `DOCK_TABS` = `changes` "Changes", `checks` "Checks", `agents` "Agents", `cost` "Cost",
  `learned` "Learned". The default is `changes`; an unknown tab → `changes`.
- Every section is built through `S.guarded`. No `full`/`col` layout: the panel is one column.

### `run_dock.build(run, tab) -> list[section]`

**changes**

- A one-line `S.triage` summary: `+a −r across n files`, plus `on <branch> · <k> commits ahead
  of <base>` when a workspace exists (`workspaces.status` / `run.workspace`).
  - Actions: **Merge into <base>** / **Discard**, reusing `run._actions`' merge/discard buttons
    and their confirms, only when the run is finished and the workspace exists.
- `S.files("Changed files", [file_entry…], diff=<unified diff, cap 600 lines>, diff_note,
  commits=[{sha, subject}])` from `_load_diffs` + `_files_and_note` + `_commits`.
  - Empty state: `S.lst("Changed files", [S.dot("No changes recorded for this run")])`.

**checks**

- `S.triage` from `outcome.resolve(run)`: text, tone, icon, counts, actions; no menu.
- One `S.checks(<verifier id>, rows)` per verifier node, with rows from its
  `verifier_<id>.json` checks (or `*.checks.tsv`). A failing row gets up to 8 log lines from
  `evidence_path` / `verifier_<id>.log`.
- `S.checks("Levels", rows)` from `run-verdict.json` (or `verdict.json`) `levels`:
  - PROVEN = pass;
  - REFUTED = fail;
  - UNVERIFIED = pending;
  - n/a = na.
- `S.findings("Review · <node>", …)` for each reviewer/lens node with a review JSON (findings
  first, then reasons).

**agents**

`S.agents("Pipeline", rows)`, one `S.agent_row` per node in `run.cols` order:
- id, state;
- lane = `node.family`;
- model = the providers.yaml `model` for that lane;
- step = the node headline;
- last = the last non-empty output line, max 140 chars, via `run._node_output`'s tail;
- cost, dur;
- do = `S.page_link("run", "graph", run=id, node=node.id)`.

**cost**

- `S.kv("Spend", …)`: this run's total (`run._cost`); calls; the run's `budget_cap_usd` from
  `run_profile.json` (or "no cap"); today's spend vs the daily cap, using
  `cost_ledger.spent_last_24h` and `MO_DAILY_BUDGET_USD` like `header.py:133-147`.
- `S.bars("By step", …)`: per node, `node.cost`, as a % of the run total.
- `S.bars("By lane", …)`: grouped by `node.family`, with colour `fam:<lane>`.

**learned**

- One `S.lst` item per node that has `learned/<node>.json`:
  - title = node id;
  - sub = `injected · <n> sources (<kinds>)` or `not injected · <reason>`;
  - mark ✓ green when injected, else • sub.
- An Open act (`S.open_path`) on `learned/<node>.md` when it exists.
- Empty state: "No learned context was recorded for this run".

### Rules

- **Read-only:** never write under the run dir.
- **Fail-soft per section** (`S.guarded`).
- **Fast:** each tab builds in under 1 s on a 9-node run.
- Level 1, and level 2 without `view=dock`, are unchanged.

## Tests (`tests/unit/test_ide_pages_run_dock.py`, temp fixtures like `test_ide_pages_run.py` / `test_ide_pages_run_story.py`)

- **Back-compat:** `view=dock` at level 2 → the tab keys are exactly changes/checks/agents/cost/learned;
  without `MINI_ORK_IDE_SPEC` → today's page; level 2 without view → the story page.
- **changes:** a run with a `framework-edit.diff` → a `files` section with per-file +/−; no diff
  → the empty-state list.
- **checks:**
  - a `verifier_test.json` with one failing check → a `checks` section with `summary.failing == 1`;
  - a reviewer findings JSON → a `findings` section;
  - levels → a "Levels" checks section.
- **agents:** rows in `run.cols` order with lane and do.
- **cost:** by-lane bars sum to the run total (±0.01).
- **learned:** an injected node shows ✓ with its source count.
- **Read-only:** a dir snapshot is unchanged after building all five tabs.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_ide_pages_run_dock.py tests/unit/test_ide_pages_run.py tests/unit/test_ide_pages_run_story.py tests/unit/test_ide_pages_outcome.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/ide_pages/run_dock.py mini_ork/ide_pages/run.py tests/unit/test_ide_pages_run_dock.py` → clean.
- **Live proof (read-only):** for each of the 5 tabs, run
  `MINI_ORK_IDE_SPEC=2 bin/mini-ork board page run --arg run=sdd-i3-kickoff-contract-20261007181747 --arg view=dock --tab <t> --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork`
  and print the section types and titles. Paste them with each tab's wall time.
- `git diff --stat` touches only the files in scope.
