# mini-ork IDE: the step graph is back as the Graph view, and every step shows WHO does it (an LLM's logo or a command icon)

## Why (the user, 2026-10-09)

> "lets have the previous draw with logo of each lane LLM or the shell in each lane to make it
> clear what llm or if command, command logo is doing the job at that step"

The Graph tab currently opens on the new flow map (`flow.rs`). The user wants the previous
column step graph back as the Graph view: the run page's `dag` section, and the full-screen
graph in `graph.rs`. Every step must make obvious who does the job.

The data is already there. Each DAG node in the page JSON (`page["graph"]["cols"]` and the `dag`
section) carries `lane`, already resolved to what served the step:
- `deepseek_flash`, `opus`, `glm`, `minimax`, `codex`, …;
- `shell` for a command step (typecheck, test, publisher, rollback, verifiers).

But the logo is tiny today: `p::lane_mark(&lane, 11.)`, next to the lane text. And for `shell`
it draws only a dim dot (`palette.rs` `lane_mark` ~:303), so a command step looks like nothing.

## Files in scope (touch ONLY these, under `crates/mini_ork_ui/src/`)

- `palette.rs`: `lane_mark` (the shell case), plus a new `pub fn lane_label(lane: &str) -> String`
- `page.rs`:
  - ONLY the Graph-tab dispatch (~:1590-1600): DAG by default, the flow map behind a toggle;
  - the run-page `dag` card layout (~:1120-1150);
  - one `bool` field for the toggle.
- `graph.rs`: ONLY the full-screen graph's node card layout (~:835-870 and the matching block
  ~:1000-1015)

Do NOT touch `flow.rs`, `sidebar.rs`, `sections.rs`, `components.rs` or `README.md`. This run
opens no PR, so the README review-banner rule does not apply.

## Changes (exact)

1. **`palette.rs`:**
   - `lane_mark(lane, size)`: when the lane is `shell`, `command`, `cmd` or empty, draw Zed's
     terminal icon (`IconName::Terminal` via `gpui::svg().path(...)`) at `size`, tinted SUB.
     Not the dim dot.
   - New `lane_label(lane)`, a human name for the step's worker:
     - `opus` → "Claude Opus";
     - `sonnet` → "Claude Sonnet";
     - `haiku` → "Claude Haiku";
     - any other `claude*` → "Claude";
     - `deepseek*` → "DeepSeek";
     - `glm*` / `zai` / `zhipu` → "GLM (z.ai)";
     - `minimax*` → "MiniMax";
     - `codex` / `gpt*` / `openai` → "OpenAI Codex";
     - `kimi` / `moonshot` → "Kimi";
     - `qwen` → "Qwen";
     - `gemini` → "Gemini";
     - `mistral` → "Mistral";
     - `grok` / `xai` → "Grok";
     - `shell` / `command` / `cmd` → "command";
     - otherwise the lane itself.

     Match case-insensitively by substring, the same way `model_image` does.
2. **Step cards** (run-page `dag` cards in `page.rs`, and the full-screen cards in `graph.rs`):
   - **Who first.**
     - The FIRST row is the worker's mark at 18px (full-screen: `18. * z`), then the step label
       (12.5px semibold).
     - The second row is `lane_label(lane)` in 11px TEXT colour (not MUTED), with the role after
       a `·` in SUB when present.
     - The status / cost / duration row stays as it is.
   - The state glyph that is the first child today moves to the status row, so the logo leads
     the card.
   - Keep the click, hover and selection behaviour unchanged.
3. **Graph tab** (`page.rs` ~:1590):
   - Show the `dag` section (the step graph) by default, i.e. `hide_dag` false.
   - Add a small two-option toggle above the graph: **Steps** (default) · **Flow map**. Mono
     11px; the selected option in TEXT, the other in SUB, clickable.
   - **Flow map** shows the existing flow map and hides the dag section, as today.
   - Store the choice in one `bool` field on `MiniOrkPage`, default false (= Steps). It persists
     while the page lives; a run change does not reset it.

## Build rules (IMPORTANT)

- Build ONLY with `script/mini-ork-build`. It is slow on this host, so build ONCE at the end,
  and once more only if it fails.
- NEVER run `cargo test` / `cargo check` / `cargo build` / `cargo clippy` directly, and never
  copy the repo.
- Do not run the IDE. Do not `git push`.
- Ignore the untracked `.mini-ork/` directory at the repo root. Do not touch, add or delete it.

## Verification command

```bash
script/mini-ork-build   # must exit 0
```

## Done when

- `script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings from the touched
  files.
- `git diff --stat` touches only the three files in scope.
- A short note pointing at the shell icon, `lane_label`, both card layouts, and the toggle
  (file:line).
