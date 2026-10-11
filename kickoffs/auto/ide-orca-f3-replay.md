# mini-ork IDE: replay a run on its flow map (Pause / Restart / 0.5×–2×), and follow a live run

## Why

The run flow map is drawn:
- Part A (zed `137fecd`): Ticket → Design → Build → Integrate.
- Part B (zed `0bf6910`): the Review hub and spokes, the Fix spoke, the optional Merge/Human
  nodes, the legend, and the stage chips.

The user's reference design also has replay controls: **Pause**, **Restart**, and a speed
choice (0.5× / 1× / 2×) that animate the run step by step.

The run page JSON already carries what replay needs, in `page["flow"]["timeline"]`:

```
[{"t": <epoch seconds>, "node": <node id>, "event": "start"|"end", "state": "in_progress"|"approved"|"sent_back"|"skipped"}]
```

The list is in order and capped at 400 entries (built by
`/Volumes/docker-ssd/ps/mini-ork/mini_ork/ide_pages/run_flow.py` `_timeline`). The steps in the
map carry their node id in `id` (design/build/integrate/review steps).

## Files in scope (touch ONLY these, under `crates/mini_ork_ui/src/`)

- `flow.rs`:
  - the replay controls;
  - a replay clock;
  - a state override applied to the parsed `Flow` before painting.
- `page.rs`: ONLY the minimal fields the replay needs on `MiniOrkPage`:
  - replay state: playing, speed, the clock position, and the run id it belongs to;
  - one timer task that ticks while playing.

Do NOT touch `graph.rs`, `sidebar.rs`, `components.rs` or `README.md`. This run opens no PR,
so the README review-banner rule does not apply.

## Behaviour (exact)

1. **Controls bar** at the top-left of the flow map, in mono 11px, in the style of the legend
   at the top-right:
   - `▶ Replay` / `❚❚ Pause` (toggles);
   - `↺ Restart`;
   - `0.5×` · `1×` · `2×` (the selected one in TEXT colour, the others SUB).

   Hidden when `timeline` is empty.
2. **The replay clock.**
   - Replay time runs from the first timeline `t` to the last.
   - At 1×, the whole timeline plays in about 12 seconds of wall time, whatever the run's real
     length (scale = 12 s / span). 0.5× and 2× scale that.
   - Tick about every 50 ms with a `cx.spawn` timer that calls `cx.notify()`. Stop ticking at
     the end or on Pause.
3. **State override while replaying** (playing, or paused mid-way):
   - For each step whose `id` appears in the timeline, the state is its LAST timeline event
     at or before the replay time:
     - `start` → `in_progress`;
     - `end` → that event's `state`;
     - no event yet → `pending`.
   - Stage states follow the same worst-of rule `run_flow.py` uses: sent_back/failed >
     in_progress > pending > approved.
   - Steps with no timeline entries keep their page state.
   - When the replay ends, or on Restart → stop, clear the override, and show the real
     states.
4. **Live run.** When the run is still running (any step is `in_progress` in the page data),
   no replay is needed: the page already reloads. Show a small `● live` badge (BLUE) in place
   of `▶ Replay`.
5. Changing run, or leaving the Graph tab, stops the replay.

## Build rules (IMPORTANT)

- Build ONLY with `script/mini-ork-build`. It is slow on this host (up to about an hour), so
  build ONCE at the end, and once more only if it fails.
- NEVER run `cargo test` / `cargo check` / `cargo build` / `cargo clippy` directly, and never
  copy the repo.
- Do not run the IDE. Do not `git push`.

## Verification command

```bash
script/mini-ork-build   # must exit 0
```

## Done when

- `script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings from `flow.rs` /
  `page.rs`.
- `git diff --stat` touches only the files in scope.
- A short note pointing at the controls, the clock and the override (file:line).
