# mini-ork IDE: ⌘J jump palette, and per-lane health in the status bar

## Why

The plan is `/Users/admin/.claude/plans/twinkling-churning-feather.md`, P4.

- **Jump palette.** Orca has a jump palette (⌘J) to reach any worktree, page or action
  instantly. The mini-ork IDE (Zed fork, `crates/mini_ork_ui`) has no fast way to jump to a run
  or page.
- **Status bar.** The status bar (`status.rs`) shows totals, but not which LANE is failing.
  Today codex silently rejected its model for hours, and runs fell back to a paid lane without
  anyone seeing it.

mini-ork (on main once run `ide-orca-p4a-lanes-composer` merges) adds a `lanes` list to
`board --json`'s `header`:

```
"lanes": [{"lane": str, "calls": int, "failed": int, "usd": float, "last_error": str}]   // last 24 h, ≤ 8, calls desc; may be absent on an older engine
```

`header` already has `needs_you`, `today_usd` and `cap_usd`.

## Files in scope (touch ONLY these, under `crates/mini_ork_ui/src/`)

- `jump.rs` (new): the palette
- `mini_ork_ui.rs`: `mod jump;` and its `init` call
- `board.rs`: ONLY add `#[serde(default)] pub lanes: Vec<Lane>` to `Header`, plus the `Lane`
  struct (all fields `#[serde(default)]`, so an older engine without `lanes` still parses)
- `status.rs`: the lane mini-bars and the needs-you chip

Do NOT touch `page.rs`, `graph.rs`, `flow.rs`, `sidebar.rs`, `sections.rs`, `run_panel.rs`,
`components.rs` or `README.md`. This run opens no PR, so the README review-banner rule does
not apply.

## Behaviour (exact)

1. **Jump palette** (`jump.rs`):
   - **Implementation.** A `picker::Picker` with a `PickerDelegate`, opened via
     `workspace.toggle_modal`. Copy the delegate plumbing from
     `crates/agent_ui/src/model_selector.rs` (`impl PickerDelegate for ModelPickerDelegate`).
   - **Items:**
     - the board's runs from the `BoardStore` (title, state mark, recipe; newest first), which
       open the run page;
     - the mini-ork pages: Inbox, Board (`kanban`), Runs, Verify, Recipes, Automations, Lanes,
       Learning, Context, Nodes, Setup, which open that page.
   - **Filtering.** Fuzzy match on the typed text, using the `fuzzy` crate the way other
     pickers do.
   - **Footer hint:** `↵ open · esc close · ↑↓ move`, in mono 11px SUB.
   - **⌘J.** Install it with `cx.intercept_keystrokes`, NOT `bind_keys`: `reload_keymaps`
     clears bindings. Match cmd-j and toggle the palette on the active workspace.
     - If `intercept_keystrokes` is not available with that name in this gpui version, read
       how `title_bar.rs` / `store.rs` hook global input.
     - Otherwise register an action plus a keymap entry in the crate's own default keymap
       JSON, if one exists (search the crate first), and say which you used.
2. **Status bar** (`status.rs`):
   - **Lanes.** When `header.lanes` is non-empty, show at most 5 lanes as compact items: the
     lane name (mono 10.5px), then a tiny 28×4 bar.
     - The bar is green for the success share (`calls - failed`) and red for the failure share.
     - The tooltip shows `calls`, `failed`, `$usd` and `last_error`.
     - A lane with `failed / calls ≥ 0.5` and `calls ≥ 3` gets a RED name.
   - **Needs-you chip.** When `needs_you > 0`, show an orange "✋ N need you" chip that opens
     the Inbox page (`page::open_page(&workspace, "inbox", …)`, the same way the sidebar opens
     pages).

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
- A short note: how ⌘J is bound, and where the delegate, the items and the status items are
  (file:line).
