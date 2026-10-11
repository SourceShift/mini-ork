# mini-ork IDE: the run flow map, part A (Rust, crates/mini_ork_ui) — canvas, track, Ticket, Design + revise arcs, Build packages, Integrate pill

## Why

The user wants each run's graph drawn as a flow map, replacing the column DAG. The plan is
`/Users/admin/.claude/plans/twinkling-churning-feather.md` ("Run flow map"). The run page JSON
(`mini-ork board page run --arg run=<id>` with `MINI_ORK_IDE_SPEC=2`) now carries a top-level
`flow` object: `ticket`, `stages`, `design`, `build`, `integrate`, `review`, `merge`, `rollback`,
`timeline`, `legend`. The exact shape is in `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ide_pages/run_flow.py`
(read its module docstring and `build_flow`). For a live example, run
`MINI_ORK_IDE_SPEC=2 ~/.local/bin/mini-ork board page run --arg run=ide-orca-b2b-story-20261008113340 --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork | python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin)["flow"],indent=1))'`.

**Reference design** (the user's screenshot), read left to right on one horizontal track:

- **Ticket in:** a thick vertical bar (6×56px, TEXT colour) with "Ticket in" (13px semibold)
  and the run id (11px SUB) below it.
- **Design:** step markers on the track. Each marker is a 22px ring (3px stroke in the step's
  state colour, filled CARD) with a 9px inner dot in the same colour. The label is 13px
  semibold below the marker, with the sub (11px SUB) under it. A score (e.g. "7/8") sits above
  the marker in mono 12px GREEN.
- **Revise loop** (`design.revise` / `build.revise`): an orange cubic arc above the track from
  `from` to `to`, a small "revise" label at its apex, and a rounded orange-outlined pill
  "approved in round N" above it when `approved_round` is set.
- **Build:**
  - The track fans out with cubic beziers into one rounded package box per package (fill =
    HOVER at about 40% alpha, radius 12, "PACKAGE n" mono 10px SUB at the top right, only when
    there are ≥2 packages).
  - Each lane is a horizontal line inside its box: a role label above, an Implementer marker,
    then the critic marker when present.
  - Lanes fan back in with cubic beziers to the Integrate pill.
- **Integrate:**
  - A pill (100×40, radius 20, 3px TEXT stroke) on the track, with "Integrate" (14px semibold)
    and "runs every check tier" (11px SUB) above it.
  - Three dots inside while `in_progress`; a ✓ in GREEN when approved; ✗ in RED when failed.
  - Below it, one row per tier: the label (12px BODY) and a 80×4 bar, filled
    `passed/total` in GREEN, or RED when the tier failed.

**Edge colour** = the state of the step the edge leads to:

| state | colour |
|---|---|
| approved | GREEN |
| in_progress | BLUE |
| sent_back | ORANGE |
| failed | RED |
| pending | BORDER2 |
| skipped | BORDER2 (marker drawn hollow, label in SUB) |
| done (Ticket only) | TEXT |

Edges are 3px. Pending edges are 2px.

**Clicking a marker** runs that step's `do` through the existing page action path, opening the
node.

## Files in scope (touch ONLY these, under `crates/mini_ork_ui/src/`)

- `flow.rs` (new): `pub(crate) fn render_flow_part_a(...)`. It computes a layout struct
  (x/y per step) and paints with `gpui::canvas` + `PathBuilder` (stroke + `cubic_bezier_to`),
  the way `graph.rs:742-780` and `thumb.rs:354-380` already do. Markers and labels are positioned
  absolute divs over the canvas.
- `page.rs`:
  - In the `graph` tab, when `page["flow"]` is an object, render the flow map at the top of the
    tab (full width, horizontally scrollable).
  - Keep the existing `dag` section below it as "Detailed DAG" for now. Part B removes it.
  - Minimal dispatch only.
  - Name clash: `page.rs` already has `render_flow` for an older `flow` SECTION type (a chain of
    items, `"flow" =>` in the section match). Leave it alone. The new map is the top-level
    `page["flow"]` object, not a section.
- `mini_ork_ui.rs`: `mod flow;`

Do NOT touch `graph.rs` (another session is editing it) or `sidebar.rs`.

## Build rules (IMPORTANT)

- Build ONLY with `script/mini-ork-build`.
- NEVER run `cargo test` / `cargo check` / `cargo build` / `cargo clippy` directly or on the
  whole workspace, and never copy the repo.
- Do not run the IDE. Do not `git push`.

## Verification command

```bash
script/mini-ork-build   # must exit 0
```

## Done when

- `script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings from `flow.rs`.
- `git diff --stat` touches only the files in scope.
- A short note pointing at the layout function and each drawn element (file:line).
