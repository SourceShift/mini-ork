# mini-ork IDE: tables, key-value lists and lists drawn in Orca's style

## Why

The plan is `/Users/admin/.claude/plans/twinkling-churning-feather.md`, P5 (Rust half). The run
page, Inbox, Board, Lanes, Verify, Automations and Recipes now lead with Orca-style sections.
Every page still ends in the older `table` / `kv` / `list` sections, as "All · …" tables and
detail lists. Those are drawn by `page.rs`:

- `render_table` ~:732
- `render_kv` ~:816
- `render_list` ~:847

They use the pre-Orca look: dense grid lines, no card, inconsistent type sizes. They sit right
under the new cards and look out of place.

The shared building blocks exist in `components.rs`:
- `card()` (~:44): CARD fill, 10px radius, BORDER;
- `section_head(title, count)` (~:90);
- `state_pill(state)` (~:55).

Orca's tokens are in `palette.rs`.

## Files in scope (touch ONLY these, under `crates/mini_ork_ui/src/`)

- `page.rs`: ONLY `render_table`, `render_kv` and `render_list` (and small private helpers
  they need)

Do NOT touch any other file. Not `components.rs`: use it as-is. Not `README.md`: this run opens
no PR, so the README review-banner rule does not apply.

## Restyle (exact)

Keep every DATA behaviour the same: the columns, cell colours (`c`), mono cells, bold (`b`),
row actions (`do`), the list markers (`m`/`mc`), and the `acts` buttons. Change only the
presentation.

1. **All three:**
   - Wrap the section in `components::card()` with 12px inner padding.
   - Head the card with `components::section_head(title, Some(row_count))` (no count for
     `kv`) in place of the current title row.
   - The `note`, when present, goes under the head (11px SUB).
2. **`table`:**
   - No vertical grid lines.
   - The header row in 10.5px SUB uppercase with letter-spacing.
   - Rows 30px tall with a 1px BORDER divider between rows only.
   - Hover on a clickable row (`do`) uses the HOVER token.
   - The text sizes are 12.5px, mono cells 11.5px.
   - Keep the existing column widths (`cols`).
3. **`kv`:** a two-column grid, keys in 11.5px SUB (right-aligned), values in 12.5px TEXT,
   mono when flagged; 6px row gap.
4. **`list`:**
   - Each item is a row: the marker glyph in its colour, then the title (12.5px), then the sub
     (11px SUB) under it.
   - The `acts` buttons sit on the right, as ghost buttons in the palette's ghost kind.
   - 8px between items.

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

- `script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings from `page.rs`.
- `git diff --stat` touches only `crates/mini_ork_ui/src/page.rs`.
- A short note per renderer (file:line).
