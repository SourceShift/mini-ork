# mini-ork IDE: draw the Kanban `columns` and the steering `composer`, and open Inbox / Board from the sidebar

## Why

mini-ork main (`cd8774a8`) now serves two new pages and two new section types:

- **`mini-ork board page inbox`:** attention-first `callout` rows plus `list` sections. The IDE
  already draws both.
- **`mini-ork board page kanban`:** one section of the NEW type `columns`:

  ```
  {"type":"columns","title":…,"cols":[{"title","c":<colour name>,"count":int,"cards":[{"id","title","sub","state","mark","meta":[{"t","c","mono"}],"do":<action>}]}]}
  ```
- **`composer`** (NEW type): `{"type":"composer","title":…,"placeholder":str,"cli":[argv…]}`.
  The typed text is appended as the LAST argument, and the result runs through the same action
  path as a `{"cli": [...]}` button.

The exact shapes are in `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ide_pages/spec.py` (`columns`,
`composer`). Live data:

```
MINI_ORK_IDE_SPEC=2 /Volumes/docker-ssd/ps/mini-ork/bin/mini-ork board page kanban --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork
```

The IDE draws neither type yet; unknown types show the "update the IDE" fallback. The
sidebar's activity bar has no entry for Inbox or Board.

## Files in scope (touch ONLY these, under `crates/mini_ork_ui/src/`)

- `sections.rs`: `render_columns` and `render_composer`
- `page.rs`: ONLY the two new `"columns"` / `"composer"` arms in `render_section`'s match, plus
  the composer's input state:
  - one editor per composer section, created like `graph.rs`'s steer editor (~:239-312,
    `ErasedEditor` via the same factory);
  - submit on Enter → `store::perform` with `{"cli": cli + [text]}`;
  - clear the editor after sending.
- `sidebar.rs`: ONLY the `ACTIVITIES` list plus the badge condition:
  - add `("inbox", "✉", "Inbox — what needs you")` right after `threads`, and
    `("kanban", "▦", "Board")` after it;
  - show the existing needs_you badge on `inbox` (today it is on `threads`; keep it there too).
  - `ACTIVITIES` is a fixed-size array; update its length.

Do NOT touch `graph.rs`, `flow.rs`, `run_panel.rs`, `components.rs` or `README.md`. This run
opens no PR, so the README review-banner rule does not apply.

## Rendering (exact)

- **`columns`:**
  - A horizontal row of columns, each 260px wide with 12px gaps, scrolling horizontally when
    wider than the page.
  - **Column header:** a colour dot (from `c`), the title (13px semibold) and the count (11px
    SUB).
  - **Cards** use the existing card look from `components.rs` (CARD fill, 10px radius, BORDER):
    - top row: the status `mark` and the title (12.5px, two lines max);
    - then `sub` (11px SUB, one line);
    - then the meta items (mono 10.5px).
  - A whole card is clickable: run its `do` through the existing action path.
  - The column body scrolls vertically.
- **`composer`:** the title (if any) above a single-line editor with the placeholder, a `Send`
  button on the right (primary), and Enter to send. Disabled (dimmed) while the text is empty.

## Build rules (IMPORTANT)

- Build ONLY with `script/mini-ork-build`. It is slow on this host, so build ONCE at the end,
  and once more only if it fails.
- NEVER run `cargo test` / `cargo check` / `cargo build` / `cargo clippy` directly, and never
  copy the repo.
- Do not run the IDE. Do not `git push`.

## Verification command

```bash
script/mini-ork-build   # must exit 0
```

## Done when

- `script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings from the touched
  files.
- `git diff --stat` touches only the files in scope.
- A short note pointing at both renderers, the composer submit path, and the sidebar entries
  (file:line).
