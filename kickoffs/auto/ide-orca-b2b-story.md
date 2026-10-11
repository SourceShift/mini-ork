# mini-ork IDE: render the run Story and its evidence (story, files, findings, checks, agents), Orca-style (Rust, crates/mini_ork_ui)

## Why

The plan is `/Users/admin/.claude/plans/twinkling-churning-feather.md`. The run page's default
**Story** tab (`MINI_ORK_IDE_SPEC=2`) now emits `triage`, `hero` and **`story`**. The coming
right-hand Run panel emits **`files`**, **`checks`**, **`findings`** and **`agents`**.
`triage`/`callout`/`hero` are drawn by `sections.rs` from the previous run; the five types
above still render as "needs a newer mini-ork IDE".

Orca's model for this is a native-chat transcript:
- tool rows: a state glyph + label + one-line summary, with time/cost on the right;
- per-file **diff cards** (verb icon, path, `+N −N` in git colours, a chevron that expands the
  inline diff);
- a Checks panel: summary counts, then expandable rows with a status icon, name and status
  label.

**JSON shapes:** read `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ide_pages/spec.py`
(`story`/`story_step`, `block_md`/`block_lines`/`block_files`/`block_findings`/`block_checks`,
`files`/`file_entry`, `findings`/`finding`, `checks`/`check_row`, `agents`/`agent_row`). For a
live example, run
`MINI_ORK_IDE_SPEC=2 ~/.local/bin/mini-ork board page run --arg run=sdd-i3-kickoff-contract-20261007181747 --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork`.

## What to draw

- **`story`** `{steps:[…]}`: a vertical list of step rows (CARD background, 1px BORDER, RADIUS_CARD).
  - **Row header** (32px, clickable to expand or collapse):
    - `components::status_mark(state, 13., key)` (spinner while running);
    - `p::lane_mark(lane, 13.)`;
    - the step title, 13px semibold TEXT;
    - the headline `t` in `named(c)`, 12px, truncated;
    - right-aligned: `dur` and `cost` in mono 11px SUB;
    - a chevron `›`/`⌄`.
  - Clicking the title text runs the step's `do` through the existing action path (opens the
    graph at that node). Clicking elsewhere on the header toggles expansion.
  - **Expansion state** lives in `MiniOrkPage` (a new `expanded: HashSet<String>` keyed
    `"<section>-<step id>"`). The initial state comes from the step's `open`. Never reset it on a
    page reload unless the run changes.
  - **Expanded body**, by block kind:
    - `md`: markdown via `self.texts.markdown` (12.5px BODY), max height 360 + scroll.
    - `lines`: mono lines in `named(c)` (reuse `code_box`).
    - `files`: one diff card per file: a status letter (A/M/D in GIT_ADD/GIT_MOD/GIT_DEL), the
      path (mono 12px), `+added` in GIT_ADD and `−removed` in GIT_DEL, and `›`.
      - Expanding a card shows that file's slice of the diff, coloured (`+` GIT_ADD, `-`
        GIT_DEL, `@@` CYAN).
      - REUSE the existing logic in `graph.rs` (`file_rows`, `diff_lines`, `diff_sections`,
        `section_for`): move the pure helpers into `components.rs` as `pub(crate)` and call them
        from both places. Do not copy them.
      - An "Open" ghost button when `abs` is set.
      - Commits as mono `sha subject` lines under the files.
    - `findings`: the verdict pill, then one row per item:
      - severity badge (high/critical/blocker RED, medium YELLOW, low/info SUB; BADGE size,
        rounded);
      - `file:line` as mono 11.5px, clickable to open `abs` (via `{"path": abs}`);
      - the `issue` in 12.5px TEXT;
      - the `snippet` as a 1-line mono DIM.
      - Then the reasons as `•` lines.
    - `checks`: a summary row (`✓ n passing  ✗ n failing  ● n pending`, coloured), then rows
      (status glyph, name 12px, detail SUB). A failing row expands to its `log` lines (mono).
- **`files` / `findings` / `checks`** as standalone sections: render exactly the same as the
  corresponding block, inside the normal section card with its title. Share the code: the
  section renderers call the block renderers.
- **`agents`** `{rows:[…]}`: Orca's agent rows.
  - Line 1: `status_mark` + `lane_mark` + id (12.5px semibold) + model (mono 10.5px SUB) + on the
    right, `dur` · `cost`.
  - Line 2: `step` (11.5px BODY, truncated).
  - Line 3: `last` (11px DIM mono, truncated).
  - The row is clickable via its `do`. Rows are separated by ROW_RULE.

All colours, radii and sizes come from `palette.rs` / `components.rs` (Orca tokens). Text that
people may want to copy goes through `self.texts.text` / `self.texts.markdown` (selectable).

## Files in scope (touch ONLY these, under `crates/mini_ork_ui/src/`)

- `sections.rs`: add `render_story`, `render_files`, `render_findings`, `render_checks`,
  `render_agents` + block renderers
- `page.rs`:
  - the dispatch arms for the five types (they draw inside the normal section card, except
    `story`, which draws its own rows full-width without a card header);
  - the new `expanded` field + its toggle;
  - `pub(crate)` tweaks.
- `components.rs`: receive the moved diff helpers
- `graph.rs`: ONLY switch to the moved helpers (no behaviour change)

## Build rules (IMPORTANT)

- Build ONLY with `script/mini-ork-build` (incremental release-fast, about 2–10 minutes).
- NEVER run `cargo test` / `cargo check` / `cargo build` directly, `cargo clippy`, or anything
  on the whole workspace. Never copy the repo.
- Do not run the IDE. Do not `git push`.

## Verification command

```bash
script/mini-ork-build   # must exit 0
```

## Done when

- `script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings from
  `crates/mini_ork_ui`.
- `git diff --stat` touches only the files in scope.
- A short note per renderer pointing at the code (file:line).
