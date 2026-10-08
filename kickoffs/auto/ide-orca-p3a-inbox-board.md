# IDE pages: an attention-first Inbox, a Kanban board, and a composer to steer a running run

## Why

The plan is `/Users/admin/.claude/plans/twinkling-churning-feather.md`, phase P3 (Python half).
In Orca, an Activity inbox lists what needs the human first, and a Kanban board groups work by
state. The mini-ork IDE has neither.

The plan's section types `columns` (a Kanban board) and `composer` (steer a run) also have no
`spec.py` helpers yet.

The Rust renderers for `columns` / `composer`, and the sidebar nav rows that open these pages,
are a separate IDE run. This run builds the DATA only: pages served by
`mini-ork board page <key>`.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/spec.py`: two new helpers (below)
- `mini_ork/ide_pages/inbox.py` (new)
- `mini_ork/ide_pages/kanban.py` (new)
- `mini_ork/ide_pages/__init__.py`: ONLY register `"inbox": "Inbox"` and `"kanban": "Board"`
  in `PAGES`
- `tests/unit/test_ide_pages_inbox_kanban.py` (new)

Do NOT touch any other file. In particular, not `board_cmd.py`, `fleet.py`, `run.py` or
`outcome.py`: reuse them read-only.

## Changes (exact)

1. **`spec.py` helpers**, following the existing `_section(kind, title, data, …)` pattern:
   - `columns(title, cols)`. `cols` is a list of
     `{"title": str, "c": colour-name, "count": int, "cards": [card]}`, where a card is
     `{"id", "title", "sub", "state", "mark", "meta": [meta_item], "do": action}`.
   - `composer(title, placeholder, cli)`. `cli` is the argv prefix as a list; the IDE appends
     the typed text as the last argument. Example:
     `["board", "steer", <run_id>, "--text"]`.
2. **`inbox.py` `build(home, tab, args)`**, the attention-first list:
   - **Rows.** Runs from the last 7 days whose fleet state is `needs_you` or `failed`. Read the
     fleet with `mini_ork.acp.fleet` the way `ide_pages/runs.py` does; reuse it.
   - **Exclude** runs that are done via `landed.json` (`task_state.landed_report`), and runs
     whose `repair.json` state is `repairing` (auto-repair owns them; show those in a separate
     small "Being repaired" list).
   - **Order.** needs_you first, then failed, newest first within each.
   - **One section per row**, built with `outcome.resolve(run)`:
     - a `callout` (tone from the outcome);
     - title = the run's kickoff title;
     - text = the outcome text plus detail;
     - actions = the outcome's actions;
     - an "Open run" button (`S.open_run`).
   - **Cap** at 25 rows. When there are more, end with a `list` section "N more…" linking to
     the runs page.
   - **Empty inbox** → one `callout` (tone green) "Nothing needs you".
   - **Header chips:** counts of needs_you / failed / being repaired.
   - **Fail-soft per row:** one broken run costs its row only (`S.guarded` or try/except),
     never the page.
3. **`kanban.py` `build(home, tab, args)`:**
   - One `columns` section with 4 columns: **Working** (running/working), **Needs you**,
     **Failed**, **Done**. Done covers the last 24 h only, and includes runs done via
     `landed.json`.
   - Each column holds at most 30 cards, newest first.
   - A card has:
     - title = the kickoff title;
     - sub = `recipe · <step or detail>`;
     - meta = `$cost` and `+added −removed` when non-zero;
     - `do` = `S.open_run(run_id)`.
4. **`__init__.py`:** register both pages. Do not change other entries.

## Tests (`tests/unit/test_ide_pages_inbox_kanban.py`; temp home + state.db fixtures like `tests/unit/test_ide_pages_run.py`)

- **Inbox ordering and exclusions.** Seed:
  - a needs_you run;
  - a failed run;
  - a failed run with `landed.json`;
  - a failed run whose `repair.json` is `repairing`.

  → needs_you first, then failed. The landed run is absent, and the repairing run is only in
  "Being repaired".
- **Empty inbox** → the "Nothing needs you" callout.
- **Kanban** puts each seeded state in its column, and a landed run under Done. The section
  type is `columns`, and each card's `do` opens the run.
- **`spec.composer` / `spec.columns`** produce the documented keys.
- **`build_page(home, "inbox")` and `build_page(home, "kanban")`** return `ok`.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_ide_pages_inbox_kanban.py tests/unit/test_ide_pages_run.py tests/unit/test_ide_pages_outcome.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check` on the touched files → clean.
- **Live proof (read-only).** Paste the section types and the first 3 row titles of:
  - `MINI_ORK_IDE_SPEC=2 bin/mini-ork board page inbox --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork`;
  - `MINI_ORK_IDE_SPEC=2 bin/mini-ork board page kanban --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork`.
- `git diff --stat` touches only the files in scope.
