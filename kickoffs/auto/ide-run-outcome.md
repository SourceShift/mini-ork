# Run page v2: one true outcome, the next action next to it, goal and criteria up front

## Why

The mini-ork IDE run page is being redesigned in Orca's style; the plan is
`/Users/admin/.claude/plans/twinkling-churning-feather.md`. Today
(`mini_ork/ide_pages/run.py` `build`, ~:1246):

- **The title is the run id.** The kickoff title sits in the sub line, and the goal and success
  criteria (`run_profile.json` `user_goal`, `success_criteria`) are not shown.
- **The outcome can be wrong.** `_state_chip` (~:344) says "published · verified" from
  `verdict.json`. `verdict.json` can say `pass:true` for a run that failed, while
  `run-verdict.json` (levels), `review-*.json`, `panel-verdict.json` and `rubric.json` are never
  read.
- **The next step is hard to find.**
  - Retry lives deep in the Overview tab (`retry_section`, ~:680-765).
  - A lane failure's suggested lanes (`needs_change.suggestions`) get no "switch lane" button,
    even though `mini-ork board retry <run> --lane <alias>=<lane>` exists.
  - A pending retry gate (`retry-gate.json`) has no Approve/Reject here, though
    `board gate approve|reject <inbox_id>` exists.

The IDE now sends `MINI_ORK_IDE_SPEC=2`. `spec.ide_level()` and the new helpers (`hero`,
`triage`, `callout`, `meta_item`, …) are on main. **An IDE at level 1 (env unset) must get
exactly today's page.**

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/outcome.py` (new)
- `mini_ork/ide_pages/run.py`: `build`, plus a new `_graph(run)` helper factored out of
  `_dag_tab.build_dag` (keep `build_dag`'s output identical), plus small helpers it needs
- `tests/unit/test_ide_pages_outcome.py` (new)
- `tests/unit/test_ide_pages_run.py`: add tests only

## Changes (exact)

### A. `outcome.py`: `resolve(run) -> dict`

Read-only: never write files. Use `retry_hint.load_or_compute(home, run_id, write=False)`.

Returns:

```
{"state": "running"|"needs_you"|"failed"|"done",
 "tone": colour,
 "icon": str,
 "text": str,
 "detail": str,
 "counts": [{"t","c"}],
 "actions": [btn],
 "menu": [btn],
 "callouts": [section]}
```

**Rules, first match wins.** The DB / card `status` ALWAYS beats any verdict file: a
failed / rolled_back run is never "done" or "pass".

1. **Running** (`status` not in published / failed / rolled_back) **and no needs-you.**
   - tone yellow, icon `●`;
   - text `Running · <current step>`, where the current step is from `run.card` `step` or the
     first running node;
   - actions Stop / Kill, moved from `_actions` with their confirms.
2. **Needs you** (`card.task_state.state == "needs_you"`).
   - **Pending retry gate** (`retry_notify.pending_fix_for_run`):
     - tone orange, icon `?`, text = the gate's `needs_change.summary` or "needs a fix";
     - a `callout` titled "This run needs your decision", with `text_md` = numbered
       `retry_notify.fix_steps(hint, home=…)`;
     - callout actions:
       - **Approve retry**: `S.cli("board","gate","approve",<inbox_id>, confirm=…)`, primary;
       - **Leave it**: `S.cli("board","gate","reject",<inbox_id>, confirm=…)`, ghost.
   - **Cost pause** (`.cost-pause`): text "Paused at the cost cap"; action Resume
     (`board retry <id>`).
   - **Otherwise** (ready to review): text = `card.task_state.detail`; actions Merge into
     `<base>` / Discard (moved from `_actions`).
3. **Failed / rolled back.**
   - Tone red, icon `✗`.
   - **Text:** `card.task_state.detail` if set, else `"Failed at <node>"`, else "Failed".
   - **Detail:** the first of
     - the hint's `needs_change.summary`;
     - the reviewer's first `reasons` entry, or the first finding `issue`
       (`review-reviewer.json`, else any `review-*.json`);
     - the first failing verifier line.
   - **Actions from the hint:**
     - `strategy == "resume-cost"` → Resume.
     - `needs_change.kind == "lane"` with `suggestions` → one primary button per suggestion
       (max 3), `Switch <alias> → <lane>` =
       `S.cli("board","retry",id,"--lane",f"{alias}={lane}", confirm=…)`, with the suggestion's
       `reason` in the confirm text. Plus a default **Retry on the same lane**
       (`board retry <id>`).
     - Other `needs_change` that is retryable → **I fixed it — retry** (`--ack-change`).
     - Retryable with no `needs_change` → **Retry from <node>**.
     - Not retryable → no retry button. Detail ends with "the change itself must be revised".
     - Discard when a workspace exists.
4. **Published.**
   - Ready to review (an open worktree with commits ahead, i.e. `task_state.state ==
     "needs_you"`) is rule 2.
   - Otherwise: tone green, icon `✓`, text "Published". Add " · verified" ONLY when
     `run-verdict.json` `levels_decision` (or `verdict.json`) passes.
   - Actions: Certify (`S.page_link("verify","certify",run=id)`).

**Counts**, each only when known:
- `n/m nodes`;
- `checks p/q` from `verifier_*.json` pass;
- `<k> findings` from the review JSON;
- `$cost`;
- duration;
- level badges from `run-verdict.json` `levels`, as `applies PROVEN` (green) /
  `executes UNVERIFIED` (yellow).

**Menu, always:** Certify this change (when not already an action), Open run folder (reveal),
Open in web UI (when `_serve_url`).

### B. `run.py` `build` at `ide_level() >= 2`

Level 1 must be byte-identical to today: assert this in a test.

- **Title** = `run.card["title"]`, else the `run_profile.json` `user_goal` first line (120
  chars), else the run id.
- **Sub** = `recipe · branch <branch> · <run id>`.
- **Chips** = [the state chip from the outcome (t = state word, c = tone), money, elapsed].
- **Header actions** = [] (they move into the triage).
- **Tabs** = `story` (default), `graph`, `kickoff`, `agents`, `learnings`, `artifacts`. The old
  keys must still resolve:
  - `dag` → graph;
  - `overview` → story.
- **Story tab sections, in order:**
  1. `S.triage` from the outcome (text, tone, icon, detail, counts, actions, menu), `full`.
  2. The outcome's callouts.
  3. `S.hero("What this run is for", goal=user_goal, criteria=success_criteria[:8],
     meta=[recipe, branch (mono), run id (mono)])`. Skip it when there is neither a goal nor
     criteria.
  4. Today's Overview sections except `retry_section`, which the outcome replaced. A later run
     replaces these with the step-by-step story.
- **Graph tab** = today's `_dag_tab(run, node)`.
- **Top-level `graph` key** (EVERY level, additive) = `_graph(run)`:
  `{"cols": <the same node dicts build_dag emits>, "heads", "run_title", "recipe",
  "state": <outcome state word>}`. `_dag_tab.build_dag` calls `_graph` so they cannot drift.

## Tests

### `tests/unit/test_ide_pages_outcome.py` (temp home fixtures; reuse the patterns in `test_ide_pages_run.py`)

- **The DB status beats `verdict.json`:** status `failed` while `verdict.json` says `pass:true`
  → state failed, tone red, no "verified".
- **A lane hint** (`needs_change.kind="lane"`, `alias="codex_lens"`,
  `suggestions=[{"lane":"deepseek","reason":"19 ok"}]`) → a "Switch codex_lens → deepseek"
  action whose cli is `["board","retry",id,"--lane","codex_lens=deepseek"]`, plus "Retry on the
  same lane".
- **A pending gate** → state needs_you + a callout with Approve/Reject clis carrying the
  inbox_id.
- **Published + `run-verdict.json` levels** → level count badges; " · verified" only when it
  passes.
- **Running** → Stop/Kill actions.
- **Read-only:** no file under the run dir changes (compare mtimes or a listing before and
  after).

### `tests/unit/test_ide_pages_run.py` (add)

- `build(..., tab=None)` with `MINI_ORK_IDE_SPEC` unset equals today's output (same keys, tabs
  and first section types). With `=2`:
  - tabs start with story/graph;
  - the first section is `triage`;
  - the page has a `graph` key whose `cols` match the graph tab's `dag` section cols;
  - tab `dag` resolves to graph.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_ide_pages_outcome.py tests/unit/test_ide_pages_run.py tests/unit/test_ide_pages_spec_v2.py tests/unit/test_ide_pages_node_changes.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/ide_pages/outcome.py mini_ork/ide_pages/run.py tests/unit/test_ide_pages_outcome.py` → clean.
- **Live proof (read-only, live home `/Volumes/docker-ssd/ps/mini-ork/.mini-ork`):**
  `MINI_ORK_IDE_SPEC=2 bin/mini-ork board page run --arg run=sdd-i3-kickoff-contract-20261007181747 --home <home> | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["title"], d["tabs"][:2], [s["type"] for s in d["sections"]][:3], d["sections"][0].get("text"), len(d["graph"]["cols"]))'`.
  The title is the kickoff title, the first section is triage, and the triage text says failed.
  Paste it.
- `git diff --stat` touches only the files in scope.
