# mini-ork IDE: Orca colours, shared components, spec-v2 plumbing (Rust, crates/mini_ork_ui)

## Why

The mini-ork IDE (a Zed fork; this repo) is being redesigned to look and behave like Orca. The
plan is `/Users/admin/.claude/plans/twinkling-churning-feather.md`. The user chose **Orca's
neutral dark theme**.

Today `crates/mini_ork_ui/src/palette.rs` `sync_theme` maps every design colour onto the active
Zed theme (the user runs Ayu Dark), so nothing looks like Orca. This run lays the Rust
foundation; later runs add the new page sections.

## Files in scope (touch ONLY these, all under `crates/mini_ork_ui/src/`)

- `palette.rs`
- `components.rs` (new) + one `mod components;` line in `mini_ork_ui.rs`
- `page.rs`: ONLY the unknown-section fallback in `render_section` (~:538)
- `board.rs`: ONLY `run_json` and `run_action` (one `.env(...)` each)
- `graph.rs`: ONLY `Model::from_page` (~:77)
- `sidebar.rs`: ONLY change `fn status_mark` (~:988) and `fn node_mark` (~:1006) to `pub(crate) fn`

Do NOT touch any other file, any Zed crate outside `crates/mini_ork_ui`, `Cargo.toml`, or
`Cargo.lock`.

## Changes (exact)

### 1. `palette.rs`: Orca dark by default

- `sync_theme`: unless env `MINI_ORK_PALETTE` == `"theme"` (today's behaviour, kept as an opt-out),
  fill the `THEMED` map from these Orca dark values instead of the theme colours. The key
  constants keep their names and values, so every call site still works; only the map changes.

  | key | Orca value | key | Orca value |
  |---|---|---|---|
  | BG | 0x0a0a0a | CHROME | 0x171717 |
  | BORDER | 0x232323 | BORDER2 | 0x333333 |
  | HOVER | 0x262626 | SELECTED | 0x353535 |
  | ROW_SELECTED | 0x262626 | ROW_RULE | 0x1c1c1c |
  | NODE_BG | 0x1f1f1f | TEXT | 0xfafafa |
  | BODY | 0xd4d4d4 | MUTED | 0xa1a1a1 |
  | SUB | 0x8b8b8b | DIM | 0x5e5e5e |
  | BLUE | 0x60a5fa | GREEN | 0x34d399 |
  | RED | 0xff6568 | YELLOW | 0xf0b100 |
  | PURPLE | 0xa78bfa | CYAN | 0x22d3ee |
  | ORANGE | 0xff6900 | STREAM_BOX | 0x111111 |
  | ELEMENT | 0x262626 | CANVAS | 0x0a0a0a |
  | SKELETON | 0x262626 | NODE_CARD | 0x171717 |
  | NODE_CARD_PENDING | 0x121212 | | |

- **New public constants:** `CARD` 0x171717, `PAGE` 0x0a0a0a, `SIDEBAR` 0x1c1c1c,
  `SIDEBAR_ACTIVE` 0x2a2a2a, `AMBER` 0xf59e0b (unread), `GIT_ADD` 0x81b88b, `GIT_MOD` 0xe2c08d,
  `GIT_DEL` 0xc74e39. Their literal hex is the value; add them to the map in theme mode too, with
  sensible theme colours: `version_control_added`/`modified`/`deleted` when the theme has them,
  else the literal.
- `named()` adds `"amber"`.
- **Type scale constants:** `TITLE` 13., `META` 11., `BADGE` 10., `PILL` 9. **Radius:** `RADIUS_CARD` 10.,
  `RADIUS_BTN` 6.
- **`state_mark` uses Orca's state language:**

  | state | mark | colour |
  |---|---|---|
  | working / running | `●` | YELLOW |
  | needs_you / needs | `?` | ORANGE |
  | failed / error | `✗` | RED |
  | done / published | `✓` | GREEN |
  | anything else | `○` | SUB |

- `mix(a: Hsla, b: Hsla, t: f32) -> Hsla`: linear blend, for Orca's "foreground 10% over
  background" active surfaces.

### 2. `components.rs` (new): small shared builders, Orca-styled, no state

- `card() -> Div`: CARD background, 1px BORDER, `RADIUS_CARD`, padding 12.
- `state_pill(state: &str) -> AnyElement`: rounded-full; the `state_mark` colour as text on that
  colour at ~15% alpha; label = the state word in uppercase (PILL size, letter-spaced).
- `status_mark(state: &str, size: f32) -> AnyElement`: Orca glyphs.
  - Working: a ring spinner. Delegate to `crate::sidebar::status_mark(state, key)`, made
    `pub(crate)`; do not copy or move it. Add a `key: &str` parameter for the animation id.
  - Others: `state_mark`.
- `section_head(title: &str, count: Option<usize>) -> Div`: 11px semibold MUTED title + a count
  pill.
- `split_button(id, primary: (label, kind), on_primary, more: Vec<(label, handler)>) -> AnyElement`:
  primary button plus a `▾` that opens a small menu. If a menu is hard in gpui here, render the
  extra items as ghost buttons next to it, and leave a TODO.

### 3. `page.rs`: unknown section types are visible

In `render_section`, the `_ =>` arm (~:538) renders the section card with its `title`/`note` and
one MUTED line: `This section (<type>) needs a newer mini-ork IDE.` It must not render an empty
div.

### 4. `board.rs`: announce spec v2

`run_json` and `run_action` add `.env("MINI_ORK_IDE_SPEC", "2")` to the command.

### 5. `graph.rs`: the graph no longer depends on the header

`Model::from_page` first looks for a top-level `page["graph"]` object:

```
{cols: [[{id,label,lane,state,role,cost,dur}]], heads: [str], run_title, recipe, state}
```

When present, read cols, heads, recipe, title (`run_title`) and state from it. Otherwise keep
exactly today's logic (the `dag` section, `sub` split on " · ", `chips[0]`). Factor the
node-parsing loop so both paths share it.

## Build rules (IMPORTANT, read before any command)

- Build ONLY with `script/mini-ork-build`. It is incremental, release-fast, about 2–10 minutes.
- NEVER run `cargo test`, `cargo check`, `cargo build` without the script, `cargo clippy`, or
  anything on the whole workspace. Those cold-compile 1000+ crates for 30+ minutes, and the host
  kills runaway processes.
- Do not run the IDE. Do not `git push`.

## Verification command

The command that proves this run succeeded:

```bash
script/mini-ork-build   # must exit 0
```

## Done when

- `script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings that come from
  `crates/mini_ork_ui`.
- `git diff --stat` touches only the files in scope.
- A short note per change pointing at the code (file:line).
