# mini-ork IDE: render the run outcome sections (triage, callout, hero) in Orca's style (Rust, crates/mini_ork_ui)

## Why

The plan is `/Users/admin/.claude/plans/twinkling-churning-feather.md`. With the IDE sending
`MINI_ORK_IDE_SPEC=2`, the run page now emits three new section types. Today the IDE renders
them only as "This section (<type>) needs a newer mini-ork IDE." This run draws them like
Orca's Checks panel triage strip and approval card.

The JSON shapes come from `mini_ork/ide_pages/spec.py` in the mini-ork repo (read it at
`/Volumes/docker-ssd/ps/mini-ork/mini_ork/ide_pages/spec.py`: `hero`, `triage`, `callout`,
`meta_item`). Every section also carries `title`, `note`, `actions` and the layout keys `full`
/ `col`.

- **`triage`** `{text, tone, icon, detail, counts:[{t,c}], menu:[btn]}` + section `actions:[btn]`
  - Orca: a one-line verdict ("2 checks failing [Fix]").
  - Draw a full-width strip: CARD background, a 3px left border in `named(tone)`, padding 12/14.
  - Row 1: the icon (in `named(tone)`, 14px) + `text` (13px semibold TEXT) + on the right, the
    `counts` as small pills (BADGE size, `named(c)` text on that colour at ~12% alpha, rounded).
  - Row 2 (when set): `detail` (11.5px MUTED, selectable via `self.texts.text`, wraps).
  - Row 3: the `actions` as buttons. The first is drawn as-is (usually `primary`), then the
    rest, then the `menu` items as ghost buttons after a thin divider. Use the existing
    `self.button(...)` so clicks route through `action_handler` / `store::perform` (confirm
    prompts included).
- **`callout`** `{title, text_md, tone}` + `actions` (Orca's ApprovalCard)
  - A bordered card: 1px border in `named(tone)` at ~45% alpha, background tone at ~6% alpha,
    RADIUS_CARD.
  - Title row: a `?` glyph (needs-you) in `named(tone)` + the title, 13px semibold.
  - Body: `text_md` as markdown through `self.texts.markdown(...)`, 12.5px BODY.
  - The actions right-aligned at the bottom.
- **`hero`** `{goal, criteria:[str], pill:{t,c}|null, meta:[{t,c,mono}]}` + title
  - Title: 11px MUTED uppercase (it is a label like "What this run is for").
  - Then `goal`: 14px TEXT, selectable markdown.
  - `criteria` as a checklist: `○` + text, 12.5px BODY, one per row.
  - `meta` items as inline mono/sans chips, 11px `named(c)`, separated by `·`.
  - The pill, when set, uses `components::state_pill`.

Use the Orca tokens and components from `palette.rs` / `components.rs` (from the previous run:
CARD, BORDER, `state_pill`, RADIUS_CARD, the type scale). These three sections render
**without** the generic section-card title header: they draw their own chrome, the same way
`dag` and `inspector` return early in `render_section`.

## Files in scope (touch ONLY these, under `crates/mini_ork_ui/src/`)

- `sections.rs` (new): `render_triage`, `render_callout`, `render_hero` as methods in an
  `impl MiniOrkPage` block (or free fns taking `&MiniOrkPage`), whichever matches how
  `page.rs` renderers access `self.texts` / `self.button`. Make the needed `page.rs` items
  `pub(crate)`.
- `page.rs`: ONLY the early-return dispatch in `render_section` for `triage` / `callout` /
  `hero`, plus `pub(crate)` visibility tweaks the new module needs.
- `mini_ork_ui.rs`: one `mod sections;` line.

## Build rules (IMPORTANT)

- Build ONLY with `script/mini-ork-build` (incremental, release-fast, about 2–10 minutes).
- NEVER run `cargo test` / `cargo check` / `cargo build` directly, `cargo clippy`, or anything
  on the whole workspace: that is a 30+ minute cold compile, and the host kills runaway
  processes.
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
