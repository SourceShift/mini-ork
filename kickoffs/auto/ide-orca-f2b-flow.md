# mini-ork IDE: the run flow map, part B (Rust) — Review hub + spokes, optional Merge + Human, legend, stage chips

## Why

Part A (`crates/mini_ork_ui/src/flow.rs`, on the `mini-ork` branch) draws Ticket → Design → Build
→ Integrate from the run page's `flow` object. This part completes the user's reference design:

- **Return track:** after Integrate, the track turns down on the right and runs back LEFT along
  a lower line (rounded corner, radius 40). The Review hub sits on that lower line, and the Merge
  bar at its left end. This is the screenshot's layout.
- **Review hub:**
  - A 120px circle (3px TEXT stroke, CARD fill), "Review" in 16px semibold, centred.
  - The verdict (11px, state colour) and the score below it ("7/8").
- **Spokes:**
  - `review.spokes` are radial lines from the hub, evenly spaced over the upper and lower arcs
    (skip the left/right sectors where the track passes). Each line is 2px BORDER2, length about
    150px.
  - Each spoke ends in an 18px ring in its state colour. The label is 13px semibold beside it,
    with the sub (11px SUB) under it.
- **Fix spoke:** `review.fix` with rounds > 0 is drawn as an orange double line to a ring labelled
  "Fix" (orange), sub "then review again · N round(s)". It turns green-outlined when its state is
  approved.
- **Merge** (ONLY when `flow.merge` is not null):
  - A vertical bar (6×56) at the left end of the lower track, labelled "Merge".
  - `merge.checks` as checkboxes (14px, ✓ in GREEN when ok) stacked above it.
  - When `merge.human` exists: a dotted vertical line down to a 52px circle with a person glyph,
    labelled "Human" plus its sub ("makes the final merge").
  - **When merge is null**, the lower track simply ends at the Review hub. When `flow.rollback`
    exists, draw a RED end marker "Rolled back · N file(s)" there instead.
- **Legend** at the top right, from `flow.legend`: 22×3 bars in BLUE / ORANGE / GREEN plus mono
  11px labels.
- **Stage chips** along the bottom, from `flow.stages`: a pill with a numbered 20px circle (GREEN
  fill when approved, BLUE ring when in_progress, ORANGE when sent_back/failed, BORDER2 when
  pending) and the label. "Click a stage to jump to it" (mono 11px SUB) after them. Clicking a
  chip scrolls the flow's horizontal ScrollHandle so that stage's x is in view, and highlights
  its stage for 1.5 s.
- **Remove the "Detailed DAG"** section below the map: the flow map is the graph view. The
  full-screen DAG still opens from its button.

## Files in scope (touch ONLY these, under `crates/mini_ork_ui/src/`)

- `flow.rs`: extend it (review, merge, legend, chips, scroll handle)
- `page.rs`: ONLY the graph-tab dispatch (drop the detailed DAG when `flow` is present)

Do NOT touch `graph.rs`, `sidebar.rs` or `README.md` (this run does not open a PR, so the
README review banner rule does not apply).

Part A is commit 137fecd: read `flow.rs` first and extend its layout struct and paint code. Do
not fork a second layout. `page.rs` also has an older `render_flow` for a legacy `flow` SECTION
type; leave it alone.

## Build rules (IMPORTANT)

- Build ONLY with `script/mini-ork-build`.
- NEVER run `cargo test` / `cargo check` / `cargo build` / `cargo clippy` directly or on the
  workspace, and never copy the repo.
- Do not run the IDE. Do not `git push`.

## Verification command

```bash
script/mini-ork-build   # must exit 0
```

## Done when

- `script/mini-ork-build` exits 0. Paste the `Finished` line and any `flow.rs` warnings.
- `git diff --stat` touches only the files in scope.
