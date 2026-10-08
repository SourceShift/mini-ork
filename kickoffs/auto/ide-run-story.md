# Run page v2: the step-by-step story (Orca-style transcript of what each pipeline node did)

## Why

The plan is `/Users/admin/.claude/plans/twinkling-churning-feather.md`. The run page's Story tab
(`mini_ork/ide_pages/run.py` `build` at `ide_level() >= 2`, on main) shows, in order:
1. the outcome triage;
2. callouts;
3. the goal hero;
4. today's Overview sections as a stopgap.

This run replaces item 4 with the **story**: one readable row per pipeline node, in DAG order.
Each row says what the node did, and expands to show the evidence: diff cards per file, check
results, review findings. It follows Orca's agent transcript (tool rows, per-file diff cards
with +N −N), using `spec.story` / `spec.story_step` and the block helpers on main.

All the data and headline logic already exists in node-level helpers. **Reuse them; do not
re-derive anything:**

| helper | location | what it gives |
|---|---|---|
| `_overview_headline(run, node, run_dir, changes=None)` | `node.py:2679` | one-sentence headline per node kind (planner objective, lens heading, implementer "changed n files (+a −r)", verifier check counts, reviewer "verdict — first reason", failure reason) |
| `_overview_facts(...)` | `node.py:2954` | facts |
| `_node_tokens(node, run)` | `node.py:523` | tokens |
| `_result_items(run, node)` | `node_changes.py:129` | per-kind items: planner, review findings (findings first since 155a054c), verifier checks, rollback |
| `_load_diffs(run)` | `node_changes.py:796` | files with +/− |
| `_files_and_note(run, show_diff, entries, source)` | `node_changes.py:738` | files with abs paths, diff note |
| `_commits(run)` | `node_changes.py:1045` | commits |
| `run.nodes` | | each node has `state`, `family` (lane), `role_lane`, `cost`, `calls`, `start`/`end`; `_wall(node)` gives its duration |
| `run.cols` | | DAG layers |

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/run_story.py` (new)
- `mini_ork/ide_pages/run.py`: ONLY the Story tab assembly in `build`, which swaps the stopgap
  Overview sections for `run_story.story_section(run)` while keeping the Overview's `files`
  section out (the story shows files). Import `run_story` INSIDE the function: `node.py`
  imports `run.py`, so a top-level import would be circular.
- `tests/unit/test_ide_pages_run_story.py` (new)

## Changes (exact)

### `run_story.story_section(run) -> dict`

Returns `S.story("What happened", steps, full=True)`, with one `S.story_step` per node in
`run.cols` order (flattened), skipping nodes never started AND not in the workflow.

**Each step:**

- `id` = node id; `title` = node id; `kind` = node.type.
- `state`: `done` / `running` / `failed` / `skipped` / `pending`, mapping the node's state
  (a `finish_reason` of skipped → skipped).
- `lane` = node.family; `model` = the provider model when cheap to get, else "".
- `headline` = `_overview_headline(...)` → `(t, c)`.
- `dur` = `_wall(node)`; `cost` = `S.money(node.cost)` if > 0.
- `meta` = [`<calls> calls`, `<tokens> tokens`] when known.
- `do` = `S.page_link("run", "graph", run=run.id, node=node.id)`.

**Body blocks by kind:**

- **implementer / code-changing:** `S.block_files(...)` from `_load_diffs` + `_files_and_note`,
  computed ONCE per run and shown under the FIRST implementer step only. Each file is an
  `S.file_entry` with `abs` when it exists. Include the unified diff text for the files when
  cheap (cap 400 lines total). Add commits.
- **reviewer / judge / lens:** `S.block_findings(items, verdict, reasons)`, built from the review
  JSON:
  - each finding becomes `S.finding(issue, severity, file, line, abs, snippet, source=node id)`;
  - reasons are the string `reasons` / `notes`;
  - for lenses without JSON, `S.block_md` of the report's first ~30 lines (`lens-*.md`).
- **verifier / test / typecheck / static_check:** `S.block_checks(rows)`, one `S.check_row` per
  check from the verifier JSON or `*.checks.tsv` (via `_verifier_items` data or the same files).
  A failing row includes up to 8 log lines.
- **planner:** `S.block_md` of the objective + up to 6 plan steps (`plan.json`).
- **publisher / rollback / other:** `S.block_lines` from `_result_items`.

**`open`** (expanded by default) = True for:
- failed / running steps;
- the reviewer when it has findings;
- the first implementer.

All others False.

### Rules

- **Read-only.** Never write under the run dir (the IDE's fs-watch reloads on writes).
- **Fail-soft per step:** an exception in one step's body becomes
  `S.block_lines([("could not read: <error>", "red")])`, and the step still renders.
- **Performance:** build in under 1.5 s on a 9-node run; compute `_load_diffs` once.

## Tests (`tests/unit/test_ide_pages_run_story.py`, temp run fixtures as in `test_ide_pages_run.py`)

- Steps follow `run.cols` order; states map correctly; a skipped node → `skipped`.
- A reviewer with `review-reviewer.json` findings → a findings block whose items carry
  issue/file/line/severity; the step is `open`.
- An implementer with a `framework-edit.diff` (or acp-diffs) → a files block with per-file
  +/−, only on the first implementer.
- A verifier with a `verifier_<id>.json` holding one failing check → a checks block with
  `summary.failing == 1`.
- An exception inside one step's body yields an error line, not a crash.
- At `MINI_ORK_IDE_SPEC=2`, `run.build` Story tab sections are: triage, (callouts), (hero),
  story; there is no Overview `files` section.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_ide_pages_run_story.py tests/unit/test_ide_pages_run.py tests/unit/test_ide_pages_outcome.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/ide_pages/run_story.py mini_ork/ide_pages/run.py tests/unit/test_ide_pages_run_story.py` → clean.
- **Live proof (read-only):**
  `MINI_ORK_IDE_SPEC=2 bin/mini-ork board page run --arg run=sdd-i3-kickoff-contract-20261007181747 --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork`.
  Print each story step's `id`, `state`, `headline.t` and body block kinds. The reviewer step has
  a findings block; the implementer has a files block. Paste it with its wall time.
- `git diff --stat` touches only the files in scope.
