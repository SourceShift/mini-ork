# mini-ork IDE: the right-hand Run panel (Orca's right sidebar): Changes · Checks · Agents · Cost · Learned

## Why

In Orca, the right sidebar shows the selected worktree's Source Control (files +/−) and
Checks next to the session. The mini-ork IDE (Zed fork, `crates/mini_ork_ui`) has the data for
this already: the Python page builder serves a single-column dock view of the run page.

```
MINI_ORK_IDE_SPEC=2 /Volumes/docker-ssd/ps/mini-ork/bin/mini-ork board page run --arg run=<id> --arg view=dock --tab <changes|checks|agents|cost|learned> --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork
```

The output has the same page JSON shape as every other page: `title`, `tabs`, `tab`,
`sections`. Its sections are types the IDE already renders: `triage`, `files`, `checks`,
`findings`, `agents`, `kv`, `table`, `list`. Try it on run `ide-orca-f3-replay-20261008195059`.
The tabs come from `DOCK_TABS` in `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ide_pages/run_dock.py`.

Nothing in the IDE shows this view yet.

## Files in scope (touch ONLY these, under `crates/mini_ork_ui/src/`)

- `run_panel.rs` (new): the panel
- `mini_ork_ui.rs`: `mod run_panel;` and ONE registration call in `init`
- `page.rs`: ONLY what the panel needs to host a `MiniOrkPage` in dock mode:
  - a constructor that takes initial args (`run`, `view=dock`, `tab`);
  - a `dock: bool` flag that hides the page header and the outer padding;
  - a setter to change the run.

  Keep every existing behaviour unchanged when `dock` is false.

Do NOT touch `graph.rs`, `flow.rs`, `sidebar.rs`, `components.rs`, `sections.rs` or
`README.md`. This run opens no PR, so the README review-banner rule does not apply.

## Behaviour (exact)

1. **`RunPanel`** implements `workspace::Panel` with:
   - `persistent_name` "mini-ork Run";
   - default position `DockPosition::Right`;
   - default size 380px;
   - `activation_priority` 50, unique: Zed panels use 0–7;
   - an icon from the existing `IconName` set (e.g. `IconName::ListTodo` or similar). Pick an
     existing one; do not add assets.

   Model it on an existing small panel. Read how `crates/debugger_ui/src/debugger_panel.rs` (or
   `crates/terminal_view/src/terminal_panel.rs`) implements `Panel`, `Focusable`,
   `EventEmitter<PanelEvent>` and `Render`. Copy only the trait plumbing.
2. **Registration.** In `init`: `cx.observe_new(|workspace: &mut Workspace, window, cx| …)` adds
   the panel with `workspace.add_panel(panel, window, cx)`, the same way `status.rs` /
   `title_bar.rs` hook new workspaces. It must be a no-op when there is no window.
3. **Content.**
   - The panel hosts a `MiniOrkPage` in dock mode: args `run=<id>`, `view=dock`, tab default
     `changes`.
   - The page's own tab bar (from the JSON `tabs`) switches dock tabs, exactly like any page.
4. **Which run.**
   - The panel follows the ACTIVE run. When the workspace's active item is a `MiniOrkPage`
     whose args carry `run=<id>`, the panel switches to that run. Subscribe to the workspace's
     active item changes.
   - When there is no run yet, show a short empty state: "Open a run to see its changes and
     checks".
5. **Reload only while visible.** The panel's page reloads with the board store like other
   pages, but it must not poll when the panel is closed (not visible).

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

- `script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings from
  `run_panel.rs` / `page.rs`.
- `git diff --stat` touches only the files in scope.
- A short note pointing at the Panel impl, the registration, and the active-run follow
  (file:line).
